from services.vpn_service import vpn_service
"""
Обработчики профиля и ключей
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

from database import get_setting, set_setting
from keyboards import (
    get_keys_keyboard,
    get_profile_keyboard,
    get_tariffs_keyboard,
    get_main_keyboard,
    get_confirmation_keyboard,
    get_back_keyboard
)
from states import EnterPromocode, AdminAddKey, UserExtendKey, CustomDeposit
from utils import (
    get_active_subscriptions,
    get_user_balance,
    find_subid_by_email,
    get_server_link,
    extend_subscription,
    get_subscription_details,
    get_tariff_by_id
)
from server_manager import server_manager
from services.vpn_service import vpn_service
from xui_client import XUIClient

router = Router()
DB_PATH = '/opt/vpn-bot/data.db'
ADMIN_ID = int(get_setting('admin_id') or 812021055)

renew_temp = {}

# ============ ПРОФИЛЬ ============

@router.message(F.text == "👤 Профиль")
async def profile(message: types.Message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('SELECT first_name, created_at, referrer_id, balance FROM users WHERE telegram_id = ?', (message.from_user.id,))
    user = c.fetchone()

    c.execute('SELECT COUNT(*) FROM subscriptions WHERE telegram_id = ? AND is_active = 1 AND datetime(end_date) > datetime("now")', (message.from_user.id,))
    active_subs_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM subscriptions WHERE telegram_id = ?', (message.from_user.id,))
    total_subs_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM users WHERE referrer_id = ?', (message.from_user.id,))
    ref_count = c.fetchone()[0]

    conn.close()

    profile_text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"📛 Имя: {user[0] if user else 'Не указано'}\n"
        f"📅 Регистрация: {user[1][:10] if user else 'Неизвестно'}\n"
        f"👥 Приглашено друзей: {ref_count}\n"
        f"💰 Баланс: {user[3] if user else 0:.2f} ₽\n"
        f"🔑 Активных ключей: {active_subs_count}\n"
        f"📋 Всего ключей: {total_subs_count}\n"
    )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM promocodes WHERE is_active = 1 AND (valid_until IS NULL OR valid_until > datetime("now"))')
    has_promocodes = c.fetchone()[0] > 0
    conn.close()

    await message.answer(
        profile_text,
        reply_markup=get_profile_keyboard(has_promocodes),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

@router.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    await callback.message.delete()
    class FakeMessage:
        from_user = callback.from_user
        chat = callback.message.chat
        async def answer(self, *args, **kwargs):
            await callback.message.answer(*args, **kwargs)
    await profile(FakeMessage())
    await callback.answer()

# ============ БАЛАНС И АВТО-ПРОДЛЕНИЕ ============

@router.callback_query(F.data == "balance_auto_renew")
async def balance_auto_renew_menu(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    balance = get_user_balance(telegram_id)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, t.name, s.end_date, s.auto_renew, t.price_rub
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.telegram_id = ? AND s.is_active = 1 AND datetime(s.end_date) > datetime('now')
        ORDER BY s.end_date ASC
    ''', (telegram_id,))
    subscriptions = c.fetchall()
    conn.close()

    text = f"""💰 <b>Баланс и авто-продление</b>

💳 <b>Ваш баланс:</b> <code>{balance:.2f} ₽</code>

<b>📖 Что такое баланс?</b>
Баланс — это ваш внутренний счёт в боте.

<b>🔄 Как работает авто-продление?</b>
• Включите авто-продление для нужных подписок
• За 3 дня до окончания мы напомним вам
• В день окончания спишем деньги с баланса

<b>📋 Ваши подписки:</b>
"""

    if not subscriptions:
        text += "\n❌ У вас нет активных подписок"
    else:
        for sub in subscriptions:
            sub_id, tariff_name, end_date, auto_renew, price = sub
            days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
            status = "✅" if auto_renew else "❌"
            text += f"\n📦 {tariff_name} (до {end_date[:10]}, {days_left} дн.) — {status} авто-продление"

    text += "\n\nВыберите действие:"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="deposit")],
        [types.InlineKeyboardButton(text="🔄 Управление подписками", callback_data="my_keys")],
        [types.InlineKeyboardButton(text="ℹ️ Как это работает", callback_data="balance_help")],
        [types.InlineKeyboardButton(text="🔙 Назад в профиль", callback_data="back_to_profile")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "balance_help")
async def balance_help(callback: types.CallbackQuery):
    text = """ℹ️ <b>Как работает баланс и авто-продление</b>

💰 <b>Баланс</b>
• Это ваш внутренний счёт в боте
• Можно пополнить через раздел "Пополнить баланс"

🔄 <b>Авто-продление</b>
• Включите авто-продление для любой подписки
• За 3 дня до окончания придёт напоминание
• В день окончания деньги автоматически спишутся

💡 <b>Зачем это нужно?</b>
• Не нужно каждый раз вручную продлевать
• Не потеряете доступ к VPN

⚠️ <b>Важно!</b>
• Убедитесь, что на балансе достаточно средств
• Вы всегда можете отключить авто-продление"""

    kb = get_back_keyboard("balance_auto_renew")
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# ============ КЛЮЧИ ============

@router.callback_query(F.data == "my_keys")
async def show_my_keys(callback: types.CallbackQuery):
    await callback.message.delete()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE telegram_id = ? AND is_active = 1 AND datetime(end_date) > datetime("now")', (callback.from_user.id,))
    count = c.fetchone()[0]
    conn.close()

    if count == 0:
        kb = get_back_keyboard("back_to_tariffs")
        await callback.message.answer(
            "🔑 <b>У вас нет активных ключей</b>\n\n"
            "Вы можете приобрести ключ в разделе «📋 Купить ключ».",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            "🔑 <b>Ваши ключи</b>\n\nВыберите подписку чтобы увидеть ключ и инструкцию:",
            reply_markup=get_keys_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
    await callback.answer()

@router.callback_query(F.data.startswith("key_"))
async def show_key_detail(callback: types.CallbackQuery):
    sub_id = int(callback.data.split("_")[1])
    sub = get_subscription_details(sub_id)

    if not sub or sub[8] != callback.from_user.id:
        await callback.message.answer("❌ Подписка не найдена", parse_mode="HTML")
        await callback.answer()
        return

    sub_id, email, end_date, tariff_name, xui_client_uid, traffic_used, tariff_id, start_date, user_id = sub
    days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
    tariff = get_tariff_by_id(tariff_id)
    traffic_limit = tariff[4] if tariff else 0
    ip_limit = tariff[5] if tariff else 3
    traffic_used_gb = traffic_used or 0
    traffic_limit_str = f"{traffic_limit:.0f} ГБ" if traffic_limit > 0 else "∞"

    subid = find_subid_by_email(email)
    if not subid:
        subid = xui_client_uid

    best_server = server_manager.get_best_server()
    if best_server:
        # Получаем ссылку из БД

        link = await vpn_service.get_subscription_link(sub_id)

        if not link:

            # Если ссылки нет в БД - формируем заново

            import re

            base_url = best_server["url"]

            match = re.match(r"(https?://[^/:]+(?::d+)?)", base_url)

            if match:

                domain_part = match.group(1)

                domain_part = re.sub(r":\d+$", ":2096", domain_part)

            else:

                domain_part = base_url.split("/panel")[0].split("/api")[0].rstrip("/")

                domain_part = re.sub(r":\d+$", ":2096", domain_part)

            link = f"{domain_part}/pod/{subid}"
    else:
        link = f"https://node5.vpn4us.ru:2096/sub/{subid}"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT auto_renew FROM subscriptions WHERE id = ?', (sub_id,))
    auto_renew_row = c.fetchone()
    conn.close()
    auto_renew_enabled = auto_renew_row[0] == 1 if auto_renew_row else False
    auto_renew_text = "✅" if auto_renew_enabled else "❌"
    user_balance = get_user_balance(callback.from_user.id)

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Продлить", callback_data=f"renew_from_key_{sub_id}")],
        [types.InlineKeyboardButton(text=f"🔄 Авто-продление {auto_renew_text}", callback_data=f"toggle_auto_renew_{sub_id}")],
        [types.InlineKeyboardButton(text="🗑 Удалить ключ", callback_data=f"delete_key_{sub_id}")],
        [types.InlineKeyboardButton(text="🔙 Назад к ключам", callback_data="my_keys")],
        [types.InlineKeyboardButton(text="🏠 На главную", callback_data="back_to_main")]
    ])

    text = (
        f"🔑 <b>Ключ: {tariff_name}</b>\n\n"
        f"📧 E-mail в панели: <code>{email}</code>\n"
        f"📅 Создан: {start_date[:10] if start_date else 'Неизвестно'}\n"
        f"📅 Истекает: {end_date[:10]}\n"
        f"⏳ Осталось: {days_left} дн.\n\n"
        f"📊 <b>Трафик:</b>\n"
        f"   • Использовано: {traffic_used_gb:.2f} ГБ\n"
        f"   • Лимит: {traffic_limit_str}\n"
        f"📱 Лимит IP: {ip_limit}\n\n"
        f"💳 <b>Баланс:</b> {user_balance:.2f} ₽\n"
        f"🔄 <b>Авто-продление:</b> {auto_renew_text}\n\n"
        f"🔗 <b>Ссылка:</b>\n"
        f"<code>{link}</code>"
    )

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

# ============ ПРОДЛЕНИЕ КЛЮЧА ============

@router.callback_query(F.data.startswith("renew_from_key_"))
async def renew_from_key(callback: types.CallbackQuery):
    sub_id = int(callback.data.replace("renew_from_key_", ""))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id FROM subscriptions WHERE id = ?', (sub_id,))
    sub = c.fetchone()
    conn.close()

    if not sub or sub[0] != callback.from_user.id:
        await callback.answer("⛔ Доступ запрещён")
        return

    renew_temp[callback.from_user.id] = sub_id
    await callback.message.answer(
        "📋 <b>Выберите тариф для продления</b>\n\n"
        "После оплаты дни будут добавлены к вашей подписке.",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

# ============ УДАЛЕНИЕ КЛЮЧА ============

@router.callback_query(F.data.startswith("delete_key_"))
async def user_delete_key(callback: types.CallbackQuery):
    sub_id = int(callback.data.replace("delete_key_", ""))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id, xui_email FROM subscriptions WHERE id = ?', (sub_id,))
    sub = c.fetchone()
    conn.close()

    if not sub or sub[0] != callback.from_user.id:
        await callback.answer("⛔ Доступ запрещён")
        return

    kb = get_confirmation_keyboard(f"confirm_delete_key_{sub_id}", "my_keys")
    await callback.message.answer(
        "⚠️ <b>Удаление ключа</b>\n\n"
        "Вы уверены, что хотите удалить этот ключ?\n"
        "После удаления доступ к VPN будет потерян.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_delete_key_"))
async def user_confirm_delete_key(callback: types.CallbackQuery):
    sub_id = int(callback.data.replace("confirm_delete_key_", ""))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id, xui_email FROM subscriptions WHERE id = ?', (sub_id,))
    sub = c.fetchone()

    if not sub or sub[0] != callback.from_user.id:
        await callback.answer("⛔ Доступ запрещён")
        return

    telegram_id, xui_email = sub
    best_server = server_manager.get_best_server()
    if best_server:
        xui = XUIClient(url=best_server['url'], api_token=best_server['api_token'])
        xui.delete_client(xui_email)

    c.execute('DELETE FROM subscriptions WHERE id = ?', (sub_id,))
    conn.commit()
    conn.close()

    await callback.message.answer("✅ Ключ удалён")
    await callback.answer()
    await show_my_keys(callback)

# ============ ПОПОЛНЕНИЕ БАЛАНСА ============

@router.callback_query(F.data == "deposit")
async def deposit_menu(callback: types.CallbackQuery):
    balance = get_user_balance(callback.from_user.id)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="100 ₽", callback_data="deposit_100")],
        [types.InlineKeyboardButton(text="200 ₽", callback_data="deposit_200")],
        [types.InlineKeyboardButton(text="500 ₽", callback_data="deposit_500")],
        [types.InlineKeyboardButton(text="1000 ₽", callback_data="deposit_1000")],
        [types.InlineKeyboardButton(text="✏️ Своя сумма", callback_data="deposit_custom")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
    ])
    await callback.message.edit_text(
        f"💰 <b>Пополнение баланса</b>\n\n"
        f"Текущий баланс: <b>{balance:.2f} ₽</b>\n\n"
        f"Выберите сумму для пополнения или введите свою:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "deposit_custom")
async def deposit_custom(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "✏️ <b>Введите сумму пополнения</b>\n\n"
        "Минимальная сумма: <b>10 ₽</b>\n"
        "Максимальная сумма: <b>100000 ₽</b>\n\n"
        "Введите число (например: 150, 250.50):\n"
        "Или отправьте /cancel для отмены",
        parse_mode="HTML"
    )
    await state.set_state(CustomDeposit.amount)
    await callback.answer()

@router.message(CustomDeposit.amount)
async def deposit_custom_amount(message: types.Message, state: FSMContext):
    if message.text.lower() in ['/cancel', 'отмена', 'cancel']:
        await state.clear()
        await message.answer("❌ Пополнение отменено.", parse_mode="HTML")
        return

    try:
        amount = float(message.text.replace(',', '.'))
        if amount < 10:
            await message.answer("❌ Минимальная сумма: <b>10 ₽</b>", parse_mode="HTML")
            return
        if amount > 100000:
            await message.answer("❌ Максимальная сумма: <b>100000 ₽</b>", parse_mode="HTML")
            return

        amount = round(amount, 2)
        from yookassa import Payment

        payment = Payment.create({
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/lestat258_bot"},
            "capture": True,
            "description": f"Пополнение баланса на {amount:.2f}₽",
            "metadata": {
                "telegram_id": message.from_user.id,
                "type": "deposit",
                "amount": amount
            }
        })

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status) VALUES (?, ?, ?, ?, ?)',
                  (payment.id, message.from_user.id, 0, amount, 'pending'))
        conn.commit()
        conn.close()

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"💳 Оплатить {amount:.2f}₽", url=payment.confirmation.confirmation_url)],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="deposit")]
        ])

        await message.answer(
            f"💳 <b>Пополнение баланса</b>\n\n"
            f"Сумма: <b>{amount:.2f} ₽</b>\n\n"
            f"Нажмите кнопку для оплаты:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число. Пример: 150", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
        await state.clear()

@router.callback_query(F.data.startswith("deposit_"))
async def deposit_process(callback: types.CallbackQuery):
    if callback.data == "deposit_custom":
        return
    amount = int(callback.data.replace("deposit_", ""))
    try:
        from yookassa import Payment
        payment = Payment.create({
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/lestat258_bot"},
            "capture": True,
            "description": f"Пополнение баланса на {amount}₽",
            "metadata": {
                "telegram_id": callback.from_user.id,
                "type": "deposit",
                "amount": amount
            }
        })
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status) VALUES (?, ?, ?, ?, ?)',
                  (payment.id, callback.from_user.id, 0, amount, 'pending'))
        conn.commit()
        conn.close()
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"💳 Оплатить {amount}₽", url=payment.confirmation.confirmation_url)],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="deposit")]
        ])
        await callback.message.edit_text(
            f"💳 <b>Пополнение баланса</b>\n\n"
            f"Сумма: <b>{amount} ₽</b>\n\n"
            f"Нажмите кнопку для оплаты:",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}", reply_markup=get_back_keyboard("deposit"))
    await callback.answer()

@router.message(Command("cancel"))
async def cancel_deposit(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == CustomDeposit.amount:
        await state.clear()
        await message.answer("❌ Пополнение отменено.", parse_mode="HTML")

# ============ АВТО-ПРОДЛЕНИЕ ============

@router.callback_query(F.data.startswith("toggle_auto_renew_"))
async def toggle_auto_renew(callback: types.CallbackQuery):
    sub_id = int(callback.data.replace("toggle_auto_renew_", ""))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id, auto_renew FROM subscriptions WHERE id = ?', (sub_id,))
    sub = c.fetchone()

    if not sub or sub[0] != callback.from_user.id:
        await callback.answer("⛔ Доступ запрещён")
        conn.close()
        return

    current_status = sub[1] or 0
    new_status = 0 if current_status else 1
    c.execute('UPDATE subscriptions SET auto_renew = ? WHERE id = ?', (new_status, sub_id))
    conn.commit()
    conn.close()

    status_text = "✅ Включено" if new_status else "❌ Выключено"
    await callback.answer(f"Авто-продление: {status_text}", show_alert=True)

    new_callback = types.CallbackQuery(
        id=callback.id,
        from_user=callback.from_user,
        chat_instance=callback.chat_instance,
        message=callback.message,
        data=f"key_{sub_id}",
        inline_message_id=callback.inline_message_id
    )
    await show_key_detail(new_callback)

# ============ ПРОМОКОДЫ И ДОСТИЖЕНИЯ ============

@router.callback_query(F.data == "promo_achievements")
async def promo_achievements_menu(callback: types.CallbackQuery):
    """Объединённое меню промокодов и достижений (главное меню раздела)"""
    from trigger_manager import trigger_manager
    from datetime import datetime

    trigger_manager.cleanup_expired_promocodes(callback.from_user.id)

    active = trigger_manager.get_user_promocodes(callback.from_user.id)
    status = trigger_manager.get_user_trigger_status(callback.from_user.id)

    text = "🎫 <b>Промокоды и достижения</b>\n\n"

    if active:
        text += "🟢 <b>Ваши промокоды:</b>\n\n"
        for promo in active:
            days_left = (datetime.fromisoformat(promo['expires_at']) - datetime.now()).days
            if promo['discount_percent'] > 0:
                discount_text = f"{promo['discount_percent']}%"
            else:
                discount_text = f"{promo['discount_amount']}₽"
            text += f"📌 <b>{promo['code']}</b> — {discount_text} (ещё {days_left} дн.)\n"
        text += "\n💡 Нажмите «Мои промокоды» для управления\n\n"
    else:
        text += "📭 <b>У вас нет активных промокодов</b>\n\n"

    if status:
        text += "🏆 <b>Прогресс достижений:</b>\n\n"
        names = {
            'referrals': '👥 Рефералы',
            'purchases': '🛒 Покупки',
            'subscription_days': '📅 Дни подписки',
            'first_payment': '💳 Первая оплата'
        }
        for trigger_type, data in status.items():
            name = names.get(trigger_type, trigger_type)
            current = data['current']
            target = data['target']
            progress = min(int((current / target) * 100), 100)
            filled = progress // 10
            empty = 10 - filled
            bar = "█" * filled + "░" * empty
            status_icon = "✅" if data['is_completed'] else "⏳"
            text += f"{name} {status_icon}  {current}/{target}  <code>{bar}</code>  {progress}%\n"
        text += "\n"

    text += "ℹ️ <b>Как это работает?</b>\n"
    text += "• Выполняйте действия → получайте промокоды\n"
    text += "• Промокоды появляются в «Мои промокоды»\n"
    text += "• Срок действия промокода — 3 месяца\n"
    text += "• Каждый промокод можно использовать 1 раз\n\n"
    text += "💡 Приглашайте друзей и получайте бонусы!"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎫 Мои промокоды", callback_data="my_promocodes")],
        [types.InlineKeyboardButton(text="🏆 Все достижения", callback_data="my_achievements")],
        [types.InlineKeyboardButton(text="🏷️ Ввести промокод", callback_data="profile_promocode")],
        [types.InlineKeyboardButton(text="📖 Как это работает", callback_data="achievements_help")],
        [types.InlineKeyboardButton(text="🔙 Назад в профиль", callback_data="back_to_profile")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "my_promocodes")
async def show_my_promocodes(callback: types.CallbackQuery):
    """Мои промокоды (уровень 2)"""
    from trigger_manager import trigger_manager
    from datetime import datetime

    trigger_manager.cleanup_expired_promocodes(callback.from_user.id)
    active = trigger_manager.get_user_promocodes(callback.from_user.id)
    used = trigger_manager.get_user_used_promocodes(callback.from_user.id)

    text = "🎫 <b>Мои промокоды</b>\n\n"

    if not active and not used:
        text += "У вас пока нет промокодов.\n"
        text += "Выполняйте достижения и получайте скидки!"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    buttons = []

    if active:
        for promo in active:
            days_left = (datetime.fromisoformat(promo['expires_at']) - datetime.now()).days
            discount_text = f"{promo['discount_percent']}%" if promo['discount_percent'] > 0 else f"{promo['discount_amount']}₽"
            expires_date = datetime.fromisoformat(promo['expires_at']).strftime("%d.%m.%Y")
            
            is_expired = days_left < 0
            status_emoji = "✅" if not is_expired else "❌"
            
            buttons.append([types.InlineKeyboardButton(
                text=f"{status_emoji} {promo['code']} ({discount_text}) до {expires_date}",
                callback_data=f"apply_promo_{promo['id']}"
            )])

    if used:
        text += "🔒 <b>Использованные:</b>\n\n"
        for promo in used[:5]:
            discount_text = f"{promo['discount_percent']}%" if promo['discount_percent'] > 0 else f"{promo['discount_amount']}₽"
            text += f"📌 {promo['code']} ({discount_text}) — {promo['used_at'][:10]}\n"
        text += "\n"

    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("apply_promo_"))
async def apply_promo_confirm(callback: types.CallbackQuery):
    """Подтверждение применения промокода (уровень 3)"""
    from trigger_manager import trigger_manager
    from datetime import datetime

    promo_id = int(callback.data.replace("apply_promo_", ""))
    promos = trigger_manager.get_user_promocodes(callback.from_user.id)
    promo = next((p for p in promos if p['id'] == promo_id), None)

    if not promo:
        await callback.answer("❌ Промокод не найден", show_alert=True)
        await show_my_promocodes(callback)
        return

    discount_text = f"{promo['discount_percent']}%" if promo['discount_percent'] > 0 else f"{promo['discount_amount']}₽"
    days_left = (datetime.fromisoformat(promo['expires_at']) - datetime.now()).days

    text = f"🎫 <b>Применить промокод?</b>\n\n"
    text += f"📌 <b>{promo['code']}</b>\n"
    text += f"💰 Скидка: {discount_text}\n"
    text += f"⏳ Действует: {days_left} дн.\n\n"
    text += "Промокод будет применён к следующей покупке.\n"
    text += "⚠️ Промокоды не суммируются!"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Да, применить", callback_data=f"confirm_apply_promo_{promo_id}")],
        [types.InlineKeyboardButton(text="❌ Нет, отмена", callback_data="my_promocodes")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_apply_promo_"))
async def confirm_apply_promo(callback: types.CallbackQuery):
    """Подтверждение применения промокода (уровень 4)"""
    from trigger_manager import trigger_manager
    promo_id = int(callback.data.replace("confirm_apply_promo_", ""))

    if trigger_manager.use_promocode(callback.from_user.id, promo_id):
        await callback.answer("✅ Промокод применён!", show_alert=True)
    else:
        await callback.answer("❌ Не удалось применить", show_alert=True)
    await show_my_promocodes(callback)


@router.callback_query(F.data == "my_achievements")
async def show_achievements(callback: types.CallbackQuery):
    """Все достижения (уровень 2)"""
    from trigger_manager import trigger_manager
    from datetime import datetime

    status = trigger_manager.get_user_trigger_status(callback.from_user.id)

    names = {
        'referrals': '👥 Рефералы',
        'purchases': '🛒 Покупки',
        'subscription_days': '📅 Дни подписки',
        'first_payment': '💳 Первая оплата'
    }
    descriptions = {
        'referrals': 'Приглашайте друзей по ссылке',
        'purchases': 'Оплачивайте подписки в боте',
        'subscription_days': 'Время с активной подпиской',
        'first_payment': 'Совершите первую покупку'
    }

    text = "🏆 <b>Достижения и бонусы</b>\n\n"
    text += f"🔄 Обновлено: {datetime.now().strftime('%H:%M:%S')}\n\n"

    if not status:
        text += "📋 <b>Доступные достижения:</b>\n\n"
        for key in ['referrals', 'purchases', 'subscription_days', 'first_payment']:
            text += f"{names[key]}\n"
            text += f"   {descriptions[key]}\n"
            text += f"   🎁 Награда: Промокод на скидку\n\n"
        text += "💡 Начните действовать, чтобы открыть достижения!"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    text += "📊 <b>Ваш прогресс:</b>\n\n"

    for trigger_type, data in status.items():
        name = names.get(trigger_type, trigger_type)
        desc = descriptions.get(trigger_type, '')

        current = data['current']
        target = data['target']
        progress = min(int((current / target) * 100), 100)
        filled = progress // 10
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        status_icon = "✅" if data['is_completed'] else "⏳"

        text += f"<b>{name}</b> {status_icon}\n"
        text += f"   {desc}\n"
        text += f"   {current}/{target}  <code>{bar}</code>  {progress}%\n"

        if data.get('promocodes'):
            text += "   🎁 <b>Награды:</b>\n"
            for promo in data['promocodes']:
                if promo['discount_percent'] > 0:
                    text += f"      • {promo['code']} — {promo['discount_percent']}%\n"
                else:
                    text += f"      • {promo['code']} — {promo['discount_amount']}₽\n"
        else:
            if data['is_completed']:
                text += "   ✅ Все награды получены!\n"
        text += "\n"

    text += "ℹ️ <b>Как это работает?</b>\n"
    text += "1️⃣ Выполняйте действия из списка\n"
    text += "2️⃣ При достижении цели вы получаете промокод\n"
    text += "3️⃣ Промокод появляется в «🎫 Мои промокоды»\n"
    text += "4️⃣ Используйте промокод в любое время (срок 3 месяца)\n\n"
    text += "💡 Приглашайте друзей и получайте бонусы!"

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Обновить", callback_data="my_achievements")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "achievements_help")
async def achievements_help(callback: types.CallbackQuery):
    """Помощь по достижениям (уровень 2)"""
    text = """📖 <b>Система достижений и бонусов</b>

🎯 <b>Что это?</b>
Вы выполняете действия → получаете промокоды на скидки!

📋 <b>Доступные достижения:</b>

👥 <b>Рефералы</b>
Приглашайте друзей: 1, 3, 5, 10 → промокод

🛒 <b>Покупки</b>
Оплачивайте подписки: 1, 3, 5 → промокод

📅 <b>Дни подписки</b>
Будьте с нами: 7, 30, 90, 365 → промокод

💳 <b>Первая оплата</b>
Первая покупка → промокод

⚡ <b>Как это работает?</b>
1️⃣ Выполняйте действие
2️⃣ При достижении цели → промокод
3️⃣ Промокод в «🎫 Мои промокоды»
4️⃣ Используйте когда хотите

📌 <b>Важно!</b>
• Срок действия: 3 месяца
• Каждый промокод = 1 раз
• Вы сами решаете когда применить

💡 Чем больше активность → тем больше скидок!"""

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")]
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "profile_promocode")
async def profile_promocode(callback: types.CallbackQuery, state: FSMContext):
    """Ввод промокода"""
    await state.set_state(EnterPromocode.waiting)  # ← ВАЖНО!
    
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="promo_achievements")]
    ])

    await callback.message.edit_text(
        "🏷️ <b>Ввести промокод</b>\n\n"
        "Введите промокод, чтобы получить скидку на подписку.\n"
        "Промокоды действуют ограниченное время.\n\n"
        "✏️ Отправьте код промокода в этом чате.\n"
        "Или отправьте /cancel для отмены.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


# @router.message(F.text)  # ВРЕМЕННО ОТКЛЮЧЕНО
async def process_promocode_input(message: types.Message, state: FSMContext):
    """Обработка введённого промокода"""
    if message.text.startswith('/'):
        return

    code = message.text.strip().upper()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        SELECT id, discount_percent, discount_amount, valid_until, max_uses, used_count
        FROM promocodes
        WHERE code = ? AND is_active = 1
    ''', (code,))
    promocode = c.fetchone()

    if not promocode:
        await message.answer("❌ Промокод не найден или неактивен", parse_mode="HTML")
        conn.close()
        return

    promocode_id, discount_percent, discount_amount, valid_until, max_uses, used_count = promocode

    if valid_until and datetime.now() > datetime.fromisoformat(valid_until):
        await message.answer("❌ Срок действия промокода истек", parse_mode="HTML")
        conn.close()
        return

    if used_count >= max_uses:
        await message.answer("❌ Промокод уже использован максимальное количество раз", parse_mode="HTML")
        conn.close()
        return

    c.execute('SELECT id FROM promocode_uses WHERE promocode_id = ? AND user_id = ?',
              (promocode_id, message.from_user.id))
    if c.fetchone():
        await message.answer("❌ Вы уже использовали этот промокод", parse_mode="HTML")
        conn.close()
        return

    c.execute('INSERT INTO promocode_uses (promocode_id, user_id) VALUES (?, ?)',
              (promocode_id, message.from_user.id))
    c.execute('UPDATE promocodes SET used_count = used_count + 1 WHERE id = ?', (promocode_id,))
    conn.commit()
    conn.close()

    if discount_percent > 0:
        set_setting(f'promocode_user_{message.from_user.id}', f'{discount_percent}|0')
        await message.answer(
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"🎁 Скидка: <b>{discount_percent}%</b>\n"
            f"💡 Скидка будет применена к следующей покупке.",
            parse_mode="HTML"
        )
    elif discount_amount > 0:
        set_setting(f'promocode_user_{message.from_user.id}', f'0|{discount_amount}')
        await message.answer(
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"🎁 Скидка: <b>{discount_amount} ₽</b>\n"
            f"💡 Скидка будет применена к следующей покупке.",
            parse_mode="HTML"
        )

    # Возвращаемся в главное меню промокодов
    await promo_achievements_menu(message)
