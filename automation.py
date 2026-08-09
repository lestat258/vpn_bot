#!/usr/bin/env python3
"""
Модуль автоматизации:
- Авто-уведомления об истечении подписки
- Авто-продление подписки
"""

import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot
from database import get_setting

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Automation')

DB_PATH = '/opt/vpn-bot/data.db'

class AutomationManager:
    def __init__(self):
        self.db_path = DB_PATH
        self.bot = None
        self.running = False
    
    async def init_bot(self):
        """Инициализация бота"""
        if not self.bot:
            token = get_setting('bot_token')
            if token:
                self.bot = Bot(token=token)
                logger.info("✅ Бот инициализирован для авто-уведомлений")
            else:
                logger.error("❌ Токен бота не найден")
    
    async def check_expiring_subscriptions(self):
        """Проверяет подписки, которые истекают"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Подписки, которые истекают через 3 дня
        c.execute('''
            SELECT s.id, s.telegram_id, s.end_date, t.name, u.first_name
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1 
            AND datetime(s.end_date) > datetime('now')
            AND datetime(s.end_date) <= datetime('now', '+3 days')
            AND s.id NOT IN (
                SELECT subscription_id FROM notifications 
                WHERE type = 'expiring_3days' 
                AND sent_at > datetime('now', '-1 day')
            )
        ''')
        expiring_3days = c.fetchall()
        
        # Подписки, которые истекают через 1 день
        c.execute('''
            SELECT s.id, s.telegram_id, s.end_date, t.name, u.first_name
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1 
            AND datetime(s.end_date) > datetime('now')
            AND datetime(s.end_date) <= datetime('now', '+1 day')
            AND s.id NOT IN (
                SELECT subscription_id FROM notifications 
                WHERE type = 'expiring_1day' 
                AND sent_at > datetime('now', '-1 day')
            )
        ''')
        expiring_1day = c.fetchall()
        
        # Подписки, которые истекают сегодня
        c.execute('''
            SELECT s.id, s.telegram_id, s.end_date, t.name, u.first_name
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1 
            AND date(s.end_date) = date('now')
            AND s.id NOT IN (
                SELECT subscription_id FROM notifications 
                WHERE type = 'expiring_today' 
                AND sent_at > datetime('now', '-1 day')
            )
        ''')
        expiring_today = c.fetchall()
        
        # Подписки, которые уже истекли (вчера)
        c.execute('''
            SELECT s.id, s.telegram_id, s.end_date, t.name, u.first_name
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1 
            AND date(s.end_date) = date('now', '-1 day')
            AND s.id NOT IN (
                SELECT subscription_id FROM notifications 
                WHERE type = 'expired_yesterday' 
                AND sent_at > datetime('now', '-1 day')
            )
        ''')
        expired_yesterday = c.fetchall()
        
        conn.close()
        
        return {
            '3days': expiring_3days,
            '1day': expiring_1day,
            'today': expiring_today,
            'yesterday': expired_yesterday
        }
    
    async def send_notification(self, telegram_id, text, parse_mode="HTML"):
        """Отправляет уведомление пользователю"""
        try:
            await self.bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode=parse_mode
            )
            logger.info(f"✅ Уведомление отправлено пользователю {telegram_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления {telegram_id}: {e}")
            return False
    
    async def process_expiring_subscriptions(self):
        """Обрабатывает подписки, которые истекают"""
        await self.init_bot()
        if not self.bot:
            return
        
        expiring = await self.check_expiring_subscriptions()
        
        # Уведомление за 3 дня
        for sub in expiring['3days']:
            sub_id, telegram_id, end_date, tariff_name, first_name = sub
            days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
            text = (
                f"⏰ <b>Напоминание!</b>\n\n"
                f"Ваша подписка «{tariff_name}» истекает через {days_left} дня.\n"
                f"📅 Дата окончания: {end_date[:10]}\n\n"
                f"Чтобы продлить подписку, выберите тариф в разделе «📋 Тарифы»."
            )
            await self.send_notification(telegram_id, text)
            await self.save_notification(sub_id, 'expiring_3days')
        
        # Уведомление за 1 день
        for sub in expiring['1day']:
            sub_id, telegram_id, end_date, tariff_name, first_name = sub
            text = (
                f"⚠️ <b>Подписка истекает завтра!</b>\n\n"
                f"Ваша подписка «{tariff_name}» истекает завтра.\n"
                f"📅 Дата окончания: {end_date[:10]}\n\n"
                f"Пожалуйста, продлите подписку, чтобы не потерять доступ."
            )
            await self.send_notification(telegram_id, text)
            await self.save_notification(sub_id, 'expiring_1day')
        
        # Уведомление в день истечения
        for sub in expiring['today']:
            sub_id, telegram_id, end_date, tariff_name, first_name = sub
            text = (
                f"🔴 <b>Подписка истекает сегодня!</b>\n\n"
                f"Ваша подписка «{tariff_name}» истекает сегодня.\n"
                f"📅 Дата окончания: {end_date[:10]}\n\n"
                f"<b>Срочно!</b> Продлите подписку, чтобы не потерять доступ к VPN."
            )
            await self.send_notification(telegram_id, text)
            await self.save_notification(sub_id, 'expiring_today')
        
        # Уведомление об истечении вчера
        for sub in expiring['yesterday']:
            sub_id, telegram_id, end_date, tariff_name, first_name = sub
            text = (
                f"❌ <b>Подписка истекла!</b>\n\n"
                f"Ваша подписка «{tariff_name}» истекла вчера.\n"
                f"📅 Дата окончания: {end_date[:10]}\n\n"
                f"Доступ к VPN отключён.\n"
                f"Чтобы восстановить доступ, выберите тариф в разделе «📋 Тарифы»."
            )
            await self.send_notification(telegram_id, text)
            await self.save_notification(sub_id, 'expired_yesterday')
    
    def save_notification(self, subscription_id, notification_type):
        """Сохраняет информацию об отправленном уведомлении"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            c.execute('''
                INSERT INTO notifications (subscription_id, type)
                VALUES (?, ?)
            ''', (subscription_id, notification_type))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка сохранения уведомления: {e}")
    
    async def auto_renew_subscriptions(self):
        """Авто-продление подписок (если включено)"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Проверяем, включено ли авто-продление
        c.execute("SELECT value FROM settings WHERE key = 'auto_renew_enabled'")
        auto_renew = c.fetchone()
        if not auto_renew or auto_renew[0] != 'true':
            conn.close()
            return
        
        # Подписки, которые истекают сегодня и у которых есть авто-продление
        c.execute('''
            SELECT s.id, s.telegram_id, s.tariff_id, s.end_date, t.price_rub
            FROM subscriptions s
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1 
            AND date(s.end_date) = date('now')
            AND s.auto_renew = 1
        ''')
        renewals = c.fetchall()
        conn.close()
        
        for sub in renewals:
            sub_id, telegram_id, tariff_id, end_date, price = sub
            
            # Проверяем, есть ли у пользователя баланс
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT balance FROM users WHERE telegram_id = ?', (telegram_id,))
            user = c.fetchone()
            conn.close()
            
            if user and user[0] >= price:
                # Списываем деньги
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute('UPDATE users SET balance = balance - ? WHERE telegram_id = ?', (price, telegram_id))
                
                # Продлеваем подписку
                new_end_date = datetime.fromisoformat(end_date) + timedelta(days=30)
                c.execute('''
                    UPDATE subscriptions 
                    SET end_date = ?, is_active = 1
                    WHERE id = ?
                ''', (new_end_date.isoformat(), sub_id))
                conn.commit()
                conn.close()
                
                # Уведомляем пользователя
                await self.init_bot()
                if self.bot:
                    await self.send_notification(
                        telegram_id,
                        f"✅ <b>Подписка продлена!</b>\n\n"
                        f"💰 С вашего баланса списано {price:.0f}₽\n"
                        f"📅 Новая дата окончания: {new_end_date.strftime('%d.%m.%Y')}\n\n"
                        f"Спасибо, что остаётесь с нами! 🚀"
                    )
                logger.info(f"✅ Подписка {sub_id} продлена для пользователя {telegram_id}")
            else:
                # Недостаточно средств
                await self.init_bot()
                if self.bot:
                    await self.send_notification(
                        telegram_id,
                        f"⚠️ <b>Недостаточно средств для продления!</b>\n\n"
                        f"На вашем балансе недостаточно средств для авто-продления.\n"
                        f"Пополните баланс в разделе «👤 Профиль»."
                    )
                logger.warning(f"⚠️ Недостаточно средств у пользователя {telegram_id} для продления")

    async def run_automation(self):
        """Запуск автоматизации"""
        if self.running:
            return
        
        self.running = True
        logger.info("🔄 Запуск автоматизации...")
        
        while self.running:
            try:
                # Проверяем подписки каждые 6 часов
                await self.process_expiring_subscriptions()
                
                # Проверяем авто-продление раз в день
                await self.auto_renew_subscriptions()
                
                # Ждём 6 часов
                await asyncio.sleep(21600)  # 6 часов
                
            except Exception as e:
                logger.error(f"❌ Ошибка в автоматизации: {e}")
                await asyncio.sleep(300)  # 5 минут

# Глобальный экземпляр
automation = AutomationManager()

def start_automation():
    """Запускает автоматизацию в фоне"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(automation.run_automation())

if __name__ == "__main__":
    start_automation()
