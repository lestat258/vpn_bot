"""Versioned one-use callbacks for tracked private navigation screens.

Only navigation.db is used. Business notifications keep a narrowly scoped,
non-financial navigation exception; URL buttons are never rewritten.
"""
import json
import logging
import secrets
import sqlite3
from contextlib import closing

from aiogram import BaseMiddleware
from aiogram.types import InlineKeyboardMarkup

LOG = logging.getLogger(__name__)
PREFIX = "nav:"
STALE = "Кнопка устарела. Откройте актуальный раздел или отправьте /menu."
NOTICE_PREFIXES = (
    "⏳ Ваша подписка истекает через ",
    "🚨 Ваша подписка истекает завтра!",
    "❌ Ваша подписка истекла сегодня!",
)


def prepare_buttons(kwargs):
    """Replace callback payloads with short opaque tokens, leaving URLs intact."""
    markup = kwargs.get("reply_markup")
    mapping = {}
    if not isinstance(markup, InlineKeyboardMarkup):
        return kwargs, mapping
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            value = button.callback_data
            if value is not None:
                token = PREFIX + secrets.token_hex(16)
                mapping[token] = value
                button = button.model_copy(update={"callback_data": token})
            new_row.append(button)
        rows.append(new_row)
    return {**kwargs, "reply_markup": markup.model_copy(update={"inline_keyboard": rows})}, mapping


def _db():
    # Lazy import avoids a cycle with the shared renderer.
    from screen_navigation import _screen_db
    db = _screen_db()
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS callback_screens(
            bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, buttons TEXT NOT NULL,
            PRIMARY KEY(bot_id,chat_id))""")
    except Exception:
        db.close()
        raise
    return db


def invalidate(key):
    """Fail closed even if the subsequent Telegram render is ambiguous."""
    with closing(_db()) as db, db:
        db.execute("DELETE FROM callback_screens WHERE bot_id=? AND chat_id=?", key)


def remember(key, mid, mapping):
    if not mapping:
        return
    with closing(_db()) as db, db:
        db.execute("""INSERT INTO callback_screens(bot_id,chat_id,message_id,buttons)
            VALUES(?,?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE
            SET message_id=excluded.message_id,buttons=excluded.buttons""",
            (*key, mid, json.dumps(mapping, ensure_ascii=False)))


def displayed(message, value):
    markup = getattr(message, "reply_markup", None)
    return any(getattr(b, "callback_data", None) == value
               for row in (getattr(markup, "inline_keyboard", None) or [])
               for b in row)


def notice_button(message, value):
    """Only known non-mutating entry points from durable notifications."""
    if not displayed(message, value):
        return False
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    if value == "paid_key_guide":
        return True
    if value in {"my_keys", "back_to_main"}:
        return text.startswith("✅ Пробная подписка создана!")
    return value == "back_to_tariffs" and text.startswith(NOTICE_PREFIXES)


def consume(key, mid, token):
    """Atomically consume one current token; return its original keyboard map.

    This survives restarts and rejects older versions of the same message.
    """
    with closing(_db()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT message_id FROM active_screens WHERE bot_id=? AND chat_id=?", key).fetchone()
        row = db.execute("SELECT message_id,buttons FROM callback_screens WHERE bot_id=? AND chat_id=?", key).fetchone()
        if not active or active[0] != mid or not row or row[0] != mid:
            return None
        mapping = json.loads(row[1])
        if token not in mapping:
            return None
        remaining = dict(mapping)
        del remaining[token]
        db.execute("UPDATE callback_screens SET buttons=? WHERE bot_id=? AND chat_id=?",
                   (json.dumps(remaining, ensure_ascii=False), *key))
        return mapping


def decode_markup(markup, mapping):
    rows = [[b.model_copy(update={"callback_data": mapping[b.callback_data]})
             if b.callback_data in mapping else b for b in row]
            for row in markup.inline_keyboard]
    return markup.model_copy(update={"inline_keyboard": rows})


class CurrentCallbackNavigation(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from screen_navigation import _key
        message = getattr(event, "message", None)
        key = _key(message)
        value = getattr(event, "data", None)
        actor = getattr(event, "from_user", None)
        if (key is None or getattr(actor, "id", None) != key[1]
                or not isinstance(value, str) or not displayed(message, value)):
            await event.answer(STALE, show_alert=True)
            return
        if not value.startswith(PREFIX):
            if notice_button(message, value):
                return await handler(event, data)
            await event.answer(STALE, show_alert=True)
            return
        try:
            mapping = consume(key, message.message_id, value)
        except (sqlite3.Error, ValueError, TypeError):
            LOG.warning("Callback navigation registry unavailable")
            mapping = None
        if not mapping:
            await event.answer(STALE, show_alert=True)
            return
        # Preserve the existing FSM/router/checkout checks: they still see
        # logical callback_data and its original button in the message.
        decoded = message.model_copy(update={"reply_markup": decode_markup(message.reply_markup, mapping)})
        event = event.model_copy(update={"data": mapping[value], "message": decoded})
        return await handler(event, data)
