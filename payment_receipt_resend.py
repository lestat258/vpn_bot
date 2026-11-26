"""Explicit resend of a verified existing receipt. No fiscal or database writes."""
import asyncio
import base64
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from payment_admin_check import order_snapshot, provider_read, provider_state, fiscal_read
from refund_fulfillment import money
from tax_fulfillment import public_receipt_url

LOG = logging.getLogger(__name__)


def read_settings(path, keys):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        return dict(db.execute(
            "SELECT key,value FROM settings WHERE key IN (" + ",".join("?" for _ in keys) + ")",
            keys))


def receipt_candidate(snapshot):
    if not snapshot:
        raise ValueError("Outside scope")
    p, j, receipts = snapshot
    uid = j.get("receipt_uuid")
    if (p["status"] != "paid" or j.get("payment_state") != "verified"
            or j.get("tax_state") != "done" or j.get("refund_state") != "none"
            or j.get("is_legacy") or not uid or len(receipts) != 1):
        raise ValueError("Not eligible")
    r = receipts[0]
    if (r["receipt_uuid"] != uid or money(r["amount"]) != money(p["amount_rub"])
            or r["user_id"] != p["telegram_id"] or int(p["telegram_id"]) <= 0):
        raise ValueError("Receipt identity mismatch")
    return p, j, uid


def verified_link(path, uid, local_url):
    inn = read_settings(path, ("tax_inn",))["tax_inn"]
    url = public_receipt_url(inn, uid)
    if local_url != url:
        raise ValueError("Receipt URL mismatch")
    return url


def bot_token(path):
    settings = read_settings(path, ("bot_token", "encryption_key"))
    token = settings["bot_token"]
    pattern = r"[0-9]+:[A-Za-z0-9_-]{20,}"
    if not isinstance(token, str) or not re.fullmatch(pattern, token):
        from cryptography.fernet import Fernet
        token = Fernet(base64.urlsafe_b64decode(settings["encryption_key"])).decrypt(
            base64.urlsafe_b64decode(token)).decode()
    if not re.fullmatch(pattern, token):
        raise ValueError("Invalid bot token")
    return token


async def send_receipt(path, user_id, url):
    # Do not import encryption.py: it runs database migrations on import.
    from aiogram import Bot
    from html import escape
    token = await asyncio.to_thread(bot_token, path)
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=user_id,
            text='🧾 <b>Чек об оплате</b>\n<a href="' + escape(url, quote=True) + '">Открыть чек</a>',
            parse_mode="HTML")
    finally:
        await bot.session.close()


async def resend_order(path, order_id, *, provider=None, fiscal=None, sender=None):
    result = {"order_id": order_id, "lines": [], "kind": "resend"}
    def outcome(message, ok=False):
        result["summary"] = message
        result["ok"] = ok
        return result
    try:
        snapshot = await asyncio.to_thread(order_snapshot, path, order_id)
        p, j, uid = receipt_candidate(snapshot)
        url = await asyncio.to_thread(verified_link, path, uid, j.get("receipt_url"))
        data = await asyncio.wait_for((provider or provider_read)(path, p["payment_id"]), 20)
        state, refunded = provider_state(p, data)
        if state != "succeeded" or refunded != 0:
            return outcome("Отправка заблокирована: оплата или отсутствие возврата не подтверждены.")
        if await asyncio.wait_for((fiscal or fiscal_read)(path, uid, money(p["amount_rub"])), 100) != "active":
            return outcome("Отправка заблокирована: чек не является действующим.")
        # A concurrent update during the external checks must invalidate this attempt.
        if await asyncio.to_thread(order_snapshot, path, order_id) != snapshot:
            return outcome("Заказ изменился во время проверки. Повтори сверку.")
    except Exception as exc:
        LOG.warning("Receipt resend validation failed: %s", type(exc).__name__)
        return outcome("Отправка заблокирована: не удалось подтвердить заказ и действующий чек.")
    try:
        await asyncio.wait_for((sender or send_receipt)(path, int(p["telegram_id"]), url), 30)
    except Exception as exc:
        LOG.warning("Receipt resend delivery unconfirmed: %s", type(exc).__name__)
        return outcome("Доставка в Telegram не подтверждена. Не повторяй отправку сразу.")
    return outcome("Существующий чек отправлен покупателю в Telegram.", True)
