from services.vpn_service import vpn_service
"""
Пользовательские хендлеры бота
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Router, types, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext

from database import get_setting, set_setting
from keyboards import (
    get_main_keyboard,
    get_device_keyboard,
    get_tariffs_keyboard,
    get_admin_keyboard,
    get_back_keyboard,
    get_profile_keyboard
)
from states import (
    AddTariff,
    AddServer,
    EditText,
    SendMessageToUser,
    EnterPromocode,
    EditTariff,
    AdminDeposit,
    AdminWithdraw
)
from utils import (
    generate_short_email,
    find_subid_by_email,
    has_used_trial,
    get_active_subscriptions,
    create_vpn_client,
    get_server_link,
    get_user_balance,
    get_tariff_by_id,
    get_user_by_telegram_id
)
from xui_client import XUIClient
from server_manager import server_manager
from yookassa import Payment

router = Router()
DB_PATH = '/opt/vpn-bot/data.db'
ADMIN_ID = int(get_setting('admin_id') or 812021055)

# Временное хранилище для продления
renew_temp = {}
extend_temp = {}

# ============ СТАРТ ============

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user

    ref_id = None
    if message.text and "ref_" in message.text:
        try:
            ref_id = int(message.text.split("ref_")[1])
        except:
            pass

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Проверяем, существует ли пользователь
    c.execute('SELECT telegram_id FROM users WHERE telegram_id = ?', (user.id,))
    existing = c.fetchone()

    if not existing:
        # Новый пользователь - добавляем с рефералом
        c.execute('INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)',
                  (user.id, user.username, user.first_name))

        # Если есть реферал - добавляем связь
        if ref_id and ref_id != user.id:
            from referral import referral_manager
            referral_manager.add_referral(user.id, ref_id)

            # Проверяем триггеры для рефералов
            from trigger_manager import trigger_manager
            activated = trigger_manager.check_and_apply_triggers(ref_id, 'referrals')
            if activated:
                for act in activated:
                    promo = act['promocode']
                    try:
                        await bot.send_message(
                            chat_id=ref_id,
                            text=f"🎉 <b>Достижение выполнено!</b>\n\n"
                                 f"Вы пригласили {act['current_value']} друзей!\n"
                                 f"Активирован промокод: <b>{promo['code']}</b>\n"
                                 f"🎁 Скидка: {promo['discount_percent']}%\n\n"
                                 f"Промокод уже применён к вашему аккаунту!",
                            parse_mode="HTML"
                        )
                        logger.info(f"✅ Уведомление о триггере отправлено {ref_id}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки уведомления о триггере: {e}")

            # Уведомляем реферала (если он есть и это не бот)
            try:
                bot = message.bot
                await bot.send_message(
                    chat_id=ref_id,
                    text=f"🎉 <b>Новый реферал!</b>\n\n"
                         f"Пользователь @{user.username or user.first_name} зарегистрировался по вашей ссылке!\n"
                         f"Вы получите бонусные дни, когда он купит подписку.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Не удалось уведомить реферала {ref_id}: {e}")
    else:
        # Обновляем существующего пользователя
        c.execute('UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?',
                  (user.username, user.first_name, user.id))

    conn.commit()
    conn.close()

    welcome_text = get_setting('text_welcome') or (
        "🎉 <b>Добро пожаловать в VPN Bot!</b>\n\n"
        "Мы выдаём VPN-ключи для доступа к быстрым и безопасным серверам на протоколе VLESS.\n"
        "Просто вставьте ключ в приложение.\n\n"
        "Меню находится в клавиатуре (≡) — выберите нужный раздел или сразу установите VPN."
    )

    start_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="Главное меню", callback_data="start_menu")]
])

    await message.answer(welcome_text, reply_markup=start_kb, parse_mode="HTML", disable_web_page_preview=True)

@router.callback_query(F.data == "start_menu")
async def start_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

# ============ ОСНОВНЫЕ КНОПКИ ============

@router.message(F.text == "📥 Установить VPN")
async def install_vpn(message: types.Message):
    text = get_setting('text_install_vpn') or "Выберите ваше устройство:"
    await message.answer(text, reply_markup=get_device_keyboard(), parse_mode="HTML", disable_web_page_preview=True)

@router.message(F.text == "📋 Купить ключ")
async def buy_key(message: types.Message):
    """Кнопка 'Купить ключ' — показывает тарифы"""
    await message.answer("Выберите подходящий тариф:", reply_markup=get_tariffs_keyboard(), parse_mode="HTML")

@router.message(F.text == "❓ Вопросы")
async def faq(message: types.Message):
    faq_text = get_setting('text_faq') or (
        "❓ <b>Часто задаваемые вопросы</b>\n\n"
        "1️⃣ <b>Как установить VPN?</b>\n"
        "Нажмите «📥 Установить VPN» и выберите ваше устройство.\n\n"
        "2️⃣ <b>Сколько стоит подписка?</b>\n"
        "Цены указаны в разделе «📋 Купить ключ»\n\n"
        "3️⃣ <b>Как оплатить?</b>\n"
        "Выберите тариф, нажмите «Оплатить» и следуйте инструкциям.\n\n"
        "4️⃣ <b>Что делать если не работает?</b>\n"
        "Напишите нашему оператору: @vpn4us_support\n\n"
        "5️⃣ <b>Можно ли использовать на нескольких устройствах?</b>\n"
        "Да, до 5 устройств одновременно.\n\n"
        "6️⃣ <b>Как получить бонус за приглашение?</b>\n"
        "Используйте реферальную ссылку из раздела «🎁 Пригласить друга»"
    )

    kb = get_back_keyboard("back_to_main")
    await message.answer(faq_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@router.message(F.text == "🎁 Пригласить друга")
async def invite_friend(message: types.Message):
    bot_username = (await message.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"

    from referral import referral_manager
    stats = referral_manager.get_referral_stats(message.from_user.id)

    invite_text = (
        "🎁 <b>Реферальная программа</b>\n\n"
        "Приглашайте друзей и получайте бонусные дни к подписке!\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        f"👤 1 уровень (прямые): {stats['level_1']} чел. → +30 дней\n"
        f"👤 2 уровень: {stats['level_2']} чел. → +15 дней\n"
        f"👤 3 уровень: {stats['level_3']} чел. → +5 дней\n"
        f"📦 Всего бонусных дней: {stats['bonus_days']} дн.\n"
        f"👥 Всего рефералов: {stats['total']}\n\n"
        "🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{invite_link}</code>\n\n"
        "📖 <b>Как это работает?</b>\n"
        "1. Отправьте ссылку другу\n"
        "2. Друг регистрируется по ссылке\n"
        "3. Когда друг купит подписку, вы получите бонусные дни!\n"
        "4. Бонусы начисляются за 3 уровня глубины"
    )

    kb = get_back_keyboard("back_to_main")
    await message.answer(invite_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@router.message(F.text == "🔧 Админ-панель")
async def admin_panel_button(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён", disable_web_page_preview=True)
        return
    await message.answer(
        "🔧 <b>Админ-панель</b>\n\nВыберите раздел для управления:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@router.message(F.text == "/menu")
async def refresh_menu(message: types.Message):
    await message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

# ============ ОБРАБОТКА УСТРОЙСТВ ============

@router.callback_query(lambda c: c.data.startswith("device_") and not c.data.startswith("select_device_"))
async def device_instruction(callback: types.CallbackQuery):
    logging.info(f"=== DEVICE INSTRUCTION: {callback.data} ===")

    device = callback.data.replace("device_", "")
    device_names = {
        'android': 'Android',
        'ios': 'iOS',
        'windows': 'Windows'
    }
    device_name = device_names.get(device, device)

    # Получаем все активные подписки пользователя
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, s.xui_email, s.end_date, t.name, s.xui_client_uid
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.telegram_id = ? AND s.is_active = 1 AND datetime(s.end_date) > datetime('now')
        ORDER BY s.end_date DESC
    ''', (callback.from_user.id,))
    subs = c.fetchall()
    conn.close()

    if not subs:
        await callback.message.answer(
            "❌ У вас нет активной подписки.\n\n"
            "Пожалуйста, выберите тариф в разделе «📋 Купить ключ» и оплатите подписку.",
            reply_markup=get_tariffs_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Если подписка одна - сразу показываем инструкцию
    if len(subs) == 1:
        sub_id, email, end_date, tariff_name, xui_client_uid = subs[0]
        await show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid)
        return

    # Если несколько подписок - показываем выбор
    buttons = []
    for sub in subs:
        sub_id, email, end_date, tariff_name, xui_client_uid = sub
        days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
        buttons.append([types.InlineKeyboardButton(
            text=f"📦 {tariff_name} (до {end_date[:10]}, {days_left} дн.)",
            callback_data=f"select_device_{device}_{sub_id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_devices")])

    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.answer(
        f"📱 <b>Вы выбрали {device_name}</b>\n\n"
        f"У вас есть несколько активных подписок.\n"
        f"Выберите, для какой подписки показать инструкцию:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("select_device_"))
async def device_key_selected(callback: types.CallbackQuery):
    """Обработчик выбора ключа для устройства"""
    logging.info(f"=== DEVICE KEY SELECTED: {callback.data} ===")

    parts = callback.data.split("_")
    device = parts[2]
    sub_id = int(parts[3])

    device_names = {
        'android': 'Android',
        'ios': 'iOS',
        'windows': 'Windows'
    }
    device_name = device_names.get(device, device)

    # Получаем данные подписки
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.xui_email, s.end_date, t.name, s.xui_client_uid
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.id = ? AND s.telegram_id = ?
    ''', (sub_id, callback.from_user.id))
    sub = c.fetchone()
    conn.close()

    if not sub:
        await callback.message.answer("❌ Подписка не найдена", parse_mode="HTML")
        await callback.answer()
        return

    email, end_date, tariff_name, xui_client_uid = sub

    await show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid)

async def show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid):
    """Показывает инструкцию для выбранного ключа"""
    from services.vpn_service import vpn_service

    instructions = {
        'android': get_setting('text_android_instruction') or (
            "📱 <b>Настройка для Android</b>\n\n"
            "1. Установите Incy из Google Play\n"
            "2. Нажмите «Добавить в 1 клик» ниже\n"
            "3. Подтвердите импорт подписки\n"
            "4. Нажмите «Подключиться»"
        ),
        'ios': get_setting('text_ios_instruction') or (
            "🍏 <b>Настройка для iOS</b>\n\n"
            "1. Установите Incy из App Store\n"
            "2. Нажмите «Добавить в 1 клик» ниже\n"
            "3. Подтвердите импорт подписки\n"
            "4. Нажмите «Подключиться»"
        ),
        'windows': get_setting('text_windows_instruction') or (
            "💻 <b>Настройка для Windows</b>\n\n"
            "1. Скачайте Incy\n"
            "2. Скопируйте ссылку ниже\n"
            "3. Импортируйте ссылку\n"
            "4. Нажмите «Подключиться»"
        )
    }

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

    text = instructions.get(device, instructions['android'])
    text = f"📦 <b>Тариф: {tariff_name}</b>\n\n" + text

    if device == 'windows':
        text += f"\n\n🔗 <b>Ваша ссылка:</b>\n<code>{link}</code>"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 Назад к ключам", callback_data="back_to_keys_from_install")],
            [types.InlineKeyboardButton(text="🔙 Назад к устройствам", callback_data="back_to_devices")]
        ])
    else:
        if device == 'ios':
            app_store_link = "https://apps.apple.com/ru/app/incy/id6756943388"
            button_text = "📲 Скачать Incy (App Store)"
        else:
            app_store_link = "https://play.google.com/store/apps/details?id=llc.itdev.incy"
            button_text = "📲 Скачать Incy (Google Play)"

        text += f"\n\n🔗 <b>Ваша ссылка для подключения:</b>\n<code>{link}</code>"
        text += "\n\n👆 Коснитесь ссылки, чтобы скопировать её в буфер обмена"

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=button_text, url=app_store_link)],
            [types.InlineKeyboardButton(text="🔙 Назад к ключам", callback_data="back_to_keys_from_install")],
            [types.InlineKeyboardButton(text="🔙 Назад к устройствам", callback_data="back_to_devices")]
        ])

    await callback.message.answer(
        text,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_devices")
async def back_to_devices(callback: types.CallbackQuery):
    """Возврат к выбору устройства"""
    await callback.message.delete()
    await callback.message.answer(
        "Выберите ваше устройство:",
        reply_markup=get_device_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_keys_from_install")
async def back_to_keys_from_install(callback: types.CallbackQuery):
    """Возврат к списку ключей из установки VPN"""
    await callback.message.delete()
    # Показываем список ключей
    from handlers.profile import show_my_keys
    await show_my_keys(callback)
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

# ============ ОБРАБОТКА ПРОМОКОДОВ (ИСПРАВЛЕНА) ============

@router.message(F.text, EnterPromocode.waiting)
async def process_promocode_input(message: types.Message, state: FSMContext):
    """
    Обработка введённого промокода.
    Срабатывает ТОЛЬКО когда пользователь в состоянии EnterPromocode.waiting.
    """
    code = message.text.strip().upper()
    
    # Проверяем, что это не команда
    if code.startswith('/'):
        await state.clear()
        await message.answer("❌ Ввод промокода отменён.", parse_mode="HTML")
        return

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
        await state.clear()
        return

    promocode_id, discount_percent, discount_amount, valid_until, max_uses, used_count = promocode

    if valid_until and datetime.now() > datetime.fromisoformat(valid_until):
        await message.answer("❌ Срок действия промокода истек", parse_mode="HTML")
        conn.close()
        await state.clear()
        return

    if used_count >= max_uses:
        await message.answer("❌ Промокод уже использован максимальное количество раз", parse_mode="HTML")
        conn.close()
        await state.clear()
        return

    c.execute('SELECT id FROM promocode_uses WHERE promocode_id = ? AND user_id = ?',
              (promocode_id, message.from_user.id))
    if c.fetchone():
        await message.answer("❌ Вы уже использовали этот промокод", parse_mode="HTML")
        conn.close()
        await state.clear()
        return

    c.execute('INSERT INTO promocode_uses (promocode_id, user_id) VALUES (?, ?)',
              (promocode_id, message.from_user.id))
    c.execute('UPDATE promocodes SET used_count = used_count + 1 WHERE id = ?', (promocode_id,))
    conn.commit()
    conn.close()

    # Сохраняем промокод в настройках пользователя
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

    # Очищаем состояние
    await state.clear()
    
    # Возвращаемся в главное меню
    await message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@router.message(Command("cancel"), EnterPromocode.waiting)
async def cancel_promocode_input(message: types.Message, state: FSMContext):
    """Отмена ввода промокода"""
    await state.clear()
    await message.answer(
        "❌ Ввод промокода отменён.",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

