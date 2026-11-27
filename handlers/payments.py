"""
Обработчики платежей и тарифов
"""
from runtime_config import DB_PATH
from screen_navigation import render_screen
from html import escape
from subscription_renewal import renewable_subscriptions, renewal_tariff, expiry_presentation
import logging
import asyncio
import sqlite3
from promo_checkout import create_checkout, request_key, check_open_checkout
from promo_reservations import ReservationBusy, ZeroPayable
from balance_ledger import rubles
from datetime import datetime, timedelta
from aiogram import Router, types, F, Bot

from database import get_setting, set_setting
from keyboards import get_tariffs_keyboard, get_main_keyboard
from services import vpn_service
from utils import (
    has_used_trial,
    get_active_subscriptions,
    get_tariff_by_id,
    get_promocode_discount,
    get_user_balance,
)
from yookassa import Payment
from server_manager import server_manager
from referral import referral_manager


ADMIN_ID = int(get_setting('admin_id') or 812021055)

router = Router()

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПРОМОКОДОВ
# ============================================================

def get_applied_promocode(user_id: int):
    """Получает применённый промокод пользователя"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, code, discount_percent, discount_amount
        FROM user_promocodes
        WHERE user_id = ? AND is_used = 0
        ORDER BY id ASC
        LIMIT 1
    ''', (user_id,))
    result = c.fetchone()
    conn.close()

    if result:
        return {
            'id': result[0],
            'code': result[1],
            'discount_percent': result[2],
            'discount_amount': result[3]
        }
    return None

def apply_discount(price: float, promocode: dict) -> tuple:
    """Применяет скидку к цене"""
    if not promocode:
        return price, 0

    discount = 0
    if promocode.get('discount_percent', 0) > 0:
        discount = price * promocode['discount_percent'] / 100
    elif promocode.get('discount_amount', 0) > 0:
        discount = min(promocode['discount_amount'], price)

    final_price = max(0, price - discount)
    return final_price, discount

# ============================================================
# ВОЗВРАТ К СПИСКУ КЛЮЧЕЙ
# ============================================================

@router.callback_query(F.data == "back_to_keys")
async def back_to_keys(callback: types.CallbackQuery):
    """Возврат к списку ключей"""
    from handlers.profile import show_my_keys
    await show_my_keys(callback)

# ============================================================
# ОБРАБОТКА ТАРИФОВ
# ============================================================

@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: types.CallbackQuery):
    tariff_id = int(callback.data.split("_")[1])

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.answer("Не найден")
        return

    tariff_id, name, price, days, traffic_gb, ip_limit = tariff

    # ============ ПРОВЕРКА НА ПРОБНЫЙ ТАРИФ ============
    if price == 0 and ('Пробный' in name or 'Тестовый' in name):
        if has_used_trial(callback.from_user.id):
            await render_screen(callback.message, 
                "❌ Вы уже использовали пробный доступ.\n\n"
                "Для продолжения выберите платный тариф в разделе «📋 Купить ключ».",
                reply_markup=get_tariffs_keyboard(),
                parse_mode="HTML"
            )
            await callback.answer()
            return

        await create_free_subscription(callback, tariff_id, name, days, traffic_gb, ip_limit)
        return

    # ============ ПРОВЕРКА НА АКТИВНУЮ ПОДПИСКУ ============
    active_subs = renewable_subscriptions(DB_PATH, callback.from_user.id)
    logging.info(f"🔍 Найдено ключей для продления: {len(active_subs)}")

    # ============ ПОКАЗЫВАЕМ ДВЕ КНОПКИ ============
    if active_subs and price > 0:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text="🔄 Продлить подписку",
                callback_data=f"renew_select_{tariff_id}"
            )],
            [types.InlineKeyboardButton(
                text="➕ Купить новый ключ (отдельный)",
                callback_data=f"new_key_{tariff_id}"
            )],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_tariffs")]
        ])

        await render_screen(callback.message, 
            f"📋 <b>{name}</b>\n\n"
            f"У вас уже есть ключ.\n"
            f"Выберите действие:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Если нет активной подписки — идём к оплате
    await process_payment(callback, tariff_id, name, price, days, traffic_gb, ip_limit, action='new_key', back_to='back_to_tariffs')

# ============================================================
# ОБРАБОТЧИК ВЫБОРА КЛЮЧА ДЛЯ ПРОДЛЕНИЯ
# ============================================================

@router.callback_query(F.data.startswith("renew_select_"))
async def handle_renew_select(callback: types.CallbackQuery):
    """Показывает список активных подписок для выбора, какую продлить"""
    tariff_id = int(callback.data.split("_")[2])

    tariff = renewal_tariff(DB_PATH, tariff_id)

    if not tariff:
        await render_screen(callback.message, "❌ Тариф не найден", parse_mode="HTML")
        await callback.answer()
        return

    tariff_id, name, price, days, traffic_gb, ip_limit = tariff

    active_subs = renewable_subscriptions(DB_PATH, callback.from_user.id)

    if not active_subs:
        await render_screen(callback.message, 
            "❌ У вас нет доступных подписок для продления.\n\n"
            "Вы можете купить новый ключ.",
            reply_markup=get_tariffs_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    buttons = []
    for sub in active_subs:
        sub_id, end_date, tariff_name_sub = sub
        current, date_label, _ = expiry_presentation(end_date)
        status = "🟢" if current else "🔴"
        buttons.append([types.InlineKeyboardButton(
            text=f"{status} №{sub_id} {tariff_name_sub} ({'до' if current else 'истёк'} {date_label})",
            callback_data=f"renew_choose_{tariff_id}_{sub_id}"
        )])

    buttons.append([types.InlineKeyboardButton(
        text="🔙 Назад",
        callback_data=f"tariff_{tariff_id}"
    )])

    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await render_screen(callback.message, 
        f"📋 <b>Выберите подписку для продления</b>\n\n"
        f"Выберите, какую подписку вы хотите продлить:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

# ============================================================
# ОБРАБОТЧИК ВЫБОРА КОНКРЕТНОЙ ПОДПИСКИ
# ============================================================

async def checkout_renewal(callback, *, direct):
    prefix = "extend_tariff_" if direct else "renew_choose_"
    try:
        a,b = callback.data.removeprefix(prefix).split("_")
        tariff_id,subscription_id = int(a),int(b)
        if min(tariff_id,subscription_id)<=0:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    tariff = renewal_tariff(DB_PATH, tariff_id)
    if not tariff or not renewable_subscriptions(DB_PATH,callback.from_user.id,subscription_id):
        await callback.answer("Продление недоступно. Выберите ключ и тариф заново.",show_alert=True)
        return
    await process_payment(callback,*tariff,action="renew",subscription_id=subscription_id,
        back_to=f"renew_from_key_{subscription_id}" if direct else f"renew_select_{tariff_id}")


@router.callback_query(F.data.startswith("extend_tariff_"))
async def handle_direct_renewal(callback: types.CallbackQuery):
    await checkout_renewal(callback,direct=True)


@router.callback_query(F.data.startswith("renew_choose_"))
async def handle_renew_choose(callback: types.CallbackQuery):
    await checkout_renewal(callback,direct=False)


# ============================================================
# ОБРАБОТЧИК ПОКУПКИ НОВОГО КЛЮЧА
# ============================================================

@router.callback_query(F.data.startswith("new_key_"))
async def handle_new_key(callback: types.CallbackQuery):
    """Покупка нового ключа"""
    tariff_id = int(callback.data.split("_")[2])

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await render_screen(callback.message, "❌ Тариф не найден", parse_mode="HTML")
        await callback.answer()
        return

    tariff_id, name, price, days, traffic_gb, ip_limit = tariff

    await process_payment(
        callback,
        tariff_id,
        name,
        price,
        days,
        traffic_gb,
        ip_limit,
        action='new_key',
        back_to='back_to_tariffs'
    )

# ============================================================
# СОЗДАНИЕ БЕСПЛАТНОЙ ПОДПИСКИ
# ============================================================

async def create_free_subscription(callback, tariff_id, name, days, traffic_gb, ip_limit):
    """Создаёт бесплатную пробную подписку"""
    payment_id = f"free_{callback.from_user.id}_{int(datetime.now().timestamp())}"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (payment_id, callback.from_user.id, tariff_id, 0, 'paid', datetime.now().isoformat()))
    conn.commit()
    conn.close()

    msg = await callback.message.answer(
        f"🎁 <b>{name}</b>\n\n"
        f"💰 Бесплатно\n"
        f"📅 {days} дней\n"
        f"📦 Трафик: {traffic_gb or '∞'} ГБ\n"
        f"📱 IP: {ip_limit or 3}\n\n"
        "⏳ Создаю пробную подписку...",
        parse_mode="HTML"
    )

    result = await vpn_service.create_subscription(
        telegram_id=callback.from_user.id,
        tariff_id=tariff_id,
        days=days,
        traffic_gb=traffic_gb,
        ip_limit=ip_limit
    )

    if result['success']:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📥 Установить VPN", callback_data="back_to_main")],
            [types.InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")]
        ])

        await msg.edit_text(
            f"✅ <b>Пробная подписка создана!</b>\n\n"
            f"📦 Тариф: {name}\n"
            f"📅 Действует до: {result['end_date'].strftime('%d.%m.%Y')}\n"
            f"📦 Трафик: {traffic_gb or '∞'} ГБ\n"
            f"📱 IP: {ip_limit or 3}\n\n"
            f"🔗 <b>Ссылка для подключения:</b>\n"
            f"<code>{result['link']}</code>\n\n"
            f"Нажмите «📥 Установить VPN» для инструкции.",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await msg.edit_text(
            f"❌ Не удалось создать пробную подписку.\n\n"
            f"Ошибка: {result.get('error', 'Неизвестная ошибка')}\n"
            f"Пожалуйста, обратитесь в поддержку: @vpn4us_support",
            parse_mode="HTML"
        )

    await callback.answer()

# ============================================================
# ОБРАБОТКА ПЛАТЕЖА
# ============================================================

async def process_payment(callback, tariff_id, name, price, days, traffic_gb, ip_limit, action='new_key', subscription_id=None, back_to='back_to_tariffs'):
    """Обрабатывает создание платежа"""
    target_text = ""
    if action == "renew":
        rows = renewable_subscriptions(DB_PATH, callback.from_user.id, subscription_id) if subscription_id is not None else []
        if not rows or renewal_tariff(DB_PATH, tariff_id) != (tariff_id,name,price,days,traffic_gb,ip_limit):
            await callback.answer("Продление недоступно", show_alert=True)
            return
        target_id, end_date, target_name = rows[0]
        current,date_label,_ = expiry_presentation(end_date)
        target_text = f"🔑 Продление ключа №{target_id}: {escape(target_name)} ({'до' if current else 'истёк'} {date_label})\n"
    try:
        username = (await callback.bot.get_me()).username
        if not username:
            raise RuntimeError("Telegram bot username unavailable")
        amount, discount, url = await asyncio.to_thread(
            create_checkout, DB_PATH, Payment, request_key(callback.bot.id, callback.id),
            callback.from_user.id, tariff_id, name, price, username,
            action=action, subscription_id=subscription_id)
        buttons = []
        if url:
            buttons.append([types.InlineKeyboardButton(text=f"💳 Оплатить {rubles(amount):.2f} ₽",url=url)])
        elif discount:
            buttons.append([types.InlineKeyboardButton(text="Проверить счёт",callback_data="promo_pending")])
        buttons.append([types.InlineKeyboardButton(text="🔙 Назад",callback_data=back_to)])
        kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        text = f"📋 <b>{escape(name)}</b>\n" + target_text + f"💰 {price:.2f} ₽"
        if discount:
            text += f" → <b>{rubles(amount):.2f} ₽</b> (скидка {rubles(discount):.2f} ₽)"
            text += "\nПромокод зарезервирован до подтверждения или отмены оплаты."
        text += f"\n📅 {days} дней\n\n"
        text += "Нажмите кнопку для оплаты:" if url else "Ссылка недоступна. Проверьте счёт или обратитесь в поддержку."
        await render_screen(callback.message,text,reply_markup=kb,parse_mode="HTML")
    except ReservationBusy:
        kb=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Проверить ожидающий счёт",callback_data="promo_pending")],
            [types.InlineKeyboardButton(text="🔙 Назад",callback_data=back_to)]])
        await render_screen(callback.message,"У вас уже есть счёт с зарезервированной скидкой.",reply_markup=kb)
    except ZeroPayable:
        await callback.answer("Нулевая сумма не поддерживается. Обратитесь в поддержку.",show_alert=True)
        return
    except Exception as exc:
        logging.error("Checkout requires review: %s",type(exc).__name__)
        kb=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Проверить ожидающий счёт",callback_data="promo_pending")],
            [types.InlineKeyboardButton(text="🔙 Назад",callback_data=back_to)]])
        await render_screen(callback.message,"Не удалось подтвердить создание счёта. Проверьте ожидающий счёт или обратитесь в поддержку.",reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "promo_pending")
async def promo_pending(callback: types.CallbackQuery):
    try:
        status,amount,url=await asyncio.to_thread(check_open_checkout,DB_PATH,Payment,callback.from_user.id)
    except Exception as exc:
        logging.warning("Discount checkout lookup failed: %s",type(exc).__name__)
        status,amount,url="review",None,None
    texts={
        "none":"Нет ожидающего счёта со скидкой.",
        "pending":"Счёт ожидает оплаты. Скидка зарезервирована.",
        "canceled":"ЮKassa подтвердила отмену. Резерв скидки снят; действующий промокод снова доступен.",
        "paid":"Оплата подтверждена ЮKassa. Ожидается обработка платежа.",
        "review":"Статус счёта требует сверки. Скидка не списана повторно. Обратитесь в поддержку.",
    }
    text=texts[status]
    buttons=[]
    if amount is not None:
        text+=f"\nСумма: {rubles(amount):.2f} ₽"
    if status=="pending" and url:
        buttons.append([types.InlineKeyboardButton(text="💳 Оплатить",url=url)])
    if status in ("pending","paid","review"):
        buttons.append([types.InlineKeyboardButton(text="Обновить статус",callback_data="promo_pending")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад",callback_data="promo_achievements")])
    await render_screen(callback.message,text,reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

# ============================================================
# ПРОДЛЕНИЕ (ПРЯМОЙ ВЫЗОВ)
# ============================================================

@router.callback_query(F.data.regexp(r"^renew_[0-9]+_[0-9]+$"))
async def handle_renew(callback: types.CallbackQuery):
    """Legacy explicit-ID buttons; never infer a target."""
    tariff_id,subscription_id=map(int,callback.data.split("_")[1:])
    tariff=renewal_tariff(DB_PATH,tariff_id)
    if not tariff or not renewable_subscriptions(DB_PATH,callback.from_user.id,subscription_id):
        await callback.answer("Продление недоступно",show_alert=True)
        return
    await process_payment(callback,*tariff,action="renew",subscription_id=subscription_id,
                          back_to=f"renew_from_key_{subscription_id}")


# ============================================================
# НАВИГАЦИЯ
# ============================================================

@router.callback_query(F.data == "back_to_tariffs")
async def back_to_tariffs(callback: types.CallbackQuery):
    """Возврат к списку тарифов"""
    await render_screen(callback.message, 
        "Выберите подходящий тариф:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await render_screen(callback.message, 
        "Главное меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_renew_select")
async def back_to_renew_select(callback: types.CallbackQuery):
    """Возврат к выбору ключа для продления"""
    await render_screen(callback.message, 
        "Выберите тариф, затем подписку для продления:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()
