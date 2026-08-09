#!/usr/bin/env python3
"""
Webhook сервер для обработки платежей ЮKassa
"""
import logging
import json
import sqlite3
import asyncio
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, Dict, Any

from aiohttp import web
from aiogram import Bot, types

from database import get_setting
from services import vpn_service
from server_manager import server_manager
from tax_manager import tax_manager

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# КОНСТАНТЫ
# ============================================================

DB_PATH = '/opt/vpn-bot/data.db'

# ============================================================
# БАЗА ДАННЫХ (контекстный менеджер)
# ============================================================

@contextmanager
def get_db():
    """Контекстный менеджер для БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ============================================================
# АСИНХРОННЫЕ ОБЕРТКИ ДЛЯ БД
# ============================================================

async def async_get_db_query(query: str, params: tuple = ()) -> list:
    """Асинхронная обертка для запросов к БД"""
    def _sync_query():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchall()
    return await asyncio.to_thread(_sync_query)

async def async_db_execute(query: str, params: tuple = ()) -> int:
    """Асинхронная обертка для выполнения запросов к БД"""
    def _sync_execute():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            return c.lastrowid
    return await asyncio.to_thread(_sync_execute)

async def async_update_db(query: str, params: tuple = ()) -> bool:
    """Асинхронная обертка для обновления БД"""
    def _sync_update():
        with get_db() as conn:
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            return True
    return await asyncio.to_thread(_sync_update)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

async def get_active_subscription(telegram_id: int) -> Optional[Dict]:
    """Асинхронное получение активной подписки"""
    query = '''
        SELECT id, xui_client_uid, xui_email, end_date, tariff_id
        FROM subscriptions
        WHERE telegram_id = ? AND is_active = 1
        ORDER BY end_date DESC LIMIT 1
    '''
    rows = await async_get_db_query(query, (telegram_id,))
    return dict(rows[0]) if rows else None

async def get_tariff_info(tariff_id: int) -> Optional[Dict]:
    """Асинхронное получение информации о тарифе"""
    query = 'SELECT name, duration_days, traffic_gb, ip_limit FROM tariffs WHERE id = ?'
    rows = await async_get_db_query(query, (tariff_id,))
    return dict(rows[0]) if rows else None

# ============================================================
# ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ ПЛАТЕЖА
# ============================================================

async def process_payment_success(payment_id: str, data: Dict) -> bool:
    """Обработка успешного платежа с использованием vpn_service"""
    try:
        # Проверяем статус платежа
        query = 'SELECT status, telegram_id, tariff_id FROM payments WHERE payment_id = ?'
        rows = await async_get_db_query(query, (payment_id,))
        
        if not rows:
            logger.error(f"❌ Платеж {payment_id} не найден в БД")
            return False
        
        payment = dict(rows[0])
        
        # Idempotency: проверяем, не обработан ли уже платеж
        if payment['status'] == 'paid':
            logger.info(f"ℹ️ Платеж {payment_id} уже обработан")
            return True
        
        telegram_id = payment['telegram_id']
        tariff_id = payment['tariff_id']
        
        # Получаем информацию о тарифе
        tariff = await get_tariff_info(tariff_id)
        if not tariff:
            logger.error(f"❌ Тариф {tariff_id} не найден")
            return False
        
        tariff_name = tariff['name']
        days_to_add = tariff['duration_days']
        traffic_gb = tariff['traffic_gb'] or 0
        ip_limit = tariff['ip_limit'] or 3
        
        # Обновляем статус платежа
        await async_update_db(
            'UPDATE payments SET status = "paid" WHERE payment_id = ?',
            (payment_id,)
        )
        
        # Получаем metadata
        metadata = data.get('object', {}).get('metadata', {})
        action = metadata.get('action', 'renew')
        subscription_id = metadata.get('subscription_id')
        
        # Получаем токен бота
        bot_token = get_setting('bot_token')
        if not bot_token:
            logger.error("❌ BOT_TOKEN не найден")
            return False
        
        try:
            from encryption import decrypt
            bot_token = decrypt(bot_token)
        except:
            pass
        
        bot = Bot(token=bot_token)
        
        try:
            if action == 'new_key':
                # ===== СОЗДАНИЕ НОВОЙ ПОДПИСКИ =====
                logger.info(f"🆕 Создание нового ключа для {telegram_id}")
                
                result = await vpn_service.create_subscription(
                    telegram_id=telegram_id,
                    tariff_id=tariff_id,
                    days=days_to_add,
                    traffic_gb=traffic_gb,
                    ip_limit=ip_limit
                )
                
                if result['success']:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=f"✅ <b>Новая подписка активирована!</b>\n\n"
                             f"📦 Тариф: {tariff_name}\n"
                             f"📅 Действует до: {result['end_date'].strftime('%d.%m.%Y')}\n\n"
                             f"🔗 <b>Ссылка для подключения:</b>\n"
                             f"<code>{result['link']}</code>",
                        parse_mode="HTML"
                    )
                    logger.info(f"✅ Подписка создана для {telegram_id}")
                else:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=f"❌ <b>Ошибка активации подписки</b>\n\n"
                             f"Пожалуйста, обратитесь в поддержку: @vpn4us_support",
                        parse_mode="HTML"
                    )
                    logger.error(f"❌ Ошибка создания ключа: {result.get('error')}")
            
            else:  # renew
                # ===== ПРОДЛЕНИЕ ПОДПИСКИ =====
                logger.info(f"🔄 Продление подписки для {telegram_id}")
                
                # Если передан конкретный subscription_id - используем его
                if subscription_id:
                    # Проверяем, что подписка принадлежит пользователю
                    query = '''
                        SELECT id, end_date FROM subscriptions
                        WHERE id = ? AND telegram_id = ? AND is_active = 1
                    '''
                    rows = await async_get_db_query(query, (subscription_id, telegram_id))
                    
                    if rows:
                        # Продлеваем подписку
                        result = await vpn_service.extend_subscription(
                            subscription_id=subscription_id,
                            days_to_add=days_to_add,
                            tariff_id=tariff_id
                        )
                        
                        if result['success']:
                            # Получаем ссылку
                            link = await vpn_service.get_subscription_link(subscription_id)
                            
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=f"✅ <b>Подписка продлена!</b>\n\n"
                                     f"📦 Тариф: {tariff_name}\n"
                                     f"📅 Новая дата окончания: {result['new_end_date'].strftime('%d.%m.%Y')}\n"
                                     f"📊 +{days_to_add} дней добавлено к вашей подписке\n\n"
                                     f"🔗 <b>Ссылка для подключения:</b>\n"
                                     f"<code>{link or 'Недоступна'}</code>",
                                parse_mode="HTML"
                            )
                            logger.info(f"✅ Подписка {subscription_id} продлена для {telegram_id}")
                        else:
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=f"❌ <b>Ошибка продления подписки</b>\n\n"
                                     f"Пожалуйста, обратитесь в поддержку: @vpn4us_support",
                                parse_mode="HTML"
                            )
                            logger.error(f"❌ Ошибка продления: {result.get('error')}")
                    else:
                        # Подписка не найдена - создаем новую
                        logger.warning(f"⚠️ Подписка {subscription_id} не найдена, создаем новую")
                        result = await vpn_service.create_subscription(
                            telegram_id=telegram_id,
                            tariff_id=tariff_id,
                            days=days_to_add,
                            traffic_gb=traffic_gb,
                            ip_limit=ip_limit
                        )
                        if result['success']:
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=f"✅ <b>Новая подписка активирована!</b>\n\n"
                                     f"📦 Тариф: {tariff_name}\n"
                                     f"📅 Действует до: {result['end_date'].strftime('%d.%m.%Y')}\n\n"
                                     f"🔗 <b>Ссылка для подключения:</b>\n"
                                     f"<code>{result['link']}</code>",
                                parse_mode="HTML"
                            )
                else:
                    # Нет subscription_id - ищем активную подписку
                    active_sub = await get_active_subscription(telegram_id)
                    
                    if active_sub:
                        # Продлеваем активную подписку
                        result = await vpn_service.extend_subscription(
                            subscription_id=active_sub['id'],
                            days_to_add=days_to_add,
                            tariff_id=tariff_id
                        )
                        
                        if result['success']:
                            link = await vpn_service.get_subscription_link(active_sub['id'])
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=f"✅ <b>Подписка продлена!</b>\n\n"
                                     f"📦 Тариф: {tariff_name}\n"
                                     f"📅 Новая дата окончания: {result['new_end_date'].strftime('%d.%m.%Y')}\n\n"
                                     f"🔗 <b>Ссылка для подключения:</b>\n"
                                     f"<code>{link or 'Недоступна'}</code>",
                                parse_mode="HTML"
                            )
                    else:
                        # Нет активной подписки - создаем новую
                        logger.info(f"ℹ️ Нет активной подписки, создаем новую для {telegram_id}")
                        result = await vpn_service.create_subscription(
                            telegram_id=telegram_id,
                            tariff_id=tariff_id,
                            days=days_to_add,
                            traffic_gb=traffic_gb,
                            ip_limit=ip_limit
                        )
                        if result['success']:
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=f"✅ <b>Новая подписка активирована!</b>\n\n"
                                     f"📦 Тариф: {tariff_name}\n"
                                     f"📅 Действует до: {result['end_date'].strftime('%d.%m.%Y')}\n\n"
                                     f"🔗 <b>Ссылка для подключения:</b>\n"
                                     f"<code>{result['link']}</code>",
                                parse_mode="HTML"
                            )
        
        except Exception as e:
            logger.error(f"❌ Ошибка обработки платежа: {e}")
            # Уведомляем админа
            try:
                admin_id = get_setting('admin_id') or 812021055
                await bot.send_message(
                    chat_id=int(admin_id),
                    text=f"🚨 <b>Ошибка обработки платежа!</b>\n\n"
                         f"Payment ID: {payment_id}\n"
                         f"User ID: {telegram_id}\n"
                         f"Error: {str(e)[:200]}",
                    parse_mode="HTML"
                )
            except:
                pass
        
        finally:
            await bot.session.close()
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в process_payment_success: {e}")
        return False

# ============================================================
# ВЕБХУК
# ============================================================

async def handle_webhook(request):
    """Обработка вебхука от ЮKassa"""
    try:
        data = await request.json()
        logger.info(f"📨 Webhook received: {json.dumps(data, indent=2)}")
        
        event = data.get('event')
        if event != 'payment.succeeded':
            logger.info(f"ℹ️ Игнорируем событие: {event}")
            return web.Response(status=200, text='OK')
        
        payment_id = data.get('object', {}).get('id')
        if not payment_id:
            logger.error("❌ Нет payment_id в вебхуке")
            return web.Response(status=400, text='Bad Request')
        
        # Обрабатываем платеж
        success = await process_payment_success(payment_id, data)
        
        if success:
            return web.Response(status=200, text='OK')
        else:
            return web.Response(status=500, text='Processing failed')
    
    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON: {e}")
        return web.Response(status=400, text='Invalid JSON')
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return web.Response(status=500, text='Internal Server Error')

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    app = web.Application()
    app.router.add_post('/webhook/', handle_webhook)
    app.router.add_get('/health', lambda r: web.Response(text='OK'))
    
    logger.info("🚀 Webhook server started on port 8081")
    web.run_app(app, host='0.0.0.0', port=8081)
