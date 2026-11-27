"""Shared renderer for bot-owned interactive callback screens.

Text screens are edited in place. Media-to-text and reply keyboards need a
replacement. Send first, then retire the old interactive screen. Never erase
payment/issuance notifications and never retry an ambiguous send.
"""
import logging
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


async def render_screen(message,text,**kwargs):
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
