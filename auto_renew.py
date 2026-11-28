"""
Модуль автоматического продления подписок через баланс
"""
from runtime_config import DB_PATH
import sqlite3
import asyncio
import time
import logging
from datetime import datetime, timedelta
from aiogram import Bot
from database import get_setting
from encryption import decrypt
from xui_client import XUIClient
from auto_renewal import activated, enabled, candidates, process_one, review_count
from contextlib import closing
from balance_ledger import rubles

logger = logging.getLogger(__name__)

class AutoRenewManager:
    def __init__(self, db_path=DB_PATH, bot=None):
        self.bot = bot
        self.db_path = db_path
    
    async def init_bot(self):
        """Инициализация бота"""
        if not self.bot:
            token = get_setting('bot_token')
            if token:
                try:
                    # Расшифровываем токен
                    token = decrypt(token)
                    self.bot = Bot(token=token)
                    logger.info("✅ Бот инициализирован для авто-продления")
                except Exception as e:
                    logger.error(f"❌ Ошибка расшифровки токена: {e}")
                    # Если не получилось расшифровать, пробуем использовать как есть
                    self.bot = Bot(token=token)
                    logger.info("✅ Бот инициализирован с токеном (без расшифровки)")
    
    def get_subscriptions_for_renew(self):
        return candidates(self.db_path, time.time_ns() // 1000000)

    def get_subscriptions_expiring_soon(self, days=3):
        """Получает подписки, которые истекут через N дней (для предупреждений)"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT 
                s.id, 
                s.telegram_id, 
                s.tariff_id, 
                s.end_date, 
                s.xui_email,
                t.price_rub,
                t.duration_days,
                t.name as tariff_name,
                u.balance
            FROM subscriptions s
            JOIN tariffs t ON t.id = s.tariff_id
            JOIN users u ON u.telegram_id = s.telegram_id
            WHERE s.is_active = 1
                AND s.auto_renew = 1
                AND date(s.end_date) BETWEEN date('now') AND date('now', '+' || ? || ' days')
                AND date(s.end_date) > date('now')
        ''', (days,))
        subscriptions = c.fetchall()
        conn.close()
        
        return subscriptions
    
    async def process_renewals(self):
        """Run only explicitly enabled, journaled automatic renewals."""
        if not activated():
            return
        subscriptions = await asyncio.to_thread(self.get_subscriptions_for_renew)
        if not subscriptions:
            pending = await asyncio.to_thread(review_count, self.db_path)
            if pending:
                logger.warning("Auto renewal: %s operations awaiting reconciliation", pending)
            return

        await self.init_bot()
        if not self.bot:
            logger.warning("Automatic renewal skipped: notification bot unavailable")
            return

        renewed = failed = 0
        for candidate in subscriptions:
            try:
                new_end = await asyncio.to_thread(
                    process_one, self.db_path, candidate, XUIClient
                )
            except Exception as exc:
                failed += 1
                logger.warning("Automatic renewal %s not completed: %s",
                               candidate.subscription_id, type(exc).__name__)
                continue

            renewed += 1
            try:
                await self.bot.send_message(
                    chat_id=candidate.owner,
                    text=(
                        f"✅ Подписка №{candidate.subscription_id} автоматически продлена!\n\n"
                        f"С баланса списано: {rubles(candidate.price):.2f} ₽\n"
                        f"Новая дата окончания: {new_end:%d.%m.%Y %H:%M}"
                    )
                )
            except Exception as exc:
                # Notification failures must never repeat the renewal.
                logger.warning("Renewal notification %s not confirmed: %s",
                               candidate.subscription_id, type(exc).__name__)

        pending = await asyncio.to_thread(review_count, self.db_path)
        logger.info("Auto renewal: completed=%s, not completed=%s, review=%s",
                    renewed, failed, pending)
        try:
            admin_id = int(get_setting('admin_id'))
            await self.bot.send_message(
                chat_id=admin_id,
                text=f"Автопродление: успешно {renewed}, не завершено {failed}. "
                     f"Операций на сверке: {pending}. "
                     "Не повторяйте операции на сверке вручную."
            )
        except Exception as exc:
            logger.warning("Auto renewal admin report not confirmed: %s", type(exc).__name__)

    async def check_expiring_soon(self):
        """Проверяет подписки, которые истекут скоро, и предупреждает пользователей"""
        if not activated():
            return
        with closing(sqlite3.connect(self.db_path)) as db:
            if not enabled(db):
                return
        await self.init_bot()
        if not self.bot:
            return
        
        # Проверяем подписки, которые истекут через 3 дня
        subscriptions = self.get_subscriptions_expiring_soon(3)
        
        for sub in subscriptions:
            sub_id, telegram_id, tariff_id, end_date, xui_email, price, duration_days, tariff_name, balance = sub
            
            days_left = (datetime.fromisoformat(end_date) - datetime.now()).days
            
            try:
                await self.bot.send_message(
                    chat_id=telegram_id,
                    text=f"🔔 <b>Напоминание о продлении</b>\n\n"
                         f"📦 Тариф: {tariff_name}\n"
                         f"⏳ Подписка истекает через {days_left} дн.\n"
                         f"💰 Стоимость продления: {price:.0f} ₽\n"
                         f"💳 На балансе: {balance:.2f} ₽\n\n"
                         f"Авто-продление включено. Убедитесь, что на балансе достаточно средств.\n"
                         f"Пополнить баланс: 👤 Профиль → 💰 Пополнить баланс",
                    parse_mode="HTML"
                )
                logger.info(f"✅ Напоминание отправлено пользователю {telegram_id}")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки напоминания пользователю {telegram_id}: {e}")

# Глобальный экземпляр
auto_renew_manager = AutoRenewManager()
