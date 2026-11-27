"""Scope custom balance input to the actual active screen."""
from aiogram import BaseMiddleware
from states import CustomDeposit, EnterPromocode
from screen_navigation import MENU_COMMANDS, _key, _read

DEPOSIT_STATE = CustomDeposit.amount.state
PROMO_STATE = EnterPromocode.waiting.state
FORM_SCREEN = "form_screen_id"


def is_current_screen(message, screen_id, *, command=False):
    key = _key(message, command=command)
    if key is None or not isinstance(screen_id, int) or screen_id <= 0:
        return False
    row = _read(key)
    return row is not None and row[0] == screen_id


async def current_deposit_input(message, state):
    data = await state.get_data()
    return (await state.get_state() == DEPOSIT_STATE
            and is_current_screen(message, data.get(FORM_SCREEN), command=True))


def menu_navigation(text):
    if not isinstance(text, str):
        return False
    if text in MENU_COMMANDS:
        return True
    return text.startswith("/start ")


class UserFormNavigation(BaseMiddleware):
    """Cancel user input on top-level navigation; block stale deposit callbacks."""
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)
        current = await state.get_state()
        if current not in (DEPOSIT_STATE, PROMO_STATE):
            return await handler(event, data)
        if hasattr(event, "data") and hasattr(event, "message"):
            if current == DEPOSIT_STATE:
                stored = (await state.get_data()).get(FORM_SCREEN)
                if (event.data != "cancel_custom_deposit"
                        or getattr(event.message, "message_id", None) != stored
                        or not is_current_screen(event.message, stored)):
                    await event.answer("Сначала завершите или отмените ввод суммы.", show_alert=True)
                    return
        elif menu_navigation(getattr(event, "text", None)):
            await state.clear()
            # Aiogram's FSM middleware snapshots raw_state before routing.
            data["raw_state"] = None
        return await handler(event, data)
