"""
FSM состояния для бота
"""
from aiogram.fsm.state import State, StatesGroup

class AddTariff(StatesGroup):
    name = State()
    price = State()
    days = State()
    traffic = State()
    ip_limit = State()

class AddServer(StatesGroup):
    name = State()
    url = State()
    token = State()

class EditText(StatesGroup):
    waiting = State()

class AdminAddKey(StatesGroup):
    user_id = State()
    tariff_id = State()
    days = State()

class SendMessageToUser(StatesGroup):
    user_id = State()
    message_text = State()

class EnterPromocode(StatesGroup):
    waiting = State()

class EditTariff(StatesGroup):
    tariff_id = State()
    name = State()
    price = State()
    days = State()
    traffic = State()
    ip_limit = State()

class AdminDeposit(StatesGroup):
    amount = State()
    user_id = State()

class AdminWithdraw(StatesGroup):
    amount = State()
    user_id = State()

class AdminAddKeyForUser(StatesGroup):
    user_id = State()
    tariff_id = State()
    days = State()

class UserExtendKey(StatesGroup):
    waiting = State()

class CustomDeposit(StatesGroup):
    amount = State()
