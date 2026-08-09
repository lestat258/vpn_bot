#!/usr/bin/env python3
"""
Модуль уведомлений для администратора и пользователей
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import get_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger('NotificationManager')

class NotificationManager:
    def __init__(self):
        self.bot_token = get_setting('bot_token')
        self.admin_id = int(get_setting('admin_id') or 812021055)
        self.bot = Bot(token=self.bot_token) if self.bot_token else None

    async def notify_admin(self, message, parse_mode="HTML"):
        """Отправляет уведомление администратору"""
        if not self.bot:
            logger.error("❌ Бот не настроен для уведомлений")
            return False

        try:
            await self.bot.send_message(
                chat_id=self.admin_id,
                text=f"🔔 <b>Уведомление</b>\n\n{message}",
                parse_mode=parse_mode
            )
            logger.info(f"✅ Уведомление отправлено админу")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления: {e}")
            return False

    def _notification_sent_today(self, subscription_id, notification_type):
        """Проверяет, отправлялось ли уведомление сегодня"""
        try:
            conn = sqlite3.connect('/opt/vpn-bot/data.db')
            c = conn.cursor()
            c.execute('''
                SELECT COUNT(*) FROM notifications
                WHERE subscription_id = ? AND type = ?
                AND datetime(sent_at) > datetime('now', '-1 day')
            ''', (subscription_id, notification_type))
            count = c.fetchone()[0]
            conn.close()
            return count > 0
        except Exception as e:
            logger.error(f"Ошибка проверки уведомления: {e}")
            return True  # Если ошибка — не отправляем

    def _mark_notification_sent(self, subscription_id, notification_type):
        """Отмечает уведомление как отправленное"""
        try:
            conn = sqlite3.connect('/opt/vpn-bot/data.db')
            c = conn.cursor()
            c.execute('''
                INSERT INTO notifications (subscription_id, type)
                VALUES (?, ?)
            ''', (subscription_id, notification_type))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка сохранения уведомления: {e}")

    async def check_expiring_subscriptions(self):
        """Проверяет подписки, истекающие скоро, и отправляет уведомления"""
        conn = sqlite3.connect('/opt/vpn-bot/data.db')
        c = conn.cursor()

        # Подписки, истекающие через 3 дня
        c.execute('''
            SELECT u.telegram_id, u.first_name, s.end_date, t.name, s.id
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1
            AND datetime(s.end_date) BETWEEN datetime('now') AND datetime('now', '+3 days')
        ''')
        expiring_3days = c.fetchall()

        # Подписки, истекающие через 1 день
        c.execute('''
            SELECT u.telegram_id, u.first_name, s.end_date, t.name, s.id
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1
            AND datetime(s.end_date) BETWEEN datetime('now') AND datetime('now', '+1 day')
        ''')
        expiring_1day = c.fetchall()

        # Подписки, истекшие сегодня
        c.execute('''
            SELECT u.telegram_id, u.first_name, s.end_date, t.name, s.id
            FROM subscriptions s
            JOIN users u ON u.telegram_id = s.telegram_id
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1
            AND date(s.end_date) = date('now')
        ''')
        expired_today = c.fetchall()

        conn.close()

        # Кнопка "Купить ключ"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Купить ключ", callback_data="back_to_tariffs")]
        ])

        # Отправляем уведомления пользователям (за 3 дня)
        for user_id, name, end_date, tariff, sub_id in expiring_3days:
            if self._notification_sent_today(sub_id, 'expiring_3days'):
                continue
            if self.bot:
                try:
                    days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"⏳ <b>Ваша подписка истекает через {days_left} дня!</b>\n\n"
                             f"📦 Тариф: {tariff}\n"
                             f"📅 До: {end_date[:10]}\n\n"
                             f"Продлите подписку, чтобы не потерять доступ.",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                    self._mark_notification_sent(sub_id, 'expiring_3days')
                    logger.info(f"✅ Уведомление (3 дня) отправлено пользователю {user_id}")
                except Exception as e:
                    logger.error(f"Ошибка отправки пользователю {user_id}: {e}")

        # Отправляем уведомления пользователям (за 1 день)
        for user_id, name, end_date, tariff, sub_id in expiring_1day:
            if self._notification_sent_today(sub_id, 'expiring_1day'):
                continue
            if self.bot:
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"🚨 <b>Ваша подписка истекает завтра!</b>\n\n"
                             f"📦 Тариф: {tariff}\n"
                             f"📅 До: {end_date[:10]}\n\n"
                             f"Продлите подписку прямо сейчас!",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                    self._mark_notification_sent(sub_id, 'expiring_1day')
                    logger.info(f"✅ Уведомление (1 день) отправлено пользователю {user_id}")
                except Exception as e:
                    logger.error(f"Ошибка отправки пользователю {user_id}: {e}")

        # Отправляем уведомления пользователям (истекли сегодня)
        for user_id, name, end_date, tariff, sub_id in expired_today:
            if self._notification_sent_today(sub_id, 'expired_today'):
                continue
            if self.bot:
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ <b>Ваша подписка истекла сегодня!</b>\n\n"
                             f"📦 Тариф: {tariff}\n"
                             f"📅 До: {end_date[:10]}\n\n"
                             f"Для продолжения использования необходимо продлить подписку.",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                    self._mark_notification_sent(sub_id, 'expired_today')
                    logger.info(f"✅ Уведомление (истекла сегодня) отправлено пользователю {user_id}")
                except Exception as e:
                    logger.error(f"Ошибка отправки пользователю {user_id}: {e}")

        # Отправляем сводку администратору (только если есть новые уведомления)
        notifications = []
        for data, label in [(expiring_3days, '3 дня'), (expiring_1day, '1 день'), (expired_today, 'сегодня')]:
            if data:
                msg = f"📋 <b>Подписки, истекающие через {label}:</b>\n"
                for user_id, name, end_date, tariff, sub_id in data:
                    msg += f"• {name or user_id} — {tariff} (до {end_date[:10]})\n"
                notifications.append(msg)

        for msg in notifications:
            await self.notify_admin(msg)

        return len(notifications) > 0

    async def check_system_health(self):
        """Проверяет здоровье системы"""
        issues = []

        # Проверяем базу данных
        try:
            conn = sqlite3.connect('/opt/vpn-bot/data.db')
            c = conn.cursor()
            c.execute('SELECT 1')
            conn.close()
        except Exception as e:
            issues.append(f"❌ Ошибка базы данных: {e}")

        # Проверяем серверы
        try:
            conn = sqlite3.connect('/opt/vpn-bot/data.db')
            c = conn.cursor()
            c.execute('SELECT name, status FROM servers WHERE is_active = 1 AND status != "online"')
            offline_servers = c.fetchall()
            conn.close()
            for name, status in offline_servers:
                issues.append(f"⚠️ Сервер {name} {status}")
        except Exception as e:
            issues.append(f"❌ Ошибка проверки серверов: {e}")

        if issues:
            msg = "🔄 <b>Проблемы с системой:</b>\n\n" + "\n".join(issues)
            await self.notify_admin(msg)
            return False

        return True

# Глобальный экземпляр
notification_manager = NotificationManager()

if __name__ == "__main__":
    import asyncio
    asyncio.run(notification_manager.check_expiring_subscriptions())
