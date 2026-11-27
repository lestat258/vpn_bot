"""Shared renderer for bot-owned interactive callback screens.

Text screens are edited in place. Media-to-text and reply keyboards need a
replacement. Send first, then retire the old interactive screen. Never erase
payment/issuance notifications and never retry an ambiguous send.
"""
import logging
import asyncio
import sqlite3
from contextlib import closing
from weakref import WeakValueDictionary
from runtime_config import DATA_DIR
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply

LOG=logging.getLogger(__name__)


def significant_message(message):
    text=getattr(message,"text",None) or getattr(message,"caption",None) or ""
    if text.startswith(("✅ Пробная подписка создана!", "✅ Ваш VPN-ключ!", "✅ Подписка продлена!")):
        return True
    markup=getattr(message,"reply_markup",None)
    return any(getattr(button,"callback_data",None)=="paid_key_guide"
               for row in (getattr(markup,"inline_keyboard",None) or []) for button in row)


async def retire_screen(message):
    try:
        await message.delete()
    except TelegramAPIError:
        # A replacement already exists. Do not retry the send or report that
        # the business operation failed just because cleanup was unavailable.
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            LOG.warning("Old navigation screen could not be retired")


async def _render_screen(message,text,**kwargs):
    """Render one screen. Only use for navigation from a bot-owned message."""
    if significant_message(message):
        return await message.answer(text,**kwargs)
    reply=kwargs.get("reply_markup")
    needs_replacement=(getattr(message,"text",None) is None or
                       isinstance(reply,(ReplyKeyboardMarkup,ReplyKeyboardRemove,ForceReply)))
    if not needs_replacement:
        try:
            result=await message.edit_text(text,**kwargs)
            return message if result is True else result
        except TelegramBadRequest as exc:
            reason=str(exc).lower()
            if "message is not modified" in reason:
                return message
            if not any(part in reason for part in
                       ("message can't be edited","message to edit not found","there is no text in the message to edit")):
                raise
    new=await message.answer(text,**kwargs)
    await retire_screen(message)
    return new


# Only explicit navigation screens are registered, not business notifications.
NAV_DB_PATH = DATA_DIR / "navigation.db"
_locks = WeakValueDictionary()


def _key(message, *, command=False):
    chat=getattr(message,"chat",None)
    bot=getattr(message,"bot",None)
    actor=getattr(message,"from_user",None)
    if getattr(chat,"type",None)!="private" or bot is None:
        return None
    expected=chat.id if command else bot.id
    if getattr(actor,"id",None)!=expected:
        return None
    return (bot.id,chat.id)


def _lock(key):
    lock=_locks.get(key)
    if lock is None:
        lock=asyncio.Lock()
        _locks[key]=lock
    return lock


def _screen_db():
    db=sqlite3.connect(NAV_DB_PATH,timeout=5)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS active_screens(
            bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('text','photo')),
            PRIMARY KEY(bot_id,chat_id))""")
    except Exception:
        db.close()
        raise
    return db


def _read(key):
    with closing(_screen_db()) as db:
        return db.execute("SELECT message_id,kind FROM active_screens WHERE bot_id=? AND chat_id=?",key).fetchone()


def _save(key,mid,kind):
    with closing(_screen_db()) as db,db:
        db.execute("""INSERT INTO active_screens(bot_id,chat_id,message_id,kind)
            VALUES(?,?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE
            SET message_id=excluded.message_id,kind=excluded.kind""",(*key,mid,kind))


async def _retire_id(bot,chat_id,mid):
    try:
        await bot.delete_message(chat_id=chat_id,message_id=mid)
    except TelegramAPIError:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id,message_id=mid,reply_markup=None)
        except TelegramAPIError:
            LOG.warning("Previous active screen could not be retired")


async def _remember(key,message,result,previous,kind,*,retired_source=False):
    mid=getattr(result,"message_id",None)
    if not isinstance(mid,int) or mid<=0:
        return
    _save(key,mid,kind)
    if previous and previous[0]!=mid:
        if retired_source and previous[0]==message.message_id:
            return
        await _retire_id(message.bot,key[1],previous[0])


async def render_screen(message,text,**kwargs):
    key=_key(message)
    if key is None:
        return await _render_screen(message,text,**kwargs)
    async with _lock(key):
        previous=_read(key)
        preserved=significant_message(message)
        result=await _render_screen(message,text,**kwargs)
        if preserved and previous and previous[0]==message.message_id:
            previous=None
        await _remember(key,message,result,previous,"text",retired_source=not preserved)
        return result


async def render_command(message,text,**kwargs):
    """Open a menu from a private user message, without editing/deleting it."""
    key=_key(message,command=True)
    if key is None:
        return await message.answer(text,**kwargs)
    async with _lock(key):
        previous=_read(key)
        reply=kwargs.get("reply_markup")
        if previous and previous[1]=="text" and not isinstance(reply,(ReplyKeyboardMarkup,ReplyKeyboardRemove,ForceReply)):
            try:
                return await message.bot.edit_message_text(text=text,chat_id=key[1],
                                                            message_id=previous[0],**kwargs)
            except TelegramBadRequest as exc:
                reason=str(exc).lower()
                if "message is not modified" in reason:
                    return None
                if not any(part in reason for part in ("message can't be edited",
                        "message to edit not found","there is no text in the message to edit")):
                    raise
        result=await message.answer(text,**kwargs)
        await _remember(key,message,result,previous,"text")
        return result


async def render_photo_screen(message,media,**kwargs):
    """Track a QR card, preserving a significant message if used as origin."""
    key=_key(message)
    async def show():
        if not significant_message(message):
            try:
                result=await message.edit_media(media=media,**kwargs)
                return message if result is True else result
            except TelegramBadRequest as exc:
                reason=str(exc).lower()
                if "message is not modified" in reason:
                    return message
                if not any(part in reason for part in ("message can't be edited",
                            "message to edit not found","there is no media in the message to edit")):
                    raise
        result=await message.answer_photo(photo=media.media,caption=media.caption,
                                          parse_mode=media.parse_mode,**kwargs)
        if not significant_message(message):
            await retire_screen(message)
        return result
    if key is None:
        return await show()
    async with _lock(key):
        previous=_read(key)
        preserved=significant_message(message)
        result=await show()
        if preserved and previous and previous[0]==message.message_id:
            previous=None
        await _remember(key,message,result,previous,"photo",retired_source=not preserved)
        return result
