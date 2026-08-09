"""
Обработчики платежей и тарифов
"""
import logging
import sqlite3
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

DB_PATH = '/opt/vpn-bot/data.db'
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
            await callback.message.answer(
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
    active_subs = get_active_subscriptions(callback.from_user.id)
    logging.info(f"🔍 Найдено активных подписок: {len(active_subs)}")

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

        await callback.message.answer(
            f"📋 <b>{name}</b>\n\n"
            f"У вас уже есть активная подписка.\n"
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

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.message.answer("❌ Тариф не найден", parse_mode="HTML")
        await callback.answer()
        return

    tariff_id, name, price, days, traffic_gb, ip_limit = tariff

    active_subs = get_active_subscriptions(callback.from_user.id)

    if not active_subs:
        await callback.message.answer(
            "❌ У вас нет активных подписок для продления.\n\n"
            "Вы можете купить новый ключ.",
            reply_markup=get_tariffs_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    buttons = []
    for sub in active_subs:
        sub_id, tariff_id_sub, xui_email, end_date, tariff_name_sub = sub
        days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
        status = "🔴" if days_left <= 0 else "🟢"
        buttons.append([types.InlineKeyboardButton(
            text=f"{status} {tariff_name_sub} (до {end_date[:10]}, {days_left} дн.)",
            callback_data=f"renew_choose_{tariff_id}_{sub_id}"
        )])

    buttons.append([types.InlineKeyboardButton(
        text="🔙 Назад",
        callback_data=f"tariff_{tariff_id}"
    )])

    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.answer(
        f"📋 <b>Выберите подписку для продления</b>\n\n"
        f"Выберите, какую из активных подписок вы хотите продлить:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

# ============================================================
# ОБРАБОТЧИК ВЫБОРА КОНКРЕТНОЙ ПОДПИСКИ
# ============================================================

@router.callback_query(F.data.startswith("renew_choose_"))
async def handle_renew_choose(callback: types.CallbackQuery):
    """Обработчик выбора конкретной подписки для продления"""
    parts = callback.data.split("_")
    tariff_id = int(parts[2])
    subscription_id = int(parts[3])

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.message.answer("❌ Тариф не найден", parse_mode="HTML")
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
        action='renew',
        subscription_id=subscription_id,
        back_to='back_to_renew_select'
    )

# ============================================================
# ОБРАБОТЧИК ПОКУПКИ НОВОГО КЛЮЧА
# ============================================================

@router.callback_query(F.data.startswith("new_key_"))
async def handle_new_key(callback: types.CallbackQuery):
    """Покупка нового ключа"""
    tariff_id = int(callback.data.split("_")[2])

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.message.answer("❌ Тариф не найден", parse_mode="HTML")
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
    discount_percent, discount_amount = get_promocode_discount(callback.from_user.id)
    applied_promo = get_applied_promocode(callback.from_user.id)

    final_price = price
    applied_discount = 0
    promo_code = None

    if applied_promo:
        final_price, applied_discount = apply_discount(price, applied_promo)
        promo_code = applied_promo['code']
        logging.info(f"💰 Применён промокод {promo_code}: скидка {applied_discount:.2f}₽")

    if discount_percent > 0:
        promo_discount = price * discount_percent / 100
        if promo_discount > applied_discount:
            final_price = price * (100 - discount_percent) / 100
            applied_discount = promo_discount
            promo_code = None
    elif discount_amount > 0:
        if discount_amount > applied_discount:
            final_price = max(0, price - discount_amount)
            applied_discount = discount_amount
            promo_code = None

    logging.info(f"💰 Цена: {price}, Скидка: {discount_percent}% / {discount_amount}₽, Итог: {final_price:.2f}")

    try:
        payment = Payment.create({
            "amount": {"value": f"{final_price:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/lestat258_bot"},
            "capture": True,
            "description": f"VPN {name}" + (f" (скидка {applied_discount:.2f}₽)" if applied_discount > 0 else ""),
            "metadata": {
                "telegram_id": callback.from_user.id,
                "tariff_id": tariff_id,
                "action": action,
                "subscription_id": subscription_id if action == 'renew' else None,
                "original_price": price,
                "discount_amount": applied_discount,
                "discount_percent": discount_percent,
                "promocode": promo_code,
                "applied_promocode_id": applied_promo['id'] if applied_promo else None
            }
        })

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status) VALUES (?,?,?,?,?)',
                  (payment.id, callback.from_user.id, tariff_id, final_price, 'pending'))
        conn.commit()
        conn.close()

        set_setting(f'promocode_user_{callback.from_user.id}', '')

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"💳 Оплатить {final_price:.0f}₽", url=payment.confirmation.confirmation_url)],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data=back_to)]
        ])

        await callback.message.answer(
            f"📋 <b>{name}</b>\n"
            f"💰 {price:.0f}₽"
            + (f" → <b>{final_price:.0f}₽</b> (скидка {discount_percent}%)" if discount_percent > 0 or discount_amount > 0 else "")
            + f"\n📅 {days} дней\n\n"
            "Нажмите кнопку для оплаты:",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Payment error: {e}")
        await callback.message.answer(
            f"❌ Ошибка при создании платежа: {e}\n\n"
            "Попробуйте позже или обратитесь в поддержку.",
            parse_mode="HTML"
        )

    await callback.answer()

# ============================================================
# ПРОДЛЕНИЕ (ПРЯМОЙ ВЫЗОВ)
# ============================================================

@router.callback_query(F.data.startswith("renew_"))
async def handle_renew(callback: types.CallbackQuery):
    """Продление существующей подписки (прямой вызов)"""
    parts = callback.data.split("_")
    tariff_id = int(parts[1])
    subscription_id = int(parts[2])

    tariff = get_tariff_by_id(tariff_id)

    if not tariff:
        await callback.message.answer("❌ Тариф не найден", parse_mode="HTML")
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
        action='renew',
        subscription_id=subscription_id,
        back_to='back_to_renew_select'
    )

# ============================================================
# НАВИГАЦИЯ
# ============================================================

@router.callback_query(F.data == "back_to_tariffs")
async def back_to_tariffs(callback: types.CallbackQuery):
    """Возврат к списку тарифов"""
    await callback.message.delete()
    await callback.message.answer(
        "Выберите подходящий тариф:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="HTML"
    )
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

@router.callback_query(F.data == "back_to_renew_select")
async def back_to_renew_select(callback: types.CallbackQuery):
    """Возврат к выбору ключа для продления"""
    await callback.message.delete()
    await callback.message.answer(
        "Выберите подписку для продления:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()
