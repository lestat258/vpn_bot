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
    success = await process_verified_payment(payment_id, data)
    if success:
        try:
            from tax_fulfillment import process_tax_receipt
            await process_tax_receipt(payment_id, db_path=DB_PATH)
        except Exception as exc:
            logger.error('Receipt workflow requires review: %s', type(exc).__name__)
    return success


# ============================================================
# ВЕБХУК
# ============================================================

async def handle_webhook(request):
    from decimal import Decimal

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

        rows = await async_get_db_query(
            "SELECT telegram_id, tariff_id, amount_rub "
            "FROM payments WHERE payment_id = ?",
            (payment_id,)
        )

        if not rows:
            logger.warning("Unknown payment: %s", payment_id)
            return web.Response(status=404, text="Not Found")

        shop_id = get_setting("yookassa_shop_id")
        stored_secret = get_setting("yookassa_secret_key")

        if not shop_id or not stored_secret:
            logger.error("YooKassa credentials missing")
            return web.Response(status=503, text="Unavailable")

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
            return web.Response(status=503, text="Unavailable")

        if (
            str(getattr(verified, "id", "")) != payment_id
            or verified.status != "succeeded"
            or verified.paid is not True
        ):
            return web.Response(status=403, text="Unconfirmed")

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
            return web.Response(status=403, text="Amount mismatch")

        metadata = getattr(verified, "metadata", None) or {}

        if (
            not isinstance(metadata, dict)
            or str(metadata.get("telegram_id")) != str(rows[0]["telegram_id"])
            or str(metadata.get("tariff_id")) != str(rows[0]["tariff_id"])
        ):
            logger.error("Metadata mismatch: %s", payment_id)
            return web.Response(status=403, text="Metadata mismatch")

        verified_data = {
            "object": {
                "id": payment_id,
                "metadata": metadata
            }
        }

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
    app.router.add_post('/webhook/', handle_webhook)
    app.router.add_get('/health', lambda r: web.Response(text='OK'))
    
    logger.info("🚀 Webhook server started on port 8081")
    web.run_app(app, host='0.0.0.0', port=8081)
