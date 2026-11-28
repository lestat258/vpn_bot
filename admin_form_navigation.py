"""Screen-scoped administrative tariff and text forms.

Only these form states are managed here. Other admin workflows retain their
existing routing until separately migrated.
"""
from aiogram import BaseMiddleware, types
from states import AddTariff, EditTariff, EditText, AddServer, SendMessageToUser
from aiogram.exceptions import TelegramAPIError
from form_navigation import is_current_screen, menu_navigation, FORM_SCREEN
from screen_navigation import render_screen, render_command, _key, _read

PARENTS = {s.state: "admin_tariffs" for s in (*AddTariff.__all_states__, *EditTariff.__all_states__)}
PARENTS[EditText.waiting.state] = "admin_texts"
SELECT = EditTariff.tariff_id.state
PARENTS.update({s.state: "admin_servers_menu" for s in AddServer.__all_states__})
SEND = SendMessageToUser.message_text.state
PARENTS[SEND] = "admin_users"
STARTS = {"admin_add_tariff", "admin_edit_tariff", "admin_add_server"}


def parent_for(current, fields):
    if current == SEND:
        user = fields.get("user_id")
        if type(user) is int and user > 0:
            return f"user_back_to_{user}"
    return PARENTS[current]
PROMPT = "admin_form_prompt"


def back_keyboard(parent):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data=parent)]
    ])


def callback_is_current(event):
    message = event.message
    if not is_current_screen(message, getattr(message, "message_id", None)):
        return False
    markup = getattr(message, "reply_markup", None)
    return any(getattr(button, "callback_data", None) == event.data
               for row in (getattr(markup, "inline_keyboard", None) or [])
               for button in row)


def private_admin(event, admin_id):
    message = getattr(event, "message", event)
    chat = getattr(message, "chat", None)
    return (getattr(getattr(event, "from_user", None), "id", None) == admin_id
            and getattr(chat, "type", None) == "private"
            and getattr(chat, "id", None) == admin_id)


def tracked_id(message, *, command):
    key = _key(message, command=command)
    row = _read(key) if key else None
    if not row:
        raise RuntimeError("Administrative form has no active screen")
    return row[0]


async def begin(callback, state, target, text, *, fields=None, keyboard=None):
    parent = parent_for(target.state, fields or {})
    await state.clear()
    await render_screen(callback.message, text, reply_markup=keyboard or back_keyboard(parent))
    await state.set_data({**(fields or {}), FORM_SCREEN: tracked_id(callback.message, command=False), PROMPT: text})
    await state.set_state(target)
    await callback.answer()


async def step(message, state, target, text, **fields):
    await render_command(message, text, reply_markup=back_keyboard(parent_for(target.state, {**await state.get_data(), **fields})))
    await state.update_data(**fields, **{FORM_SCREEN: tracked_id(message, command=True), PROMPT: text})
    await state.set_state(target)


async def retry(message, state, error):
    current = await state.get_state()
    data = await state.get_data()
    text = error + "\n\n" + data[PROMPT]
    await render_command(message, text, reply_markup=back_keyboard(parent_for(current, data)))
    await state.update_data(**{FORM_SCREEN: tracked_id(message, command=True)})


async def finish(message, state, text, *, clean_cancel=False):
    current = await state.get_state()
    parent = parent_for(current, await state.get_data())
    # Clear before displaying result: an ambiguous Telegram response must not
    # repeat an already committed administrative operation.
    await state.clear()
    await render_command(message, text, reply_markup=back_keyboard(parent), clean_cancel=clean_cancel)


class AdminFormNavigation(BaseMiddleware):
    def __init__(self, admin_id):
        self.admin_id = admin_id

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)
        current = await state.get_state()
        callback = hasattr(event, "data") and hasattr(event, "message")
        if callback:
            value = event.data or ""
            select = value.startswith("admin_edit_tariff_select_")
            start = value in STARTS or value.startswith(("edittext_", "user_send_message_"))
            if current not in PARENTS:
                if start or select:
                    if (current is not None or select or not private_admin(event, self.admin_id)
                            or not callback_is_current(event)):
                        await event.answer("Откройте актуальный раздел админки.", show_alert=True)
                        return
                return await handler(event, data)
            stored = (await state.get_data()).get(FORM_SCREEN)
            if (not private_admin(event, self.admin_id)
                    or getattr(event.message, "message_id", None) != stored
                    or not callback_is_current(event)):
                await event.answer("Откройте актуальную форму.", show_alert=True)
                return
            if value == parent_for(current, await state.get_data()):
                await state.clear()
                data["raw_state"] = None
                return await handler(event, data)
            if current == SELECT and select:
                return await handler(event, data)
            await event.answer("Завершите ввод или нажмите «Назад».", show_alert=True)
            return
        if current not in PARENTS:
            return await handler(event, data)
        if not private_admin(event, self.admin_id):
            await state.clear()
            return
        text = getattr(event, "text", None)
        if menu_navigation(text):
            await state.clear()
            data["raw_state"] = None
            return await handler(event, data)
        stored = (await state.get_data()).get(FORM_SCREEN)
        if not is_current_screen(event, stored, command=True):
            await state.clear()
            await event.answer("Форма устарела. Откройте её заново.")
            return
        if text == "/cancel":
            await finish(event, state, "Ввод отменён. Изменения не сохранены.", clean_cancel=True)
            return
        if current == SELECT:
            await event.answer("Выберите тариф кнопкой или отправьте /cancel.")
            return
        if not isinstance(text, str) or not text.strip():
            await retry(event, state, "❌ Отправьте текст.")
            return
        if text.startswith("/") and not (text == "/skip" and current.startswith("EditTariff:")):
            await retry(event, state, "❌ Команда не относится к этой форме.")
            return
        return await handler(event, data)
