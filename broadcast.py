"""
Модуль для рассылки сообщений
"""
import sqlite3
import asyncio
import logging
from aiogram import Bot
from database import get_setting
from encryption import decrypt

logger = logging.getLogger(__name__)

async def send_broadcast_with_text(text):
    """Отправляет рассылку с текстом"""
    bot_token = get_setting('bot_token')
    if not bot_token:
        logger.error("❌ Токен бота не настроен")
        return 0
    
    # Расшифровываем токен
    try:
        bot_token = decrypt(bot_token)
        logger.info("✅ Токен расшифрован")
    except Exception as e:
        logger.error(f"❌ Ошибка расшифровки токена: {e}")
        return 0
    
    bot = Bot(token=bot_token)
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT telegram_id FROM users WHERE is_blocked = 0')
    users = c.fetchall()
    conn.close()
    
    sent_count = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user[0], text=text, parse_mode="HTML")
            sent_count += 1
            await asyncio.sleep(0.05)  # Небольшая задержка
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user[0]}: {e}")
    
    await bot.session.close()
    logger.info(f"📨 Рассылка отправлена {sent_count} пользователям")
    return sent_count
