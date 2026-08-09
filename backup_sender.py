#!/usr/bin/env python3
"""
Отправка бэкапа в Telegram
"""
import os
import logging
import asyncio
from aiogram import Bot
from aiogram.types import FSInputFile
from database import get_setting
from encryption import decrypt
from backup_manager import backup_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def send_backup_to_admin():
    """Создаёт бэкап и отправляет администратору в Telegram"""
    bot_token = get_setting("bot_token")
    admin_id = get_setting("admin_id")

    logging.info(f"🔍 BOT_TOKEN: {'✅ есть' if bot_token else '❌ нет'}")
    logging.info(f"🔍 ADMIN_ID: {admin_id}")

    if not bot_token or not admin_id:
        logging.error("❌ BOT_TOKEN или ADMIN_ID не настроены")
        return False

    # Расшифровываем токен
    try:
        bot_token = decrypt(bot_token)
        logging.info("✅ Токен расшифрован")
    except Exception as e:
        logging.error(f"❌ Ошибка расшифровки токена: {e}")
        return False

    # Создаём бэкап
    logging.info("📦 Создание бэкапа...")
    result = backup_manager.create_backup()

    if not result.get("success"):
        logging.error(f"❌ Ошибка создания бэкапа: {result.get('error', 'Неизвестная ошибка')}")
        return False

    archive_path = result["archive"]
    logging.info(f"✅ Бэкап создан: {archive_path}")

    # Отправляем файл
    bot = Bot(token=bot_token)
    try:
        file_size = os.path.getsize(archive_path)
        logging.info(f"📦 Размер файла: {file_size / 1024:.1f} KB")

        document = FSInputFile(archive_path)

        await bot.send_document(
            chat_id=int(admin_id),
            document=document,
            caption=f"📦 <b>Бэкап создан</b>\n\n"
                    f"📅 Дата: {result['timestamp']}\n"
                    f"📦 Размер: {file_size / 1024:.1f} KB",
            parse_mode="HTML"
        )
        logging.info(f"✅ Бэкап отправлен админу {admin_id}")
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка отправки бэкапа: {e}")
        return False
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(send_backup_to_admin())
