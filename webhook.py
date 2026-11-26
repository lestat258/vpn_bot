#!/usr/bin/env python3
"""
Webhook сервер для обработки платежей ЮKassa
"""
import logging
import json
import sqlite3
import asyncio
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, Dict, Any

from aiohttp import web
from aiogram import Bot, types

from database import get_setting
from services import vpn_service
from server_manager import server_manager
from tax_manager import tax_manager

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# КОНСТАНТЫ
# ============================================================

DB_PATH = '/opt/vpn-bot/data.db'

# ============================================================
# БАЗА ДАННЫХ (контекстный менеджер)
# ============================================================

@contextmanager
def get_db():
    """Контекстный менеджер для БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ============================================================
# АСИНХРОННЫЕ ОБЕРТКИ ДЛЯ БД
# ============================================================

async def async_get_db_query(query: str, params: tuple = ()) -> list:
    """Асинхронная обертка для запросов к БД"""
    def _sync_query():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchall()
    return await asyncio.to_thread(_sync_query)

async def async_db_execute(query: str, params: tuple = ()) -> int:
    """Асинхронная обертка для выполнения запросов к БД"""
    def _sync_execute():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            return c.lastrowid
    return await asyncio.to_thread(_sync_execute)

async def async_update_db(query: str, params: tuple = ()) -> bool:
    """Асинхронная обертка для обновления БД"""
    def _sync_update():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            return True
    return await asyncio.to_thread(_sync_update)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

async def get_active_subscription(telegram_id: int) -> Optional[Dict]:
    """Асинхронное получение активной подписки"""
    query = '''
        SELECT id, xui_client_uid, xui_email, end_date, tariff_id
        FROM subscriptions
        WHERE telegram_id = ? AND is_active = 1
        ORDER BY end_date DESC LIMIT 1
    '''
    rows = await async_get_db_query(query, (telegram_id,))
    return dict(rows[0]) if rows else None

async def get_tariff_info(tariff_id: int) -> Optional[Dict]:
    """Асинхронное получение информации о тарифе"""
    query = 'SELECT name, duration_days, traffic_gb, ip_limit FROM tariffs WHERE id = ?'
    rows = await async_get_db_query(query, (tariff_id,))
    return dict(rows[0]) if rows else None

# ============================================================
# ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ ПЛАТЕЖА
# ============================================================

async def process_payment_success(payment_id, data):
    """Fulfill a payment previously verified through YooKassa API."""
    from payment_fulfillment import process_verified_payment
    from tax_fulfillment import queue_verified
    # Commit verified fiscal work before fulfillment. The dedicated worker
    # independently rechecks YooKassa and never blocks the VPN response.
    await asyncio.to_thread(queue_verified, DB_PATH, payment_id)
    return await process_verified_payment(payment_id, data)


# ============================================================
# ВЕБХУК
# ============================================================

async def verify_provider_payment(payment_id):
    """Read-only provider lookup and validation shared by webhook and tax worker."""
    from decimal import Decimal

    rows = await async_get_db_query(
        "SELECT telegram_id, tariff_id, amount_rub "
        "FROM payments WHERE payment_id = ?",
        (payment_id,)
    )

    if not rows:
        logger.warning("Unknown payment: %s", payment_id)
        return None, web.Response(status=404, text="Not Found")

    shop_id = get_setting("yookassa_shop_id")
    stored_secret = get_setting("yookassa_secret_key")

    if not shop_id or not stored_secret:
        logger.error("YooKassa credentials missing")
        return None, web.Response(status=503, text="Unavailable")

    try:
        from encryption import decrypt
        secret = decrypt(stored_secret)
    except Exception:
        secret = stored_secret

    def verify_payment():
        from yookassa import Configuration, Payment
        Configuration.account_id = str(shop_id)
        Configuration.secret_key = secret
        return Payment.find_one(payment_id)

    try:
        verified = await asyncio.to_thread(verify_payment)
    except Exception as exc:
        logger.error(
            "YooKassa API error: %s",
            type(exc).__name__
        )
        return None, web.Response(status=503, text="Unavailable")

    if (
        str(getattr(verified, "id", "")) != payment_id
        or verified.status != "succeeded"
        or verified.paid is not True
    ):
        return None, web.Response(status=403, text="Unconfirmed")

    try:
        expected = Decimal(
            f"{float(rows[0]['amount_rub']):.2f}"
        )
        actual = Decimal(str(verified.amount.value))
        valid_amount = (
            actual == expected
            and verified.amount.currency == "RUB"
        )
    except Exception:
        valid_amount = False

    if not valid_amount:
        logger.error("Amount mismatch: %s", payment_id)
        return None, web.Response(status=403, text="Amount mismatch")

    metadata = getattr(verified, "metadata", None) or {}

    if (
        not isinstance(metadata, dict)
        or str(metadata.get("telegram_id")) != str(rows[0]["telegram_id"])
        or str(metadata.get("tariff_id")) != str(rows[0]["tariff_id"])
    ):
        logger.error("Metadata mismatch: %s", payment_id)
        return None, web.Response(status=403, text="Metadata mismatch")

    verified_data = {
        "object": {
            "id": payment_id,
            "metadata": metadata,
            "captured_at": getattr(verified, "captured_at", None),
            "refunded_amount": getattr(verified, "refunded_amount", None)
        }
    }

    return verified_data, None


async def verify_for_tax(payment_id):
    from decimal import Decimal
    from tax_fulfillment import TaxReviewRequired, validated_operation_time

    verified, error = await verify_provider_payment(payment_id)
    if error is not None:
        if error.status >= 500:
            raise RuntimeError("Provider unavailable")
        raise TaxReviewRequired("Provider payment mismatch")
    obj = verified["object"]
    refunded = obj.get("refunded_amount")
    if refunded is not None:
        try:
            value = Decimal(str(refunded.value))
            if not value.is_finite() or value < 0:
                raise ValueError()
        except Exception:
            raise TaxReviewRequired("Invalid refunded amount") from None
        if value > 0:
            raise TaxReviewRequired("Payment has a refund")
    try:
        return validated_operation_time(obj.get("captured_at"))
    except (ValueError, TypeError):
        raise TaxReviewRequired("Capture timestamp unavailable") from None


async def tax_worker_context(app):
    from contextlib import suppress
    from tax_fulfillment import drain_queue, interrupted_count

    async def worker():
        while True:
            try:
                count = await asyncio.to_thread(interrupted_count, DB_PATH)
                if count:
                    logger.warning("Fiscal processing interrupted: %d require manual reconciliation", count)
                await drain_queue(DB_PATH, verify_for_tax)
            except Exception as exc:
                logger.error("Fiscal queue unavailable: %s", type(exc).__name__)
            await asyncio.sleep(15)

    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def handle_webhook(request):

    try:
        data = await request.json()

        if not isinstance(data, dict) or data.get("type") != "notification":
            return web.Response(status=400, text="Bad Request")

        if data.get("event") != "payment.succeeded":
            return web.Response(status=200, text="OK")

        obj = data.get("object")
        if not isinstance(obj, dict):
            return web.Response(status=400, text="Bad Request")

        payment_id = obj.get("id")
        if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 128:
            return web.Response(status=400, text="Bad Request")

        verified_data, error = await verify_provider_payment(payment_id)
        if error is not None:
            return error

        success = await process_payment_success(
            payment_id, verified_data
        )

        return web.Response(
            status=200 if success else 500,
            text="OK" if success else "Processing failed"
        )

    except Exception as exc:
        logger.error(
            "Webhook error: %s",
            type(exc).__name__
        )
        return web.Response(status=500, text="Internal Error")


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    app = web.Application()
    app.cleanup_ctx.append(tax_worker_context)
    app.router.add_post('/webhook/', handle_webhook)
    app.router.add_get('/health', lambda r: web.Response(text='OK'))
    
    logger.info("🚀 Webhook server started on port 8081")
    web.run_app(app, host='0.0.0.0', port=8081)
