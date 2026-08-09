"""
Модуль автоматического продления подписок через баланс
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from aiogram import Bot
from database import get_setting
from encryption import decrypt
from xui_client import XUIClient
from server_manager import server_manager

logger = logging.getLogger(__name__)
DB_PATH = '/opt/vpn-bot/data.db'

class AutoRenewManager:
    def __init__(self):
        self.bot = None
        self.db_path = DB_PATH
    
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
        """Получает подписки, которые нужно продлить (истекают сегодня)"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Подписки, которые истекают сегодня и у которых включено авто-продление
        c.execute('''
            SELECT 
                s.id, 
                s.telegram_id, 
                s.tariff_id, 
                s.end_date, 
                s.xui_email,
                t.price_rub,
                t.duration_days,
                t.name as tariff_name
            FROM subscriptions s
            JOIN tariffs t ON t.id = s.tariff_id
            WHERE s.is_active = 1
                AND s.auto_renew = 1
                AND date(s.end_date) <= date('now')
                AND date(s.end_date) > date('now', '-1 day')
        ''')
        subscriptions = c.fetchall()
        conn.close()
        
        return subscriptions
    
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
    
    def get_user_balance(self, telegram_id):
        """Получает баланс пользователя"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT balance FROM users WHERE telegram_id = ?', (telegram_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0
    
    def extend_subscription(self, subscription_id, new_end_date, tariff_id, xui_email):
        """Продлевает подписку"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            UPDATE subscriptions 
            SET end_date = ?, tariff_id = ?
            WHERE id = ?
        ''', (new_end_date.isoformat(), tariff_id, subscription_id))
        conn.commit()
        conn.close()
        
        # Обновляем в 3X-UI
        try:
            server = server_manager.get_best_server()
            if server:
                xui = XUIClient(url=server['url'], api_token=server['api_token'])
                xui.update_client_expiry(xui_email, int(new_end_date.timestamp() * 1000))
                logger.info(f"✅ Обновлён expiryTime в 3X-UI для {xui_email}")
        except Exception as e:
            logger.error(f"❌ Ошибка обновления 3X-UI: {e}")
        
        return True
    
    async def process_renewals(self):
        """Обрабатывает автоматическое продление подписок"""
        await self.init_bot()
        if not self.bot:
            logger.error("❌ Бот не инициализирован для авто-продления")
            return
        
        # Получаем подписки для продления
        subscriptions = self.get_subscriptions_for_renew()
        
        if not subscriptions:
            logger.info("ℹ️ Нет подписок для автоматического продления")
            return
        
        renewed_count = 0
        failed_count = 0
        
        for sub in subscriptions:
            sub_id, telegram_id, tariff_id, end_date, xui_email, price, duration_days, tariff_name = sub
            
            # Проверяем баланс пользователя
            balance = self.get_user_balance(telegram_id)
            
            if balance >= price:
                # Списываем деньги
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute('UPDATE users SET balance = balance - ? WHERE telegram_id = ?', (price, telegram_id))
                conn.commit()
                conn.close()
                
                # Продлеваем подписку
                new_end_date = datetime.fromisoformat(end_date) + timedelta(days=duration_days)
                self.extend_subscription(sub_id, new_end_date, tariff_id, xui_email)
                
                # Уведомляем пользователя
                try:
                    await self.bot.send_message(
                        chat_id=telegram_id,
                        text=f"✅ <b>Подписка автоматически продлена!</b>\n\n"
                             f"📦 Тариф: {tariff_name}\n"
                             f"💰 С баланса списано: {price:.0f} ₽\n"
                             f"📅 Новая дата окончания: {new_end_date.strftime('%d.%m.%Y')}\n"
                             f"💳 Остаток на балансе: {balance - price:.2f} ₽\n\n"
                             f"Спасибо, что остаётесь с нами! 🚀",
                        parse_mode="HTML"
                    )
                    logger.info(f"✅ Подписка {sub_id} продлена для пользователя {telegram_id}")
                    renewed_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления пользователя {telegram_id}: {e}")
                    renewed_count += 1
                    
            else:
                # Недостаточно средств - уведомляем пользователя
                try:
                    await self.bot.send_message(
                        chat_id=telegram_id,
                        text=f"⚠️ <b>Недостаточно средств для автоматического продления!</b>\n\n"
                             f"📦 Тариф: {tariff_name}\n"
                             f"💰 Нужно: {price:.0f} ₽\n"
                             f"💳 На балансе: {balance:.2f} ₽\n"
                             f"📅 Подписка истекает: {end_date[:10]}\n\n"
                             f"Пополните баланс в разделе «👤 Профиль» или продлите подписку вручную.",
                        parse_mode="HTML"
                    )
                    failed_count += 1
                    logger.warning(f"⚠️ Недостаточно средств у пользователя {telegram_id} для продления")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления пользователя {telegram_id}: {e}")
                    failed_count += 1
        
        logger.info(f"📊 Авто-продление: {renewed_count} успешно, {failed_count} не удалось")
        
        # Отправляем отчёт администратору
        if renewed_count > 0 or failed_count > 0:
            try:
                admin_id = int(get_setting('admin_id') or 812021055)
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=f"📊 <b>Отчёт по авто-продлению</b>\n\n"
                         f"✅ Продлено: {renewed_count}\n"
                         f"❌ Не удалось: {failed_count}\n"
                         f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"❌ Ошибка отправки отчёта админу: {e}")
    
    async def check_expiring_soon(self):
        """Проверяет подписки, которые истекут скоро, и предупреждает пользователей"""
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
