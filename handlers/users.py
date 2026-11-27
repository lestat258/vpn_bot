from runtime_config import DB_PATH
from screen_navigation import render_screen, render_command, render_home
from form_navigation import FORM_SCREEN, PROMO_STATE, current_form_input
from contextlib import closing
from subscription_renewal import expiry_presentation
from html import escape
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

    is_new = not existing
    try:
        if is_new:
            c.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
                (user.id, user.username, user.first_name)
            )
        else:
            c.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (user.username, user.first_name, user.id)
            )
        conn.commit()
    finally:
        conn.close()

    # Do not call referral_manager on a second SQLite connection while the
    # registration transaction is still uncommitted.
    if is_new and ref_id and ref_id != user.id:
        from referral import referral_manager
        try:
            referral_added = referral_manager.add_referral(user.id, ref_id)
        except Exception as exc:
            logging.exception("Failed to register referral for %s: %s", user.id, exc)
            referral_added = False

        if referral_added:
            try:
                from trigger_manager import trigger_manager
                activated = trigger_manager.check_and_apply_triggers(ref_id, 'referrals')
                for act in (activated or []):
                    promo = act.get('promocode', {})
                    await message.bot.send_message(
                        chat_id=ref_id,
                        text=(
                            "🎉 <b>Достижение выполнено!</b>\\n\\n"
                            f"Вы пригласили {act.get('current_value', 0)} друзей!\\n"
                            f"Активирован промокод: <b>{promo.get('code', '')}</b>"
                        ),
                        parse_mode="HTML"
                    )
            except Exception as exc:
                logging.warning("Unable to process referral bonus notification: %s", exc)

            try:
                await message.bot.send_message(
                    chat_id=ref_id,
                    text=(
                        "🎉 <b>Новый реферал!</b>\\n\\n"
                        "По вашей ссылке зарегистрировался новый пользователь.\\n"
                        "Бонусы начисляются согласно условиям реферальной программы."
                    ),
                    parse_mode="HTML"
                )
            except Exception as exc:
                logging.info("Referral notification was not delivered: %s", exc)

    welcome_text = get_setting('text_welcome') or (
        "🎉 <b>Добро пожаловать в VPN Bot!</b>\n\n"
        "Мы выдаём VPN-ключи для доступа к быстрым и безопасным серверам на протоколе VLESS.\n"
        "Просто вставьте ключ в приложение.\n\n"
        "Меню находится в клавиатуре (≡) — выберите нужный раздел или сразу установите VPN."
    )

    # The welcome itself carries the permanent keyboard. Do not send a
    # second service message or a redundant inline main-menu button.
    await render_home(message,welcome_text,
                      keyboard=get_main_keyboard(message.from_user.id),
                      parse_mode="HTML", disable_web_page_preview=True)

@router.callback_query(F.data == "start_menu")
async def start_menu(callback: types.CallbackQuery):
    await render_screen(callback.message, 
        "Главное меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

# ============ ОСНОВНЫЕ КНОПКИ ============

@router.callback_query(F.data == "paid_key_guide")
async def paid_key_guide(callback: types.CallbackQuery):
    await callback.answer()
    text = get_setting('text_install_vpn') or "Выберите ваше устройство:"
    await render_screen(callback.message,text,reply_markup=get_device_keyboard(),parse_mode="HTML",disable_web_page_preview=True)


@router.message(Command("install"))
@router.message(F.text == "📥 Установить VPN")
async def install_vpn(message: types.Message):
    text = get_setting('text_install_vpn') or "Выберите ваше устройство:"
    await render_command(message,text, keyboard=get_main_keyboard(message.from_user.id), reply_markup=get_device_keyboard(), parse_mode="HTML", disable_web_page_preview=True)

@router.message(Command("buy"))
@router.message(F.text == "📋 Купить ключ")
async def buy_key(message: types.Message):
    """Кнопка 'Купить ключ' — показывает тарифы"""
    await render_command(message,"Выберите подходящий тариф:", keyboard=get_main_keyboard(message.from_user.id), reply_markup=get_tariffs_keyboard(), parse_mode="HTML")

@router.message(Command("help"))
@router.message(Command("faq"))
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
        "Да, в пределах лимита выбранного тарифа.\n\n"
        "6️⃣ <b>Как получить бонус за приглашение?</b>\n"
        "Используйте реферальную ссылку из раздела «🎁 Пригласить друга»"
    )

    kb = get_back_keyboard("back_to_main")
    await render_command(message,faq_text, keyboard=get_main_keyboard(message.from_user.id), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@router.message(Command("referral"))
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
    await render_command(message,invite_text, keyboard=get_main_keyboard(message.from_user.id), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@router.message(Command("admin"))
@router.message(F.text == "🔧 Админ-панель")
async def admin_panel_button(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён", disable_web_page_preview=True)
        return
    await render_command(message,
        "🔧 <b>Админ-панель</b>\n\nВыберите раздел для управления:",
        keyboard=get_main_keyboard(message.from_user.id),
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@router.message(F.text == "/menu")
async def refresh_menu(message: types.Message):
    await render_home(message,"Главное меню VPN4US",
                      keyboard=get_main_keyboard(message.from_user.id))

# ============ ОБРАБОТКА УСТРОЙСТВ ============

@router.callback_query(lambda c: c.data.startswith("device_") and not c.data.startswith("select_device_"))
async def device_instruction(callback: types.CallbackQuery):
    logging.info(f"=== DEVICE INSTRUCTION: {callback.data} ===")

    device = callback.data.removeprefix("device_")
    await render_install_choices(callback, device)


@router.callback_query(F.data.startswith("install_select_"))
async def install_select_back(callback: types.CallbackQuery):
    await render_install_choices(callback, callback.data.removeprefix("install_select_"), force_select=True)


async def render_install_choices(callback, device, *, force_select=False):
    device_names = {
        'android': 'Android',
        'ios': 'iOS',
        'windows': 'Windows'
    }
    if device not in device_names:
        await callback.answer("Неизвестное устройство", show_alert=True)
        return
    device_name = device_names[device]

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
        await render_screen(callback.message, 
            "❌ У вас нет активной подписки.\n\n"
            "Пожалуйста, выберите тариф в разделе «📋 Купить ключ» и оплатите подписку.",
            reply_markup=get_tariffs_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Если подписка одна - сразу показываем инструкцию
    if len(subs) == 1 and not force_select:
        sub_id, email, end_date, tariff_name, xui_client_uid = subs[0]
        await show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid)
        return

    # Если несколько подписок - показываем выбор
    buttons = []
    for sub in subs:
        sub_id, email, end_date, tariff_name, xui_client_uid = sub
        _, date_label, remaining = expiry_presentation(end_date)
        buttons.append([types.InlineKeyboardButton(
            text=f"📦 {tariff_name} (до {date_label}, {remaining.lower()})",
            callback_data=f"select_device_{device}_{sub_id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_devices")])

    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await render_screen(callback.message, 
        f"📱 <b>Вы выбрали {device_name}</b>\n\n"
        "Выберите, для какой подписки показать инструкцию:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("select_device_"))
async def device_key_selected(callback: types.CallbackQuery):
    """Обработчик выбора ключа для устройства"""
    logging.info(f"=== DEVICE KEY SELECTED: {callback.data} ===")

    try:
        _, _, device, raw_id = callback.data.split("_")
        sub_id = int(raw_id)
        if device not in ("android", "ios", "windows") or sub_id <= 0:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

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
          AND s.is_active = 1 AND datetime(s.end_date) > datetime('now')
    ''', (sub_id, callback.from_user.id))
    sub = c.fetchone()
    conn.close()

    if not sub:
        await render_screen(callback.message, "❌ Подписка не найдена", 
                            reply_markup=get_back_keyboard(f"install_select_{device}"), parse_mode="HTML")
        await callback.answer()
        return

    email, end_date, tariff_name, xui_client_uid = sub

    await show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid,
                                   back_to=f"install_select_{device}")

async def show_instruction_for_key(callback, device, device_name, sub_id, email, tariff_name, xui_client_uid,
                                   *, back_to="back_to_devices"):
    """Показывает инструкцию для выбранного ключа с возвратом в исходный экран."""

    instructions = {
        'android': get_setting('text_android_instruction') or (
            "📱 <b>Настройка для Android</b>\n\n"
            "1. Установите Incy из Google Play\n"
            "2. Скопируйте ссылку ниже и добавьте подписку в Incy\n"
            "3. Подтвердите импорт подписки\n"
            "4. Нажмите «Подключиться»"
        ),
        'ios': get_setting('text_ios_instruction') or (
            "🍏 <b>Настройка для iOS</b>\n\n"
            "1. Установите Incy из App Store\n"
            "2. Скопируйте ссылку ниже и добавьте подписку в Incy\n"
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

    # The source of truth is the subscription's stored URL, not a random
    # "best" server. Never invent or reconstruct links on a shared panel.
    link = await vpn_service.get_subscription_link(sub_id)
    if not isinstance(link, str) or not link.startswith("https://"):
        await render_screen(callback.message, 
            "Не удалось получить ссылку подписки. Откройте «Мои ключи» "
            "или обратитесь в поддержку — новый ключ создавать не нужно.",
            reply_markup=get_back_keyboard(back_to),
        )
        await callback.answer()
        return

    text = instructions.get(device, instructions['android'])
    # Older configured instructions may still reference the removed deep link.
    text = text.replace("Нажмите «Добавить в 1 клик» ниже",
                        "Скопируйте ссылку ниже и добавьте подписку в Incy")
    text = f"📦 <b>Тариф: {escape(tariff_name)}</b>\n\n" + text

    if device == 'windows':
        text += f"\n\n🔗 <b>Ваша ссылка:</b>\n<code>{escape(link)}</code>"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data=back_to)]
        ])
    else:
        if device == 'ios':
            app_store_link = "https://apps.apple.com/ru/app/incy/id6756943388"
            button_text = "📲 Скачать Incy (App Store)"
        else:
            app_store_link = "https://play.google.com/store/apps/details?id=llc.itdev.incy"
            button_text = "📲 Скачать Incy (Google Play)"

        text += f"\n\n🔗 <b>Ваша ссылка для подключения:</b>\n<code>{escape(link)}</code>"
        text += "\n\n👆 Коснитесь ссылки, чтобы скопировать её в буфер обмена"

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=button_text, url=app_store_link)],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data=back_to)]
        ])

    await render_screen(callback.message, 
        text,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_devices")
async def back_to_devices(callback: types.CallbackQuery):
    """Возврат к выбору устройства"""
    await render_screen(callback.message, 
        "Выберите ваше устройство:",
        reply_markup=get_device_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_keys_from_install")
async def back_to_keys_from_install(callback: types.CallbackQuery):
    """У старой кнопки нет данных об устройстве; возвращаем в установку."""
    await back_to_devices(callback)

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await render_screen(callback.message, 
        "Главное меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

# ============ ОБРАБОТКА ПРОМОКОДОВ (ИСПРАВЛЕНА) ============

async def promo_input_error(message, state, error):
    from handlers.profile import PROMO_PROMPT
    result = await render_command(message, error + "\n\n" + PROMO_PROMPT,
        reply_markup=get_back_keyboard("promo_achievements"), parse_mode="HTML")
    mid = getattr(result, "message_id", None)
    if mid is not None:
        await state.update_data({FORM_SCREEN: mid})


@router.message(EnterPromocode.waiting)
async def process_promocode_input(message: types.Message, state: FSMContext):
    if not await current_form_input(message, state, PROMO_STATE):
        await state.clear()
        await message.answer("Ввод завершён. Откройте промокоды заново.")
        return
    code = (message.text or "").strip().upper()
    if code.startswith("/") or code in ("ОТМЕНА", "CANCEL"):
        from handlers.profile import promo_menu_content
        await state.clear()
        text, kb = promo_menu_content(message.from_user.id)
        await render_command(message, "Ввод промокода отменён.\n\n" + text,
                             reply_markup=kb, parse_mode="HTML", clean_cancel=True)
        return

    # Keep the existing manual-discount lifecycle in this UI change.
    # The two writes are now committed together; checkout reservation and
    # consumption of discounts belong to a separate financial change.
    owner = message.from_user.id
    error = None
    discount_text = None
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute("""
            SELECT id, discount_percent, discount_amount, valid_until, max_uses, used_count
            FROM promocodes WHERE code = ? AND is_active = 1
        """, (code,))
        promo = c.fetchone()
        if not promo:
            error = "❌ Промокод не найден или неактивен"
        else:
            pid, percent, amount, valid_until, max_uses, used_count = promo
            if valid_until and datetime.now() > datetime.fromisoformat(valid_until):
                error = "❌ Срок действия промокода истёк"
            elif used_count >= max_uses:
                error = "❌ Промокод уже использован максимальное количество раз"
            elif c.execute("SELECT id FROM promocode_uses WHERE promocode_id=? AND user_id=?", (pid, owner)).fetchone():
                error = "❌ Вы уже использовали этот промокод"
            elif percent > 0:
                value, discount_text = f"{percent}|0", f"{percent}%"
            elif amount > 0:
                value, discount_text = f"0|{amount}", f"{amount} ₽"
            else:
                error = "❌ Промокод не содержит скидку"
            if error is None:
                c.execute("INSERT INTO promocode_uses (promocode_id,user_id) VALUES (?,?)", (pid, owner))
                c.execute("UPDATE promocodes SET used_count=used_count+1 WHERE id=?", (pid,))
                c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                          (f"promocode_user_{owner}", value))
    if error is not None:
        await promo_input_error(message, state, error)
        return
    await state.clear()
    await render_command(message,
        f"✅ <b>Промокод активирован!</b>\n\n🎁 Скидка: <b>{discount_text}</b>",
        reply_markup=get_back_keyboard("promo_achievements"), parse_mode="HTML")
