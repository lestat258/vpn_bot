"""
Все клавиатуры для бота
"""
import sqlite3
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from datetime import datetime

DB_PATH = '/opt/vpn-bot/data.db'

def get_main_keyboard(telegram_id=None):
    """Главная клавиатура"""
    buttons = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📋 Купить ключ")],
        [KeyboardButton(text="🎁 Пригласить друга"), KeyboardButton(text="❓ Вопросы")],
        [KeyboardButton(text="📥 Установить VPN")],
    ]
    if telegram_id:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute('SELECT is_admin FROM users WHERE telegram_id = ?', (telegram_id,))
            row = c.fetchone()
            is_admin = row[0] == 1 if row else False
        except sqlite3.OperationalError:
            from database import get_setting
            admin_id = int(get_setting('admin_id') or 812021055)
            is_admin = (telegram_id == admin_id)
        conn.close()
        
        if is_admin:
            buttons.append([KeyboardButton(text="🔧 Админ-панель")])
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, persistent=True)

def get_device_keyboard():
    """Клавиатура выбора устройства"""
    buttons = [
        [InlineKeyboardButton(text="🤖 Android", callback_data="device_android")],
        [InlineKeyboardButton(text="🍏 iOS", callback_data="device_ios")],
        [InlineKeyboardButton(text="💻 Windows", callback_data="device_windows")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_tariffs_keyboard():
    """Клавиатура выбора тарифа"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days FROM tariffs WHERE is_active = 1 ORDER BY price_rub')
    tariffs = c.fetchall()
    conn.close()

    buttons = []
    for t in tariffs:
        buttons.append([InlineKeyboardButton(
            text=f"📦 {t[1]} — {t[2]:.0f} ₽ / {t[3]} дн.",
            callback_data=f"tariff_{t[0]}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():
    """Админ-клавиатура"""
    buttons = [
        [InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="admin_settings_menu")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="🗄 Бэкап", callback_data="admin_backup_menu")],
        [InlineKeyboardButton(text="📢 Рассылка промокодов", callback_data="admin_broadcast_promo")],
        [InlineKeyboardButton(text="🛡️ Защита от спама", callback_data="admin_rate_limit")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_keys_keyboard(telegram_id):
    """Клавиатура списка ключей"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT s.id, s.xui_email, s.end_date, t.name
                 FROM subscriptions s
                 JOIN tariffs t ON t.id = s.tariff_id
                 WHERE s.telegram_id = ? AND s.is_active = 1
                 ORDER BY s.end_date DESC''', (telegram_id,))
    subs = c.fetchall()
    conn.close()

    buttons = []
    for sub in subs:
        sub_id, email, end_date, tariff_name = sub
        days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
        status = "✅" if days_left > 0 else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {tariff_name} (до {end_date[:10]})",
            callback_data=f"key_{sub_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_user_list_keyboard():
    """Клавиатура списка пользователей для админа"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id, first_name FROM users ORDER BY created_at DESC LIMIT 20')
    users = c.fetchall()
    conn.close()

    buttons = []
    for user in users:
        telegram_id, first_name = user
        display_name = first_name or str(telegram_id)
        buttons.append([InlineKeyboardButton(
            text=f"👤 {display_name} ({telegram_id})",
            callback_data=f"user_{telegram_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_user_action_keyboard(telegram_id):
    """Клавиатура карточки пользователя"""
    buttons = [
        [InlineKeyboardButton(text="🔑 Ключи", callback_data=f"user_list_keys_{telegram_id}")],
        [InlineKeyboardButton(text="💬 Написать", callback_data=f"user_send_message_{telegram_id}")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data=f"user_balance_{telegram_id}")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"user_block_{telegram_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirmation_keyboard(callback_data_yes, callback_data_no):
    """Клавиатура подтверждения"""
    buttons = [
        [InlineKeyboardButton(text="✅ Да", callback_data=callback_data_yes)],
        [InlineKeyboardButton(text="❌ Нет", callback_data=callback_data_no)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard(callback_data):
    """Клавиатура с кнопкой назад"""
    buttons = [[InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_profile_keyboard(has_promocodes=True):
    """Клавиатура профиля"""
    kb_buttons = [
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")],
        [InlineKeyboardButton(text="💰 Баланс и авто-продление", callback_data="balance_auto_renew")],
        [InlineKeyboardButton(text="🎫 Промокоды и достижения", callback_data="promo_achievements")],
    ]
    kb_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb_buttons)

def get_user_keys_admin_keyboard(user_id):
    """Клавиатура списка ключей для админа"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, t.name, s.end_date, s.is_active
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.telegram_id = ?
        ORDER BY s.end_date DESC
    ''', (user_id,))
    subs = c.fetchall()
    conn.close()

    buttons = []
    for s in subs:
        sub_id, tariff_name, end_date, is_active = s
        status = "✅" if is_active else "❌"
        date_str = end_date[:10] if end_date else "∞"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {tariff_name} (до {date_str})",
            callback_data=f"admin_key_detail_{sub_id}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить ключ", callback_data=f"user_add_key_{user_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_back_to_{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_key_detail_keyboard(sub_id, user_id):
    """Клавиатура деталей ключа"""
    buttons = [
        [InlineKeyboardButton(text="🔄 Продлить", callback_data=f"admin_extend_key_{sub_id}")],
        [InlineKeyboardButton(text="🗑 Удалить ключ", callback_data=f"admin_delete_key_confirm_{sub_id}|{user_id}")],
        [InlineKeyboardButton(text="🔙 Назад к ключам", callback_data=f"user_list_keys_{user_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_auto_renew_keyboard(subscription_id, is_enabled):
    """Клавиатура для управления авто-продлением"""
    status_text = "✅ Включено" if is_enabled else "❌ Выключено"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔄 Авто-продление: {status_text}",
            callback_data=f"toggle_auto_renew_{subscription_id}"
        )]
    ])
