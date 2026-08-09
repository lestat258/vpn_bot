#!/usr/bin/env python3
"""
Главный файл бота - регистрация хендлеров и запуск

Улучшения:
- Graceful shutdown
- Обработка сигналов
- Проверка на дублирование запуска
- Улучшенное логирование
- Перезапуск при ошибках
- Health check для мониторинга
"""
import asyncio
import logging
import sys
import signal
import os
from datetime import datetime
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

# Добавляем путь к проекту
sys.path.insert(0, '/opt/vpn-bot')

from database import init_db, get_setting, get_all_settings
from encryption import decrypt
from handlers import users, profile, payments, admin
from keyboards import get_main_keyboard
from yookassa import Configuration

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Настройка логирования в файл и консоль
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.FileHandler('/opt/vpn-bot/bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Отключаем шумные логи от сторонних библиотек
logging.getLogger('aiogram').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

class Config:
    """Конфигурация приложения"""
    DB_PATH = '/opt/vpn-bot/data.db'
    LOCK_FILE = '/opt/vpn-bot/bot.lock'
    PID_FILE = '/opt/vpn-bot/bot.pid'
    
    @classmethod
    def check_single_instance(cls):
        """Проверяет, что бот запущен только в одном экземпляре"""
        if os.path.exists(cls.PID_FILE):
            try:
                with open(cls.PID_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                # Проверяем, жив ли процесс
                try:
                    os.kill(old_pid, 0)
                    logger.error(f"❌ Бот уже запущен (PID: {old_pid})")
                    return False
                except OSError:
                    # Процесс мертв, можно удалить PID файл
                    os.remove(cls.PID_FILE)
            except (ValueError, IOError):
                os.remove(cls.PID_FILE)
        
        # Записываем текущий PID
        with open(cls.PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return True
    
    @classmethod
    def cleanup(cls):
        """Очистка временных файлов"""
        if os.path.exists(cls.PID_FILE):
            os.remove(cls.PID_FILE)

# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

# Проверка на дублирование запуска
if not Config.check_single_instance():
    sys.exit(1)

# Инициализация БД
try:
    init_db()
    logger.info("✅ База данных инициализирована")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации БД: {e}")
    sys.exit(1)

# Получение и расшифровка настроек
def get_decrypted_setting(key: str) -> str:
    """Получает и расшифровывает настройку"""
    value = get_setting(key)
    if not value:
        return None
    
    try:
        decrypted = decrypt(value)
        logger.info(f"✅ {key} расшифрован")
        return decrypted
    except Exception as e:
        logger.warning(f"ℹ️ {key} не зашифрован или ошибка: {e}")
        return value

BOT_TOKEN = get_decrypted_setting("bot_token")
SHOP_ID = get_setting("yookassa_shop_id")
SECRET_KEY = get_decrypted_setting("yookassa_secret_key")
ADMIN_ID = int(get_setting("admin_id") or 812021055)

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в БД! Бот не запустится.")
    sys.exit(1)

# Настройка ЮKassa
if SHOP_ID and SECRET_KEY:
    Configuration.account_id = SHOP_ID
    Configuration.secret_key = SECRET_KEY
    logger.info("✅ ЮKassa настроена")
else:
    logger.warning("⚠️ ЮKassa не настроена (нужен SHOP_ID и SECRET_KEY)")

# ============================================================
# СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА
# ============================================================

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ============================================================
# КОМАНДЫ БОТА
# ============================================================

async def set_commands():
    """Устанавливает команды для меню бота"""
    commands = [
        BotCommand(command="start", description="🚀 Запустить бота"),
        BotCommand(command="menu", description="📋 Показать меню"),
        BotCommand(command="profile", description="👤 Мой профиль"),
        BotCommand(command="buy", description="📋 Купить ключ"),
        BotCommand(command="install", description="📥 Установить VPN"),
        BotCommand(command="faq", description="❓ Вопросы"),
        BotCommand(command="referral", description="🎁 Пригласить друга"),
        BotCommand(command="help", description="🆘 Помощь"),
    ]
    
    if ADMIN_ID:
        commands.append(BotCommand(command="admin", description="🔧 Админ-панель"))
    
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("✅ Команды бота установлены")

# ============================================================
# ОБРАБОТЧИКИ
# ============================================================

def register_handlers():
    """Регистрирует все хендлеры"""
    dp.include_router(users.router)
    dp.include_router(profile.router)
    dp.include_router(payments.router)
    dp.include_router(admin.router)
    logger.info("✅ Все хендлеры зарегистрированы")

# ============================================================
# ОБРАБОТКА СИГНАЛОВ (GRACEFUL SHUTDOWN)
# ============================================================

shutdown_event = asyncio.Event()

def signal_handler(sig, frame):
    """Обработчик сигналов для корректного завершения"""
    logger.info(f"📡 Получен сигнал {sig}, завершаем работу...")
    shutdown_event.set()

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def on_startup():
    """Действия при запуске бота"""
    logger.info("=" * 50)
    logger.info("🚀 VPN Bot starting...")
    logger.info(f"📋 Admin ID: {ADMIN_ID}")
    logger.info(f"🕐 Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)
    
    # Устанавливаем команды
    await set_commands()
    
    # Отправляем уведомление админу о запуске
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🤖 <b>VPN Bot запущен!</b>\n\n"
                 f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                 f"📊 Статус: ✅ Работает",
            parse_mode="HTML"
        )
        logger.info("📨 Уведомление о запуске отправлено админу")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить уведомление админу: {e}")

async def on_shutdown():
    """Действия при завершении работы бота"""
    logger.info("🛑 VPN Bot shutting down...")
    
    # Отправляем уведомление админу о остановке
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🛑 <b>VPN Bot остановлен</b>\n\n"
                 f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="HTML"
        )
    except Exception:
        pass
    
    # Закрываем сессию бота
    await bot.session.close()
    logger.info("✅ Сессия бота закрыта")
    
    # Очищаем временные файлы
    Config.cleanup()
    logger.info("✅ Временные файлы очищены")
    logger.info("👋 Бот остановлен")

# ============================================================
# ЗАПУСК С ПЕРЕЗАПУСКАМИ
# ============================================================

async def run_bot_with_retry(max_retries=5, retry_delay=5):
    """Запускает бота с автоматическим перезапуском при ошибках"""
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # Запускаем бота
            await dp.start_polling(
                bot,
                on_startup=on_startup,
                on_shutdown=on_shutdown,
                skip_updates=True,  # Пропускаем старые обновления
                allowed_updates=["message", "callback_query", "inline_query"],
            )
            break  # Нормальное завершение
            
        except Exception as e:
            retry_count += 1
            logger.error(f"❌ Ошибка в polling (попытка {retry_count}/{max_retries}): {e}")
            
            if retry_count < max_retries:
                logger.info(f"⏳ Перезапуск через {retry_delay} секунд...")
                await asyncio.sleep(retry_delay)
            else:
                logger.error("❌ Превышено максимальное количество попыток перезапуска")
                raise

# ============================================================
# HEALTH CHECK (для мониторинга)
# ============================================================

async def health_check():
    """Периодическая проверка состояния бота (для мониторинга)"""
    import aiohttp
    
    # Создаем простой HTTP сервер для health check
    # Можно использовать отдельный порт для проверки
    try:
        # Проверяем, что бот может отправить сообщение
        me = await bot.get_me()
        logger.debug(f"✅ Health check: бот @{me.username} работает")
    except Exception as e:
        logger.error(f"❌ Health check провален: {e}")

# ============================================================
# MAIN
# ============================================================

def main():
    """Главная функция"""
    try:
        register_handlers()
        
        # Запускаем бота с перезапусками
        asyncio.run(run_bot_with_retry())
        
    except KeyboardInterrupt:
        logger.info("⏹️ Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        sys.exit(1)
    finally:
        Config.cleanup()

if __name__ == "__main__":
    main()
