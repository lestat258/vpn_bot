"""
Административные хендлеры бота
"""
from runtime_config import BACKUP_DIR, DB_PATH
from balance_ledger import admin_adjust, rubles, kopecks
from admin_form_navigation import begin as form_begin, step as form_step, retry as form_retry, finish as form_finish
from admin_form_navigation import parent_for, back_keyboard, CONFIRM_TOKEN, callback_is_current, private_admin, FORM_SCREEN
from screen_navigation import render_screen
from contextlib import closing
from html import escape
import math
from urllib.parse import urlsplit
from encryption import encrypt
from aiogram.exceptions import TelegramAPIError
import logging
import sqlite3
import json
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

from database import get_setting, set_setting
from keyboards import (
    get_admin_keyboard,
    get_user_list_keyboard,
    get_user_action_keyboard,
    get_user_keys_admin_keyboard,
    get_key_detail_keyboard,
    get_confirmation_keyboard,
    get_back_keyboard,
    get_main_keyboard
)
from states import (
    AddTariff,
    AddServer,
    EditText,
    SendMessageToUser,
    AdminAddKey,
    AdminExtendKey,
    EditTariff,
    AdminDeposit,
    AdminWithdraw
)
from services import vpn_service
from utils import (
    generate_short_email,
    get_user_by_telegram_id,
    get_tariff_by_id,
    create_vpn_client,
    get_active_subscriptions
)
from xui_client import XUIClient
from server_manager import server_manager
from backup_manager import backup_manager

router = Router()

ADMIN_ID = int(get_setting('admin_id') or 812021055)

# Временное хранилище для продления
extend_temp = {}

# ============================================================
# АДМИН: ПОЛЬЗОВАТЕЛИ
# ============================================================

@router.callback_query(F.data == "admin_users")
async def admin_users(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    await render_screen(callback.message,
        "👥 <b>Выберите пользователя:</b>",
        reply_markup=get_user_list_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(lambda c: c.data.startswith("user_") and not any(c.data.startswith(x) for x in ["user_back_to_", "user_list_", "user_add_", "user_send_", "user_balance_", "user_deposit_", "user_withdraw_", "user_delete_", "user_block_"]))
async def admin_user_card(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT first_name, username, created_at, is_blocked, balance FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE telegram_id = ? AND is_active = 1', (telegram_id,))
    keys_count = c.fetchone()[0]
    conn.close()

    if not user:
        await callback.message.answer("❌ Пользователь не найден")
        await callback.answer()
        return

    first_name, username, created_at, is_blocked, balance = user

    text = (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"📛 Имя: {escape(first_name or 'Не указано')}\n"
        f"🆔 ID: {telegram_id}\n"
        f"📝 Username: @{escape(username or 'Нет')}\n"
        f"📅 Регистрация: {created_at[:10] if created_at else 'Неизвестно'}\n"
        f"🔑 Ключей: {keys_count}\n"
        f"💰 Баланс: {balance or 0:.2f} ₽\n"
        f"🚫 Статус: {'🔴 Заблокирован' if is_blocked else '🟢 Активен'}"
    )

    await render_screen(callback.message,
        text,
        reply_markup=get_user_action_keyboard(telegram_id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("user_back_to_"))
async def admin_back_to_user_card(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_back_to_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT first_name, username, created_at, is_blocked, balance FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE telegram_id = ? AND is_active = 1', (telegram_id,))
    keys_count = c.fetchone()[0]
    conn.close()

    if not user:
        await callback.message.answer("❌ Пользователь не найден")
        await callback.answer()
        return

    first_name, username, created_at, is_blocked, balance = user

    text = (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"📛 Имя: {escape(first_name or 'Не указано')}\n"
        f"🆔 ID: {telegram_id}\n"
        f"📝 Username: @{escape(username or 'Нет')}\n"
        f"📅 Регистрация: {created_at[:10] if created_at else 'Неизвестно'}\n"
        f"🔑 Ключей: {keys_count}\n"
        f"💰 Баланс: {balance or 0:.2f} ₽\n"
        f"🚫 Статус: {'🔴 Заблокирован' if is_blocked else '🟢 Активен'}"
    )

    await render_screen(callback.message,
        text,
        reply_markup=get_user_action_keyboard(telegram_id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("user_list_keys_"))
async def admin_list_keys_for_user(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_list_keys_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, t.name, s.end_date, s.is_active
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.telegram_id = ?
        ORDER BY s.end_date DESC
    ''', (telegram_id,))
    subs = c.fetchall()
    conn.close()

    buttons = []

    if not subs:
        await render_screen(callback.message,
            "🔑 <b>У пользователя нет ключей</b>",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="➕ Добавить ключ", callback_data=f"user_add_key_{telegram_id}")],
                [types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_back_to_{telegram_id}")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    for s in subs:
        sub_id, tariff_name, end_date, is_active = s
        status = "✅" if is_active else "❌"
        date_str = end_date[:10] if end_date else "∞"
        buttons.append([types.InlineKeyboardButton(
            text=f"{status} {tariff_name} (до {date_str})",
            callback_data=f"admin_key_detail_{sub_id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="➕ Добавить ключ", callback_data=f"user_add_key_{telegram_id}")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_back_to_{telegram_id}")])

    await render_screen(callback.message,
        f"🔑 <b>Ключи пользователя</b>\n\nВыберите ключ для управления или добавьте новый:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_key_detail_"))
async def admin_key_detail(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    sub_id = int(callback.data.replace("admin_key_detail_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, s.xui_email, s.end_date, t.name, s.xui_client_uid,
               s.start_date, s.traffic_used_gb, s.telegram_id
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.id = ?
    ''', (sub_id,))
    sub = c.fetchone()
    conn.close()

    if not sub:
        await callback.message.answer("❌ Ключ не найден")
        await callback.answer()
        return

    sub_id, email, end_date, tariff_name, client_uid, start_date, traffic_used, user_id = sub
    days_left = (datetime.fromisoformat(end_date) - datetime.now()).days

    text = (
        f"🔑 <b>Карточка ключа</b>\n\n"
        f"📦 Тариф: {escape(tariff_name)}\n"
        f"📧 Email: <code>{escape(email or '')}</code>\n"
        f"📅 Создан: {start_date[:10] if start_date else 'Неизвестно'}\n"
        f"📅 Истекает: {end_date[:10]}\n"
        f"⏳ Осталось: {days_left} дн.\n"
        f"📊 Трафик: {traffic_used or 0:.2f} ГБ\n"
    )

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Продлить", callback_data=f"admin_extend_key_{sub_id}")],
        [types.InlineKeyboardButton(text="🗑 Удалить ключ", callback_data=f"admin_delete_key_confirm_{sub_id}|{user_id}")],
        [types.InlineKeyboardButton(text="🔙 Назад к ключам", callback_data=f"user_list_keys_{user_id}")]
    ])

    await render_screen(callback.message,
        text,
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

def admin_issue_tariff(tariff_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute("""SELECT id,name,price_rub,duration_days,traffic_gb,ip_limit FROM tariffs
                             WHERE id=? AND is_active=1 AND duration_days>0""",(tariff_id,)).fetchone()


def admin_issue_owner(user_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return bool(db.execute("SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0",(user_id,)).fetchone())


@router.callback_query(F.data.startswith("user_add_key_"))
async def admin_add_key_for_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    try:
        user_id=int(callback.data.removeprefix("user_add_key_"))
        if user_id<=0 or not admin_issue_owner(user_id):
            raise ValueError
    except ValueError:
        await callback.answer("Пользователь недоступен",show_alert=True)
        return
    with closing(sqlite3.connect(DB_PATH)) as db:
        tariffs=db.execute("SELECT id,name,price_rub,duration_days FROM tariffs WHERE is_active=1 AND duration_days>0").fetchall()
    if not tariffs:
        await callback.answer("Нет доступных тарифов",show_alert=True)
        return
    kb=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"{t[1]} — {t[2]}₽ / {t[3]} дн.",
            callback_data=f"admin_tariff_select_{t[0]}|{user_id}")] for t in tariffs])
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,AdminAddKey.tariff_id,
        f"🔑 Выдача нового ключа пользователю {user_id}\nВыберите тариф:",fields={"user_id":user_id},keyboard=kb)


@router.callback_query(F.data.startswith("admin_tariff_select_"))
async def admin_add_key_tariff(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    data=await state.get_data()
    try:
        raw=callback.data.removeprefix("admin_tariff_select_").split("|")
        tariff_id,user_id=map(int,raw)
        if user_id!=data.get("user_id") or not admin_issue_owner(user_id):
            raise ValueError
    except ValueError:
        await callback.answer("Выберите пользователя заново",show_alert=True)
        return
    tariff=admin_issue_tariff(tariff_id)
    if not tariff:
        await callback.answer("Тариф недоступен",show_alert=True)
        return
    await form_begin(callback,state,AdminAddKey.days,
        f"📦 Тариф: {tariff[1]}\n📅 Дней: {tariff[3]}\n\nВведите срок в днях или /skip для стандартного:",
        fields={"user_id":user_id,"tariff_id":tariff_id,"tariff":tariff})


@router.message(AdminAddKey.days)
async def admin_add_key_days(message: types.Message, state: FSMContext):
    data=await state.get_data()
    tariff=admin_issue_tariff(data["tariff_id"])
    if not tariff or tuple(data["tariff"])!=tariff or not admin_issue_owner(data["user_id"]):
        await form_finish(message,state,"Пользователь или тариф изменились. Откройте форму заново.")
        return
    try:
        days=tariff[3] if message.text=="/skip" else int(message.text)
        if not 1<=days<=36500:
            raise ValueError
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите целое число от 1 до 36500 или /skip.")
        return
    await form_step(message,state,AdminAddKey.confirm,
        f"Подтвердите выдачу нового ключа\n\nПользователь: {data['user_id']}\n"
        f"Тариф: {tariff[1]}\nСрок: {days} дн.\nТрафик: {tariff[4] or '∞'} ГБ\n"
        f"Лимит IP: {tariff[5]}\n\nОплата не списывается.",days=days)


@router.callback_query(F.data.startswith("user_send_message_"))
async def send_message_from_user_card(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    try:
        telegram_id=int(callback.data.removeprefix("user_send_message_"))
        if telegram_id<=0:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный пользователь",show_alert=True)
        return
    user=get_user_by_telegram_id(telegram_id)
    if not user:
        await callback.answer("Пользователь не найден",show_alert=True)
        return
    user_name=user[2] or user[3] or str(telegram_id)
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,SendMessageToUser.message_text,
        f"💬 Отправка сообщения пользователю\n\n👤 Пользователь: {user_name}\n"
        f"🆔 ID: {telegram_id}\n\nВведите текст сообщения (поддерживается HTML). "
        "После ввода сообщение будет отправлено сразу.",
        fields={"user_id":telegram_id,"user_name":user_name})


@router.message(SendMessageToUser.message_text)
async def send_message_process(message: types.Message, state: FSMContext):
    data=await state.get_data()
    user_id=data.get("user_id")
    if type(user_id) is not int or user_id<=0 or not get_user_by_telegram_id(user_id):
        await form_finish(message,state,"Пользователь не найден. Сообщение не отправлено.")
        return
    if len(message.text.encode("utf-16-le"))//2>3900:
        await form_retry(message,state,"❌ Сообщение слишком длинное (до 3900 символов UTF-16).")
        return
    # Never retry an ambiguous Telegram send; preserve the recipient-specific back button.
    try:
        await message.bot.send_message(chat_id=user_id,
            text=f"📨 <b>Сообщение от администратора</b>\n\n{message.text}",parse_mode="HTML")
    except Exception as exc:
        logging.warning("Admin message delivery requires review: %s",type(exc).__name__)
        await form_finish(message,state,"Не удалось подтвердить отправку. Проверьте доставку перед повтором.")
        return
    await form_finish(message,state,f"✅ Сообщение отправлено пользователю {data.get('user_name',user_id)}.")


def admin_balance_keyboard(telegram_id, *, review=False):
    buttons = []
    if not review:
        buttons.extend([
            [types.InlineKeyboardButton(text="💰 Пополнить", callback_data=f"user_deposit_{telegram_id}")],
            [types.InlineKeyboardButton(text="💸 Списать", callback_data=f"user_withdraw_{telegram_id}")]
        ])
    else:
        buttons.append([types.InlineKeyboardButton(text="🔍 Проверить баланс", callback_data=f"user_balance_{telegram_id}")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_back_to_{telegram_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("user_balance_"))
async def admin_user_balance(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_balance_", ""))
    await render_admin_balance(callback, telegram_id)


async def render_admin_balance(callback, telegram_id, *, canceled=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT balance FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()
    conn.close()

    if not user:
        await callback.message.answer("❌ Пользователь не найден")
        await callback.answer()
        return

    balance = user[0] or 0

    kb = admin_balance_keyboard(telegram_id)

    await callback.message.edit_text(
        ("❌ Операция отменена\n\n" if canceled else "") +
        f"💰 <b>Баланс пользователя</b>\n\n"
        f"Текущий баланс: <b>{balance:.2f} ₽</b>\n\n"
        f"Выберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

def admin_balance_cancel_keyboard(telegram_id, mode):
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="🔙 Назад",
            callback_data=f"admin_balance_cancel_{mode}_{telegram_id}")
    ]])


@router.callback_query(F.data.startswith("admin_balance_cancel_"))
async def admin_balance_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    try:
        mode, raw_id = callback.data.removeprefix("admin_balance_cancel_").split("_")
        owner = int(raw_id)
        expected = {"deposit": AdminDeposit.amount.state, "withdraw": AdminWithdraw.amount.state}[mode]
        data = await state.get_data()
        if owner <= 0 or await state.get_state() != expected or data.get("user_id") != owner:
            raise ValueError("Stale input")
    except (ValueError, KeyError, TypeError):
        await callback.answer("Этот ввод уже завершён. Откройте баланс заново.", show_alert=True)
        return
    await state.clear()
    await render_admin_balance(callback, owner, canceled=True)


@router.callback_query(F.data.startswith("user_deposit_"))
async def admin_deposit_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_deposit_", ""))
    await state.update_data(user_id=telegram_id)
    await state.set_state(AdminDeposit.amount)

    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\n"
        "Введите сумму пополнения (в рублях) или /cancel для отмены:",
        reply_markup=admin_balance_cancel_keyboard(telegram_id, "deposit"),
        parse_mode="HTML"
    )
    await callback.answer()

async def admin_balance_operation(message, state, *, withdraw):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Доступ запрещён")
        return

    owner = None
    try:
        data = await state.get_data()
        owner = int(data["user_id"])
        if owner <= 0:
            owner = None
            raise ValueError("Invalid user")
    except (ValueError, KeyError, TypeError):
        await state.clear()
        await message.answer("Пользователь не выбран. Откройте его карточку заново.")
        return

    raw = (message.text or "").strip()
    review = False
    if raw == "/cancel":
        text = "❌ Операция отменена"
    else:
        try:
            amount = raw.replace(",", ".")
            n = kopecks(amount, positive=True, maximum=10000000)
            balance, changed = await asyncio.to_thread(
                admin_adjust, DB_PATH,
                f"admin:{message.chat.id}:{message.message_id}",
                owner, message.from_user.id, amount, withdraw=withdraw)
            label = "Списание" if withdraw else "Пополнение"
            text = (f"✅ {label}: {rubles(n):.2f} ₽\n"
                    f"Баланс после операции: {rubles(balance):.2f} ₽")
            if not changed:
                text = "Операция уже учтена.\n" + text
        except (ValueError, KeyError, TypeError):
            text = "Операция недоступна: проверьте сумму, остаток и наличие блокировки средств."
        except Exception:
            logging.error("Administrative balance operation requires review")
            text = "Результат операции не подтверждён. Проверьте журнал перед повтором."
            review = True
    # Return to the menu, never leave an input state active under navigation.
    await state.clear()
    await message.answer(text, reply_markup=admin_balance_keyboard(owner, review=review))


@router.message(AdminDeposit.amount)
async def admin_deposit_process(message: types.Message, state: FSMContext):
    await admin_balance_operation(message, state, withdraw=False)



@router.callback_query(F.data.startswith("user_withdraw_"))
async def admin_withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_withdraw_", ""))
    await state.update_data(user_id=telegram_id)
    await state.set_state(AdminWithdraw.amount)

    await callback.message.edit_text(
        "💸 <b>Списание баланса</b>\n\n"
        "Введите сумму списания (в рублях) или /cancel для отмены:",
        reply_markup=admin_balance_cancel_keyboard(telegram_id, "withdraw"),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminWithdraw.amount)
async def admin_withdraw_process(message: types.Message, state: FSMContext):
    await admin_balance_operation(message, state, withdraw=True)



@router.callback_query(F.data.startswith("user_block_"))
async def admin_block_user(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id = int(callback.data.replace("user_block_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT is_blocked FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()

    if not user:
        await callback.answer("❌ Пользователь не найден")
        return

    new_status = 0 if user[0] else 1
    c.execute('UPDATE users SET is_blocked = ? WHERE telegram_id = ?', (new_status, telegram_id))
    conn.commit()
    conn.close()

    status_text = "заблокирован" if new_status else "разблокирован"
    await callback.answer(f"✅ Пользователь {status_text}", show_alert=True)

    await admin_back_to_user_card(callback)

# ============================================================
# АДМИН: ТАРИФЫ
# ============================================================

@router.callback_query(F.data == "admin_tariffs")
async def admin_tariffs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tariffs = conn.execute('SELECT id, name, price_rub, duration_days, traffic_gb, ip_limit, is_active FROM tariffs').fetchall()
    text = "📋 <b>Тарифы:</b>\n\n"
    for t in tariffs:
        status = "✅" if t[6] else "❌"
        tr = f"{t[4]:.0f} ГБ" if t[4] else "∞"
        text += f"{status} <b>{escape(t[1])}</b> — {t[2]:.2f}₽ / {t[3]}дн / {tr} / IP:{t[5]}\n"
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Добавить тариф", callback_data="admin_add_tariff")],
        [types.InlineKeyboardButton(text="✏️ Редактировать тариф", callback_data="admin_edit_tariff")],
        [types.InlineKeyboardButton(text="🗑 Удалить тариф", callback_data="admin_delete_tariff")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])
    await render_screen(callback.message,text,reply_markup=kb,parse_mode="HTML")
    await callback.answer()


def tariff_number(text, *, integer=False, minimum=0):
    value = int(text) if integer else float(text)
    if not math.isfinite(value) or value < minimum:
        raise ValueError("Invalid tariff value")
    return value


@router.callback_query(F.data == "admin_add_tariff")
async def admin_add_tariff_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,AddTariff.name,"Название тарифа:")


@router.message(AddTariff.name)
async def tariff_name(message: types.Message, state: FSMContext):
    await form_step(message,state,AddTariff.price,"Цена (₽):",name=message.text.strip())


@router.message(AddTariff.price)
async def tariff_price(message: types.Message, state: FSMContext):
    try:
        price=tariff_number(message.text)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите неотрицательное число.")
        return
    await form_step(message,state,AddTariff.days,"Дней:",price=price)


@router.message(AddTariff.days)
async def tariff_days(message: types.Message, state: FSMContext):
    try:
        days=tariff_number(message.text,integer=True,minimum=1)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите целое число больше нуля.")
        return
    await form_step(message,state,AddTariff.traffic,"Трафик (ГБ, 0=∞):",days=days)


@router.message(AddTariff.traffic)
async def tariff_traffic(message: types.Message, state: FSMContext):
    try:
        traffic=tariff_number(message.text)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите неотрицательное число.")
        return
    await form_step(message,state,AddTariff.ip_limit,"Лимит IP:",traffic=traffic)


@router.message(AddTariff.ip_limit)
async def tariff_ip(message: types.Message, state: FSMContext):
    try:
        ip_limit=tariff_number(message.text,integer=True)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите неотрицательное целое число.")
        return
    data=await state.get_data()
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn,conn:
            conn.execute('INSERT INTO tariffs (name, price_rub, duration_days, traffic_gb, ip_limit) VALUES (?,?,?,?,?)',
                (data['name'],data['price'],data['days'],data['traffic'],ip_limit))
    except (sqlite3.Error,OverflowError):
        logging.exception("Admin tariff insert failed")
        await form_finish(message,state,"Не удалось подтвердить создание тарифа. Проверьте список перед повтором.")
        return
    await form_finish(message,state,f"✅ Тариф «{data['name']}» создан!")


@router.callback_query(F.data == "admin_edit_tariff")
async def admin_edit_tariff_select(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tariffs=conn.execute('SELECT id, name, price_rub, duration_days, is_active FROM tariffs').fetchall()
    if not tariffs:
        await callback.answer("Нет тарифов для редактирования",show_alert=True)
        return
    kb=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"{'✅' if t[4] else '❌'} {t[1]} — {t[2]}₽ / {t[3]} дн.",
            callback_data=f"admin_edit_tariff_select_{t[0]}")] for t in tariffs
    ]+[[types.InlineKeyboardButton(text="🔙 Назад",callback_data="admin_tariffs")]])
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,EditTariff.tariff_id,"✏️ Выберите тариф для редактирования:",keyboard=kb)


@router.callback_query(F.data.startswith("admin_edit_tariff_select_"))
async def admin_edit_tariff_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    try:
        tariff_id=int(callback.data.removeprefix("admin_edit_tariff_select_"))
    except ValueError:
        await callback.answer("Некорректный тариф",show_alert=True)
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tariff=conn.execute('SELECT id, name, price_rub, duration_days, traffic_gb, ip_limit, is_active FROM tariffs WHERE id=?',
                            (tariff_id,)).fetchone()
    if not tariff:
        await callback.answer("Тариф не найден. Вернитесь назад.",show_alert=True)
        return
    text=(f"✏️ Редактирование тарифа\n\n"
          f"📛 Название: {tariff[1]}\n"
          f"💰 Цена: {tariff[2]} ₽\n"
          f"📅 Дней: {tariff[3]}\n"
          f"📦 Трафик: {tariff[4] or '∞'} ГБ\n"
          f"📱 IP: {tariff[5]}\n"
          f"📊 Статус: {'✅ Активен' if tariff[6] else '❌ Неактивен'}\n\n"
          "Введите новое название или /skip для пропуска:")
    await form_begin(callback,state,EditTariff.name,text,fields={"tariff_id":tariff_id,"tariff":tariff})


async def edit_tariff_field(message,state,target,prompt,field,index,*,integer=False,minimum=0):
    data=await state.get_data()
    try:
        value=data['tariff'][index] if message.text=="/skip" else tariff_number(message.text,integer=integer,minimum=minimum)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите допустимое число или /skip.")
        return
    await form_step(message,state,target,prompt,**{field:value})


@router.message(EditTariff.name)
async def edit_tariff_name(message: types.Message, state: FSMContext):
    data=await state.get_data()
    name=data['tariff'][1] if message.text=="/skip" else message.text.strip()
    await form_step(message,state,EditTariff.price,"💰 Введите новую цену (или /skip):",name=name)


@router.message(EditTariff.price)
async def edit_tariff_price(message: types.Message, state: FSMContext):
    await edit_tariff_field(message,state,EditTariff.days,"📅 Введите новое количество дней (или /skip):",
        "price",2)


@router.message(EditTariff.days)
async def edit_tariff_days(message: types.Message, state: FSMContext):
    await edit_tariff_field(message,state,EditTariff.traffic,"📦 Введите новый трафик (ГБ, 0 = ∞) (или /skip):",
        "days",3,integer=True,minimum=1)


@router.message(EditTariff.traffic)
async def edit_tariff_traffic(message: types.Message, state: FSMContext):
    await edit_tariff_field(message,state,EditTariff.ip_limit,"📱 Введите новый лимит IP (или /skip):",
        "traffic",4)


@router.message(EditTariff.ip_limit)
async def edit_tariff_ip_limit(message: types.Message, state: FSMContext):
    data=await state.get_data()
    old=data['tariff']
    try:
        ip_limit=old[5] if message.text=="/skip" else tariff_number(message.text,integer=True)
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите допустимое целое число или /skip.")
        return
    name,price,days,traffic=(data.get(k,old[i]) for k,i in (("name",1),("price",2),("days",3),("traffic",4)))
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn,conn:
            # Do not overwrite a concurrent web-admin change or active status.
            changed=conn.execute("""UPDATE tariffs SET name=?, price_rub=?, duration_days=?, traffic_gb=?, ip_limit=?
                WHERE id=? AND name IS ? AND price_rub IS ? AND duration_days IS ? AND traffic_gb IS ?
                AND ip_limit IS ? AND is_active IS ?""",
                (name,price,days,traffic,ip_limit,*old)).rowcount
    except (sqlite3.Error,OverflowError):
        logging.exception("Admin tariff update failed")
        await form_finish(message,state,"Не удалось подтвердить обновление тарифа. Проверьте список.")
        return
    if changed!=1:
        await form_finish(message,state,"Тариф изменён или удалён в другом сеансе. Откройте его заново.")
        return
    await form_finish(message,state,
        f"✅ Тариф обновлён!\n\n📛 Название: {name}\n💰 Цена: {price} ₽\n"
        f"📅 Дней: {days}\n📦 Трафик: {traffic or '∞'} ГБ\n📱 IP: {ip_limit}")


@router.callback_query(F.data == "admin_delete_tariff")
async def admin_delete_tariff_start(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days, is_active FROM tariffs')
    tariffs = c.fetchall()
    conn.close()

    if not tariffs:
        await callback.message.answer("📭 Нет тарифов для удаления")
        await callback.answer()
        return

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=f"🗑 {'✅' if t[4] else '❌'} {t[1]} — {t[2]}₽ / {t[3]} дн.",
            callback_data=f"admin_delete_tariff_confirm_{t[0]}"
        )] for t in tariffs
    ] + [[types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_tariffs")]])

    await callback.message.edit_text(
        "🗑 <b>Выберите тариф для удаления:</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_delete_tariff_confirm_"))
async def admin_delete_tariff_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    tariff_id = int(callback.data.replace("admin_delete_tariff_confirm_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name FROM tariffs WHERE id = ?', (tariff_id,))
    tariff = c.fetchone()
    conn.close()

    if not tariff:
        await callback.message.answer("❌ Тариф не найден")
        await callback.answer()
        return

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_delete_tariff_execute_{tariff_id}")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_tariffs")]
    ])

    await callback.message.edit_text(
        f"⚠️ <b>Удаление тарифа</b>\n\n"
        f"📦 Тариф: {tariff[1]}\n\n"
        f"<b>ВНИМАНИЕ!</b> Удаление тарифа не удалит существующие подписки.\n\n"
        f"Вы уверены?",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_delete_tariff_execute_"))
async def admin_delete_tariff_execute(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    tariff_id = int(callback.data.replace("admin_delete_tariff_execute_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM tariffs WHERE id = ?', (tariff_id,))
    conn.commit()
    conn.close()

    await callback.answer("✅ Тариф удалён", show_alert=True)
    await admin_tariffs(callback)

# ============================================================
# АДМИН: ПРОДЛЕНИЕ КЛЮЧА (ИСПРАВЛЕНО: ИСПОЛЬЗУЕТ vpn_service)
# ============================================================

def admin_renew_target(sub_id):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute("""SELECT s.id,s.telegram_id,s.tariff_id,s.end_date,s.server_id,s.xui_client_uid,s.xui_subid
            FROM subscriptions s JOIN users u ON u.telegram_id=s.telegram_id JOIN servers v ON v.id=s.server_id
            WHERE s.id=? AND s.is_active=1 AND s.managed_by_vpn4us=1 AND u.is_blocked=0 AND v.is_active=1""",
            (sub_id,)).fetchone()


@router.callback_query(F.data.startswith("admin_extend_key_"))
async def admin_extend_key_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    try:
        sub_id=int(callback.data.removeprefix("admin_extend_key_"))
        if sub_id<=0:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный ключ",show_alert=True)
        return
    target=admin_renew_target(sub_id)
    if not target:
        await callback.answer("Ключ недоступен для продления",show_alert=True)
        return
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,AdminExtendKey.days,
        f"📅 Продление ключа №{sub_id}\nПользователь: {target[1]}\nСрок до: {target[3][:10]}\n\n"
        "Введите количество дней (или 0 для отмены):",
        fields={"subscription_id":sub_id,"target":target})


@router.message(AdminExtendKey.days)
async def admin_extend_key_process(message: types.Message, state: FSMContext):
    data=await state.get_data()
    try:
        days=int(message.text)
        if not 0<=days<=36500:
            raise ValueError
    except (ValueError,TypeError,OverflowError):
        await form_retry(message,state,"❌ Введите целое число от 1 до 36500.")
        return
    if days==0:
        await form_finish(message,state,"Продление отменено.")
        return
    target=admin_renew_target(data["subscription_id"])
    if not target or tuple(data["target"])!=target:
        await form_finish(message,state,"Ключ изменился. Откройте карточку заново.")
        return
    await form_step(message,state,AdminExtendKey.confirm,
        f"Подтвердите продление ключа №{target[0]}\n\nПользователь: {target[1]}\n"
        f"Добавить: {days} дн.\n\nОплата не списывается.",days=days)


@router.callback_query(F.data.startswith("admin_key_apply_"))
async def admin_apply_key_operation(callback: types.CallbackQuery,state: FSMContext):
    current=await state.get_state()
    data=await state.get_data()
    if (current not in (AdminAddKey.confirm.state,AdminExtendKey.confirm.state)
            or not private_admin(callback,ADMIN_ID) or not callback_is_current(callback)
            or data.get(FORM_SCREEN)!=getattr(callback.message,"message_id",None)
            or callback.data!=data.get(CONFIRM_TOKEN)):
        await callback.answer("Подтверждение устарело",show_alert=True)
        return
    parent=parent_for(current,data)
    # One confirmation, one service attempt. Clear before any external mutation.
    await state.clear()
    await callback.answer()
    days=data["days"]
    if current==AdminAddKey.confirm.state:
        user_id=data["user_id"]
        tariff=admin_issue_tariff(data["tariff_id"])
        if not tariff or tuple(data["tariff"])!=tariff or not admin_issue_owner(user_id):
            result_text="Пользователь или тариф изменились. Откройте форму заново."
        else:
            try:
                result=await vpn_service.create_subscription(telegram_id=user_id,tariff_id=tariff[0],days=days,
                    traffic_gb=tariff[4],ip_limit=tariff[5],allow_additional_key=True)
                if result.get("success"):
                    date=result["end_date"].strftime("%d.%m.%Y")
                    result_text=f"✅ Ключ создан!\nПользователь: {user_id}\nТариф: {tariff[1]}\nДо: {date}\n\n{result['link']}"
                    try:
                        await callback.bot.send_message(chat_id=user_id,
                            text=f"🎉 <b>Администратор выдал вам ключ!</b>\n\n📦 Тариф: {escape(tariff[1])}\n"
                                 f"📅 Действует до: {date}\n\n<code>{escape(result['link'])}</code>",
                            parse_mode="HTML")
                    except Exception:
                        result_text+="\nНе удалось подтвердить доставку уведомления."
                else:
                    result_text="Выдача не подтверждена. Проверьте ключи и журнал выдачи перед повтором."
            except Exception as exc:
                logging.warning("Manual key issuance requires review: %s",type(exc).__name__)
                result_text="Выдача не подтверждена. Проверьте ключи и журнал выдачи перед повтором."
    else:
        target=admin_renew_target(data["subscription_id"])
        if not target or tuple(data["target"])!=target:
            result_text="Ключ изменился. Откройте карточку заново."
        else:
            try:
                result=await vpn_service.extend_subscription(subscription_id=target[0],days_to_add=days,tariff_id=target[2])
                result_text=(f"✅ Ключ продлён на {days} дн.\nНовая дата: {result['new_end_date'].strftime('%d.%m.%Y')}"
                    if result.get("success") else "Продление не подтверждено. Проверьте ключ и панель перед повтором.")
            except Exception as exc:
                logging.warning("Manual renewal requires review: %s",type(exc).__name__)
                result_text="Продление не подтверждено. Проверьте ключ и панель перед повтором."
    await render_screen(callback.message,result_text,reply_markup=back_keyboard(parent))


async def admin_list_keys_for_user_prepared(message, user_id):
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

    if not subs:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Добавить ключ", callback_data=f"user_add_key_{user_id}")],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_back_to_{user_id}")]
        ])
        await message.answer(
            "🔑 <b>У пользователя нет ключей</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    await message.answer(
        f"🔑 <b>Ключи пользователя</b>\n\nВыберите ключ для управления:",
        reply_markup=get_user_keys_admin_keyboard(user_id),
        parse_mode="HTML"
    )

# ============================================================
# АДМИН: УДАЛЕНИЕ КЛЮЧА
# ============================================================

@router.callback_query(F.data.startswith("admin_delete_key_confirm_"))
async def admin_delete_key_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    await callback.message.answer(
        "⚠️ Удаление через Telegram-админку временно отключено. "
        "Сначала требуется проверка принадлежности клиента vpn4us и точного "
        "сервера/inbound, чтобы не затронуть пользователей другой системы."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_delete_execute_"))
async def admin_delete_key_execute(callback: types.CallbackQuery):
    # Historic confirmation callbacks must remain fail-closed.
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    await callback.answer(
        "Удаление отключено до внедрения проверки принадлежности клиента.",
        show_alert=True,
    )


# ============================================================
# АДМИН: СЕРВЕРЫ
# ============================================================

@router.callback_query(F.data == "admin_servers_menu")
async def admin_servers_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, url, api_token, is_active, status, online_count FROM servers')
    servers = c.fetchall()
    conn.close()

    if not servers:
        text = "📭 <b>Серверы не добавлены</b>"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_add_server")],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
        ])
        await render_screen(callback.message,text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    text = "🖥 <b>Список серверов:</b>\n\n"
    buttons = []

    for server in servers:
        server_id, name, url, api_token, is_active, status, online_count = server

        if is_active == 0:
            status_emoji = "⛔"
            status_text = "❌ Отключён"
        elif status == 'online':
            status_emoji = "🟢"
            status_text = f"✅ Работает ({online_count or 0} клиентов)"
        elif status == 'timeout':
            status_emoji = "🟡"
            status_text = "⏱️ Не отвечает"
        else:
            status_emoji = "🔴"
            status_text = "❌ Недоступен"

        text += f"{status_emoji} <b>{escape(name)}</b>\n"
        text += f"   Статус: {status_text}\n"
        text += f"   ID: {server_id}\n\n"

        buttons.append([types.InlineKeyboardButton(
            text=f"{status_emoji} {name}",
            callback_data=f"server_detail_{server_id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="🔄 Обновить", callback_data="servers_refresh")])
    buttons.append([types.InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_add_server")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")])

    await render_screen(callback.message,
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "servers_refresh")
async def servers_refresh(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    await admin_servers_menu(callback)

@router.callback_query(F.data.startswith("server_detail_"))
async def server_detail(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    server_id = int(callback.data.replace("server_detail_", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, url, api_token, is_active, status, online_count, load_cpu, load_memory, total_traffic_gb, last_check FROM servers WHERE id = ?', (server_id,))
    server = c.fetchone()
    conn.close()

    if not server:
        await callback.message.answer("❌ Сервер не найден")
        await callback.answer()
        return

    server_id, name, url, api_token, is_active, status, online_count, load_cpu, load_memory, total_traffic_gb, last_check = server

    status_emoji = "🟢" if is_active and status == 'online' else "🔴"
    status_text = "✅ Активен" if is_active else "⛔ Отключён"

    text = f"🖥 <b>{name}</b>\n\n"
    text += f"🆔 ID: {server_id}\n"
    text += f"📡 URL: <code>{url}</code>\n"
    text += f"📊 Статус: {status_emoji} {status_text}\n"
    text += f"👥 Онлайн: {online_count or 0} клиентов\n"
    text += f"💻 CPU: {load_cpu or 0}%\n"
    text += f"🧠 Память: {load_memory or 0}%\n"
    text += f"📦 Трафик: {total_traffic_gb or 0} GB\n"
    text += f"🕐 Последняя проверка: {last_check[:16] if last_check else 'никогда'}\n"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Проверить", callback_data=f"server_check_{server_id}")],
        [types.InlineKeyboardButton(text="🗑 Удалить сервер", callback_data=f"server_delete_{server_id}")],
        [types.InlineKeyboardButton(text="🔙 Назад к списку", callback_data="admin_servers_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("server_check_"))
async def server_check(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    server_id = int(callback.data.replace("server_check_", ""))

    await callback.message.edit_text("⏳ Проверка сервера...", parse_mode="HTML")

    try:
        result = server_manager.check_server_health(server_id)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE servers SET status = ? WHERE id = ?', (result['status'], server_id))
        conn.commit()
        conn.close()

        await callback.message.answer(
            f"✅ <b>Сервер проверен</b>\n\n"
            f"Статус: {result['status']}\n"
            f"{result.get('message', '')}",
            parse_mode="HTML"
        )
        await admin_servers_menu(callback)

    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)}")
        await admin_servers_menu(callback)

@router.callback_query(F.data.startswith("server_delete_"))
async def server_delete_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    data = callback.data

    if data.startswith("server_delete_execute_"):
        server_id = int(data.replace("server_delete_execute_", ""))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT name FROM servers WHERE id = ?', (server_id,))
        server = c.fetchone()
        server_name = server[0] if server else "Сервер"

        c.execute('DELETE FROM servers WHERE id = ?', (server_id,))
        conn.commit()
        conn.close()

        await callback.answer(f"✅ Сервер «{server_name}» удалён", show_alert=True)
        await admin_servers_menu(callback)
        return

    if data.startswith("server_delete_"):
        server_id = int(data.replace("server_delete_", ""))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT name FROM servers WHERE id = ?', (server_id,))
        server = c.fetchone()
        conn.close()

        if not server:
            await callback.answer("❌ Сервер не найден", show_alert=True)
            await admin_servers_menu(callback)
            return

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"server_delete_execute_{server_id}")],
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data=f"server_detail_{server_id}")]
        ])

        await callback.message.edit_text(
            f"⚠️ <b>Удаление сервера</b>\n\n"
            f"Сервер: <b>{server[0]}</b>\n\n"
            f"<b>ВНИМАНИЕ!</b> Это действие удалит сервер из базы данных.\n\n"
            f"Вы уверены?",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
        return

@router.callback_query(F.data == "admin_add_server")
async def admin_add_server_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,AddServer.name,"Название сервера:")


@router.message(AddServer.name)
async def server_name(message: types.Message, state: FSMContext):
    name=message.text.strip()
    if len(name)>100:
        await form_retry(message,state,"❌ Название должно быть не длиннее 100 символов.")
        return
    await form_step(message,state,AddServer.url,"URL панели (https:// или http://):",name=name)


def panel_url(value):
    value=value.strip()
    parsed=urlsplit(value)
    if (len(value)>2048 or any(c.isspace() for c in value)
            or parsed.scheme not in ("http","https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.port==0):
        raise ValueError("Invalid panel URL")
    return value.rstrip("/")


@router.message(AddServer.url)
async def server_url(message: types.Message, state: FSMContext):
    try:
        url=panel_url(message.text)
    except ValueError:
        await form_retry(message,state,"❌ Введите адрес панели без логина, пароля, параметров и фрагмента.")
        return
    await form_step(message,state,AddServer.token,
        "API Token (будет зашифрован в базе; бот попробует удалить сообщение с токеном):",url=url)


@router.message(AddServer.token)
async def server_token(message: types.Message, state: FSMContext):
    data=await state.get_data()
    token=message.text.strip()
    removed=True
    try:
        await message.delete()
    except TelegramAPIError:
        removed=False
    notice="" if removed else "\nУдалите сообщение с токеном вручную."
    if not token or len(token)>4096 or any(c.isspace() for c in token):
        await form_retry(message,state,"❌ Некорректный токен."+notice)
        return
    try:
        encoded=encrypt(token)
        with closing(sqlite3.connect(DB_PATH)) as conn,conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM servers WHERE rtrim(url,'/')=?",(data["url"],)).fetchone():
                result="Сервер с таким URL уже есть. Новый сервер не добавлен."
            else:
                conn.execute("INSERT INTO servers(name,url,api_token,is_active,status) VALUES(?,?,?,?,?)",
                    (data["name"],data["url"],encoded,1,"pending"))
                result=f"✅ Сервер «{data['name']}» добавлен. Проверка соединения ещё не выполнена."
    except Exception as exc:
        logging.warning("Admin server insert requires review: %s",type(exc).__name__)
        result="Не удалось подтвердить добавление сервера. Проверьте список перед повтором."
    await form_finish(message,state,result+notice)


# ============================================================
# АДМИН: СТАТИСТИКА
# ============================================================

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users = c.fetchone()[0]
    c.execute('SELECT COUNT(*), SUM(amount_rub) FROM payments WHERE status="paid"')
    paid = c.fetchone()
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE is_active=1')
    subs = c.fetchone()[0]
    conn.close()

    text = f"📊 <b>Статистика:</b>\n\n👤 Пользователей: {users}\n✅ Подписок: {subs}\n💰 Платежей: {paid[0] or 0} на {paid[1] or 0:.0f}₽"
    kb = get_back_keyboard("admin_back_to_menu")

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# ============================================================
# АДМИН: ТЕКСТЫ
# ============================================================

ADMIN_TEXT_FIELDS = {
    'text_welcome': '🎉 Приветственное',
    'text_install_vpn': '📥 Установить VPN',
    'text_android_instruction': '🤖 Android инструкция',
    'text_ios_instruction': '🍏 iOS инструкция',
    'text_windows_instruction': '💻 Windows инструкция',
    'text_faq': '❓ Вопросы (FAQ)'
}


@router.callback_query(F.data == "admin_texts")
async def admin_texts(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    buttons=[]
    for key,label in ADMIN_TEXT_FIELDS.items():
        val=get_setting(key) or '(пусто)'
        short=val[:35].replace('\n',' ')+('...' if len(val)>35 else '')
        buttons.append([types.InlineKeyboardButton(text=f"✏️ {label}: {short}",callback_data=f"edittext_{key}")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад",callback_data="admin_back_to_menu")])
    await render_screen(callback.message,"💬 <b>Редактирование текстов</b>\nВыберите поле:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("edittext_"))
async def edit_text_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    field=callback.data.removeprefix("edittext_")
    if field not in ADMIN_TEXT_FIELDS:
        await callback.answer("Недопустимое поле",show_alert=True)
        return
    current=get_setting(field) or '(пусто)'
    preview=current[:2500]+("…" if len(current)>2500 else "")
    extend_temp.pop(callback.from_user.id,None)
    await form_begin(callback,state,EditText.waiting,
        f"✏️ {ADMIN_TEXT_FIELDS[field]}\n\nТекущий текст:\n{preview}\n\nОтправьте новый текст полностью. Поддерживается HTML.",
        fields={"field":field})


@router.message(EditText.waiting)
async def edit_text_save(message: types.Message, state: FSMContext):
    data=await state.get_data()
    field=data.get('field')
    if field not in ADMIN_TEXT_FIELDS:
        await form_finish(message,state,"Недопустимое поле. Изменения не сохранены.")
        return
    try:
        set_setting(field,message.text)
    except Exception:
        logging.exception("Admin text update failed")
        await form_finish(message,state,"Не удалось подтвердить сохранение текста. Проверьте текущее значение.")
        return
    await form_finish(message,state,f"✅ {ADMIN_TEXT_FIELDS[field]}: текст обновлён!")


# ============================================================
# АДМИН: АВТОМАТИЗАЦИЯ
# ============================================================

@router.callback_query(F.data == "admin_automation")
async def admin_automation_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'auto_renew_enabled'")
    auto_renew = c.fetchone()
    conn.close()

    status = "✅ Включено" if auto_renew and auto_renew[0] == 'true' else "❌ Выключено"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Авто-продление", callback_data="toggle_auto_renew")],
        [types.InlineKeyboardButton(text="📊 Статус авто-уведомлений", callback_data="auto_notify_status")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])

    await callback.message.edit_text(
        f"⚙️ <b>Настройки автоматизации</b>\n\n"
        f"📌 Авто-продление: {status}\n"
        f"📌 Авто-уведомления: Активны\n\n"
        f"Выберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "toggle_auto_renew")
async def toggle_auto_renew(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'auto_renew_enabled'")
    current = c.fetchone()

    new_value = 'false' if current and current[0] == 'true' else 'true'
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_renew_enabled', ?)", (new_value,))
    conn.commit()
    conn.close()

    status = "✅ Включено" if new_value == 'true' else "❌ Выключено"

    kb = get_back_keyboard("admin_automation")

    await callback.message.edit_text(
        f"🔄 Авто-продление: {status}",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "auto_notify_status")
async def auto_notify_status(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM notifications WHERE type = 'expiring_3days'")
    expiring_3days = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM notifications WHERE type = 'expiring_1day'")
    expiring_1day = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM notifications WHERE type = 'expiring_today'")
    expiring_today = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM notifications WHERE type = 'expired_yesterday'")
    expired_yesterday = c.fetchone()[0]

    conn.close()

    text = (
        f"📊 <b>Статистика уведомлений</b>\n\n"
        f"📌 За 3 дня: {expiring_3days}\n"
        f"📌 За 1 день: {expiring_1day}\n"
        f"📌 Сегодня: {expiring_today}\n"
        f"📌 Истекли вчера: {expired_yesterday}\n\n"
        f"🔄 Авто-уведомления работают каждые 6 часов"
    )

    kb = get_back_keyboard("admin_automation")

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# ============================================================
# АДМИН: НАЛОГИ
# ============================================================

@router.callback_query(F.data == "admin_tax")
async def admin_tax_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    tax_enabled = get_setting('tax_enabled') == 'true'
    tax_inn = get_setting('tax_inn') or 'Не настроен'

    status_text = "✅ Включено" if tax_enabled else "❌ Выключено"
    inn_display = f"ИНН: {tax_inn[:4]}****{tax_inn[-4:]}" if tax_inn != 'Не настроен' and len(tax_inn) >= 12 else "ИНН: не настроен"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Включить/Выключить", callback_data="tax_toggle")],
        [types.InlineKeyboardButton(text="📊 Статус", callback_data="tax_status")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])

    await callback.message.edit_text(
        f"🧾 <b>Настройки налогов</b>\n\n"
        f"📌 Статус: {status_text}\n"
        f"📌 {inn_display}\n\n"
        f"Для настройки используйте веб-админку",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "tax_toggle")
async def tax_toggle(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    current = get_setting('tax_enabled') == 'true'
    new_value = 'false' if current else 'true'
    set_setting('tax_enabled', new_value)

    status = "✅ Включено" if new_value == 'true' else "❌ Выключено"

    await callback.answer(f"Налоги: {status}", show_alert=True)
    await admin_tax_menu(callback)

@router.callback_query(F.data == "tax_status")
async def tax_status(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    tax_enabled = get_setting('tax_enabled') == 'true'
    tax_inn = get_setting('tax_inn') or 'Не настроен'
    has_password = bool(get_setting('tax_password'))

    text = (
        f"📊 <b>Статус налоговой интеграции</b>\n\n"
        f"🔹 Включено: {'✅' if tax_enabled else '❌'}\n"
        f"🔹 ИНН: {tax_inn}\n"
        f"🔹 Пароль: {'✅ Установлен' if has_password else '❌ Не установлен'}\n\n"
    )

    if not tax_enabled:
        text += "⚠️ Интеграция отключена."
    elif not has_password:
        text += "⚠️ Не установлен пароль от «Мой налог»."
    else:
        text += "✅ Всё готово к работе!"

    kb = get_back_keyboard("admin_tax")

    await callback.message.edit_text(
        text,
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

# ============================================================
# АДМИН: БЭКАП
# ============================================================

@router.callback_query(F.data == "admin_backup_menu")
async def admin_backup_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📦 Создать бэкап", callback_data="backup_create")],
        [types.InlineKeyboardButton(text="📤 Отправить бэкап в Telegram", callback_data="manual_backup")],
        [types.InlineKeyboardButton(text="📋 Список бэкапов", callback_data="backup_list")],
        [types.InlineKeyboardButton(text="🔄 Восстановить", callback_data="backup_restore")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])

    await callback.message.edit_text(
        "🗄 <b>Управление бэкапами</b>\n\nВыберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "manual_backup")
async def manual_backup_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    await callback.answer("⏳ Создание бэкапа...")

    try:
        from backup_sender import send_backup_to_admin
        await send_backup_to_admin()
        await callback.message.answer("✅ Бэкап отправлен в личные сообщения!", parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")

@router.callback_query(F.data == "backup_create")
async def backup_create(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    await callback.message.edit_text("⏳ Создание бэкапа...", parse_mode="HTML")

    try:
        result = backup_manager.create_backup()
        await callback.message.edit_text(
            f"✅ <b>Бэкап создан!</b>\n\n"
            f"📅 Дата: {result['timestamp']}\n"
            f"📁 Папка: {BACKUP_DIR}/",
            reply_markup=get_back_keyboard("backup_menu_back"),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка создания бэкапа: {str(e)}",
            reply_markup=get_back_keyboard("backup_menu_back")
        )

@router.callback_query(F.data == "backup_list")
async def backup_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    try:
        backups = backup_manager.get_backup_list()

        if not backups:
            await callback.message.edit_text(
                "📭 Нет сохранённых бэкапов",
                reply_markup=get_back_keyboard("backup_menu_back")
            )
            await callback.answer()
            return

        text = "📋 <b>Список бэкапов:</b>\n\n"
        for i, b in enumerate(backups[:10]):
            date_str = b['datetime'][:16]
            size_kb = b.get('size', 0) / 1024
            text += f"{i+1}. {date_str} — {size_kb:.1f} KB\n"

        if len(backups) > 10:
            text += f"\n... и ещё {len(backups) - 10} бэкапов"

        await callback.message.edit_text(
            text,
            reply_markup=get_back_keyboard("backup_menu_back"),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка: {str(e)}",
            reply_markup=get_back_keyboard("backup_menu_back")
        )

@router.callback_query(F.data == "backup_restore")
async def backup_restore(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    try:
        backups = backup_manager.get_backup_list()

        if not backups:
            await callback.message.edit_text(
                "📭 Нет бэкапов для восстановления",
                reply_markup=get_back_keyboard("backup_menu_back")
            )
            await callback.answer()
            return

        buttons = []
        for b in backups[:5]:
            buttons.append([types.InlineKeyboardButton(
                text=f"🔄 {b['timestamp']}",
                callback_data=f"backup_restore_confirm_{b['timestamp']}"
            )])
        buttons.append([types.InlineKeyboardButton(text="🔙 В меню бэкапов", callback_data="backup_menu_back")])

        await callback.message.edit_text(
            "🔄 <b>Выберите бэкап для восстановления:</b>\n\n"
            "ВНИМАНИЕ! Текущие данные будут заменены!",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка: {str(e)}",
            reply_markup=get_back_keyboard("backup_menu_back")
        )

@router.callback_query(F.data.startswith("backup_restore_confirm_"))
async def backup_restore_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    timestamp = callback.data.replace("backup_restore_confirm_", "")

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Да, восстановить", callback_data=f"backup_restore_execute_{timestamp}")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="backup_restore")]
    ])

    await callback.message.edit_text(
        f"⚠️ <b>Восстановление бэкапа</b>\n\n"
        f"Бэкап от: {timestamp}\n\n"
        f"<b>ВНИМАНИЕ!</b> Текущие данные будут заменены!\n\n"
        f"Вы уверены?",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("backup_restore_execute_"))
async def backup_restore_execute(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    timestamp = callback.data.replace("backup_restore_execute_", "")

    await callback.message.edit_text("⏳ Восстановление бэкапа...", parse_mode="HTML")

    try:
        result = backup_manager.restore_backup(timestamp)

        if result['success']:
            await callback.message.edit_text(
                f"✅ <b>Бэкап восстановлен!</b>\n\n"
                f"📅 Дата: {timestamp}\n"
                f"🔄 Данные восстановлены из бэкапа.",
                reply_markup=get_back_keyboard("backup_menu_back"),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"❌ Ошибка восстановления: {result.get('error', 'Неизвестная ошибка')}",
                reply_markup=get_back_keyboard("backup_menu_back")
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка: {str(e)}",
            reply_markup=get_back_keyboard("backup_menu_back")
        )

@router.callback_query(F.data == "backup_menu_back")
async def backup_menu_back(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    await admin_backup_menu(callback)

# ============================================================
# АДМИН: РАССЫЛКА ПРОМОКОДОВ
# ============================================================

@router.callback_query(F.data == "admin_broadcast_promo")
async def admin_broadcast_promo_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM promocodes WHERE trigger_type != 'manual' AND is_active = 1")
    achievement_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM promocodes WHERE trigger_type = 'manual' AND is_active = 1")
    manual_count = c.fetchone()[0]
    conn.close()

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=f"🏆 Достижения ({achievement_count})",
            callback_data="promo_broadcast_achievements"
        )],
        [types.InlineKeyboardButton(
            text=f"✋ Ручные ({manual_count})",
            callback_data="promo_manual"
        )],
        [types.InlineKeyboardButton(
            text="🔙 Назад",
            callback_data="admin_back_to_menu"
        )]
    ])

    text = "📢 <b>Рассылка промокодов</b>\n\nВыберите раздел:"

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "promo_broadcast_achievements")
async def promo_broadcast_achievements_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT code, discount_percent, trigger_type
        FROM promocodes
        WHERE trigger_type != 'manual' AND is_active = 1
        ORDER BY trigger_type, code
    ''')
    promos = c.fetchall()
    conn.close()

    if not promos:
        await callback.message.edit_text(
            "🏆 <b>Достижения</b>\n\n❌ Нет активных промокодов в этом разделе.",
            reply_markup=get_back_keyboard("admin_broadcast_promo"),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    buttons = []
    for code, discount, trigger_type in promos:
        icons = {
            'referrals': '👥',
            'purchases': '🛒',
            'subscription_days': '📅',
            'first_payment': '💳'
        }
        icon = icons.get(trigger_type, '🎯')
        buttons.append([types.InlineKeyboardButton(
            text=f"{icon} {code} ({discount}%)",
            callback_data=f"broadcast_promo_{code}"
        )])

    buttons.append([types.InlineKeyboardButton(
        text="🔙 Назад к разделам",
        callback_data="admin_broadcast_promo"
    )])

    await callback.message.edit_text(
        f"🏆 <b>Достижения</b>\n\nВсего: {len(promos)}\n\nВыберите промокод для рассылки:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "promo_manual")
async def promo_manual_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT code, discount_percent
        FROM promocodes
        WHERE trigger_type = 'manual' AND is_active = 1
        ORDER BY created_at DESC
    ''')
    promos = c.fetchall()
    conn.close()

    if not promos:
        await callback.message.edit_text(
            "✋ <b>Ручные</b>\n\n❌ Нет активных промокодов в этом разделе.",
            reply_markup=get_back_keyboard("admin_broadcast_promo"),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    buttons = []
    for code, discount in promos:
        buttons.append([types.InlineKeyboardButton(
            text=f"📌 {code} ({discount}%)",
            callback_data=f"broadcast_promo_{code}"
        )])

    buttons.append([types.InlineKeyboardButton(
        text="🔙 Назад к разделам",
        callback_data="admin_broadcast_promo"
    )])

    await callback.message.edit_text(
        f"✋ <b>Ручные</b>\n\nВсего: {len(promos)}\n\nВыберите промокод для рассылки:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("broadcast_promo_"))
async def broadcast_promo_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    code = callback.data.replace("broadcast_promo_", "")

    logging.info(f"🔍 Выбран промокод: {code}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, discount_percent, valid_until FROM promocodes WHERE upper(code) = upper(?) AND is_active = 1", (code,))
    promo = c.fetchone()
    conn.close()

    if not promo:
        await callback.answer("❌ Промокод не найден", show_alert=True)
        return

    code, discount, valid_until = promo
    valid_text = valid_until[:10] if valid_until else "бессрочно"

    text = f"📢 <b>Отправить промокод?</b>\n\n"
    text += f"🏷️ {code} ({discount}%)\n"
    text += f"📅 Действует до: {valid_text}\n\n"
    text += "Сообщение будет отправлено всем пользователям."

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Отправить", callback_data=f"confirm_send_{code}")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_promo")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_send_"))
async def confirm_send_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    code = callback.data.replace("confirm_send_", "")

    logging.info(f"✅ Подтверждена отправка: {code}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, discount_percent FROM promocodes WHERE upper(code) = upper(?) AND is_active = 1", (code,))
    promo = c.fetchone()
    conn.close()

    if not promo:
        await callback.answer("❌ Промокод не найден", show_alert=True)
        return

    code, discount = promo

    message = f"🎉 <b>Новый промокод!</b>\n\n🏷️ <b>{code}</b> — скидка {discount}%\n\n💡 Введите код в разделе «🎫 Промокоды и достижения»"

    await callback.message.edit_text("⏳ Отправка...", parse_mode="HTML")

    from broadcast import send_broadcast_with_text
    sent = await send_broadcast_with_text(message)

    await callback.message.edit_text(
        f"✅ <b>Готово!</b>\n\n📨 Отправлено: {sent} пользователям\n🏷️ Промокод: {code}",
        reply_markup=get_back_keyboard("admin_broadcast_promo"),
        parse_mode="HTML"
    )
    await callback.answer()

# ============================================================
# АДМИН: ЗАЩИТА ОТ СПАМА
# ============================================================

@router.callback_query(F.data == "admin_rate_limit")
async def admin_rate_limit_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    from rate_limiter import rate_limiter

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('SELECT COUNT(DISTINCT user_id) FROM rate_limits WHERE is_blocked = 1')
    blocked_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM rate_limits')
    total_records = c.fetchone()[0]
    conn.close()

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📊 Статистика", callback_data="rate_limit_stats")],
        [types.InlineKeyboardButton(text="🔓 Снять блокировку", callback_data="rate_limit_unblock")],
        [types.InlineKeyboardButton(text="⚙️ Настройки", callback_data="rate_limit_settings")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])

    await callback.message.edit_text(
        f"🛡️ <b>Защита от спама и брутфорса</b>\n\n"
        f"📊 Заблокировано пользователей: {blocked_count}\n"
        f"📋 Всего записей в БД: {total_records}\n\n"
        f"Выберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "rate_limit_stats")
async def rate_limit_stats(callback: types.CallbackQuery):
    """Read-only summary for the statistics button in spam protection."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM rate_limits WHERE is_blocked = 1"
        ).fetchone()[0]
    await callback.message.edit_text(
        "📊 <b>Статистика защиты от спама</b>\\n\\n"
        f"Записей лимитов: {total}\\n"
        f"Заблокировано пользователей: {blocked}\\n\\n"
        "Для просмотра списка и снятия блокировок вернитесь в меню защиты.",
        reply_markup=get_back_keyboard("admin_rate_limit"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "rate_limit_settings")
async def rate_limit_settings(callback: types.CallbackQuery):
    """Настройки защиты от спама"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    from rate_limiter import rate_limiter

    text = "⚙️ <b>Настройки лимитов</b>\n\n"
    
    # Проверяем, есть ли атрибут limits
    if hasattr(rate_limiter, 'limits') and rate_limiter.limits:
        limits = rate_limiter.limits
        for action_type, config in limits.items():
            text += f"📌 <b>{action_type}</b>\n"
            text += f"   Макс. запросов: {config.get('max_requests', '?')}\n"
            text += f"   Окно (сек): {config.get('time_window', '?')}\n"
            text += f"   Блокировка (сек): {config.get('block_duration', '?')}\n\n"
    else:
        # Если нет атрибута — показываем дефолтные значения
        text += "📌 <b>command</b>\n"
        text += "   Макс. запросов: 10\n"
        text += "   Окно (сек): 60\n"
        text += "   Блокировка (сек): 300\n\n"
        text += "📌 <b>callback</b>\n"
        text += "   Макс. запросов: 30\n"
        text += "   Окно (сек): 60\n"
        text += "   Блокировка (сек): 300\n\n"
        text += "📌 <b>message</b>\n"
        text += "   Макс. запросов: 20\n"
        text += "   Окно (сек): 60\n"
        text += "   Блокировка (сек): 600\n\n"
        text += "📌 <b>payment</b>\n"
        text += "   Макс. запросов: 5\n"
        text += "   Окно (сек): 300\n"
        text += "   Блокировка (сек): 1800\n\n"
        text += "📌 <b>admin</b>\n"
        text += "   Макс. запросов: 100\n"
        text += "   Окно (сек): 60\n"
        text += "   Блокировка (сек): 60\n\n"

    text += "Для изменения настроек отредактируйте файл rate_limiter.py"

    kb = get_back_keyboard("admin_rate_limit")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "rate_limit_unblock")
async def rate_limit_unblock_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        SELECT DISTINCT user_id, blocked_until
        FROM rate_limits
        WHERE is_blocked = 1
        ORDER BY blocked_until DESC
        LIMIT 20
    ''')
    blocked_users = c.fetchall()
    conn.close()

    if not blocked_users:
        await callback.message.edit_text(
            "✅ Нет заблокированных пользователей",
            reply_markup=get_back_keyboard("admin_rate_limit"),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    buttons = []
    for user_id, blocked_until in blocked_users:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT first_name, username FROM users WHERE telegram_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()

        name = user[0] or user[1] or str(user_id) if user else str(user_id)
        until = blocked_until[:16] if blocked_until else "неизвестно"

        buttons.append([types.InlineKeyboardButton(
            text=f"🔓 {name} ({user_id}) — до {until}",
            callback_data=f"rate_unblock_{user_id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_rate_limit")])

    await callback.message.edit_text(
        "🔓 <b>Заблокированные пользователи</b>\n\n"
        "Выберите пользователя для снятия блокировки:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("rate_unblock_"))
async def rate_limit_unblock_user(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    user_id = int(callback.data.replace("rate_unblock_", ""))

    from rate_limiter import rate_limiter
    rate_limiter.reset_limits(user_id)

    await callback.answer(f"✅ Блокировка снята с пользователя {user_id}", show_alert=True)
    await rate_limit_unblock_menu(callback)

# ============================================================
# ВОЗВРАТ В АДМИН-МЕНЮ
# ============================================================

@router.callback_query(F.data == "admin_back_to_menu")
async def admin_back_to_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    await callback.message.edit_text(
        "🔧 <b>Админ-панель</b>\n\nВыберите раздел для управления:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_settings_menu")
async def admin_settings_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён")
        return

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📋 Тарифы", callback_data="admin_tariffs")],
        [types.InlineKeyboardButton(text="🖥 Серверы", callback_data="admin_servers_menu")],
        [types.InlineKeyboardButton(text="💬 Тексты", callback_data="admin_texts")],
        [types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [types.InlineKeyboardButton(text="⚡ Автоматизация", callback_data="admin_automation")],
        [types.InlineKeyboardButton(text="🧾 Налоги", callback_data="admin_tax")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_to_menu")]
    ])

    await callback.message.edit_text(
        "⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


# Final navigation safety-net. Registered last to avoid shadowing real actions.
@router.callback_query()
async def unknown_callback_help(callback: types.CallbackQuery):
    await callback.answer(
        "Эта кнопка устарела. Откройте главное меню командой /menu.",
        show_alert=True,
    )


@router.message(F.text)
async def unknown_message_help(message: types.Message):
    if message.text and message.text.startswith("/"):
        await message.answer(
            "Неизвестная команда. Используйте /menu или /help."
        )
    else:
        await message.answer(
            "Не поняла сообщение. Выберите нужный раздел на клавиатуре "
            "или отправьте /help."
        )
