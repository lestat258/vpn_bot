#!/usr/bin/env python3
"""
Модуль работы с базой данных
- Пул соединений через sqlite3 (с кэшированием)
- Миграции схемы
- Контекстный менеджер для безопасной работы
- Индексы для оптимизации запросов
"""
import sqlite3
import logging
import os
import time
from contextlib import contextmanager
from threading import Lock
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

DB_PATH = "/opt/vpn-bot/data.db"
SCHEMA_VERSION = 5  # Текущая версия схемы

# ============================================================
# ПУЛ СОЕДИНЕНИЙ (для sqlite3 с кэшированием)
# ============================================================

class ConnectionPool:
    """Простой пул соединений для sqlite3"""
    
    def __init__(self, max_connections: int = 10):
        self.max_connections = max_connections
        self._connections = []
        self._lock = Lock()
        self._stats = {
            'hits': 0,
            'misses': 0,
            'created': 0,
            'closed': 0
        }
    
    def get_connection(self):
        """Получает соединение из пула или создает новое"""
        with self._lock:
            if self._connections:
                conn = self._connections.pop()
                self._stats['hits'] += 1
                return conn
            
            self._stats['misses'] += 1
            self._stats['created'] += 1
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")  # Улучшает производительность
            conn.execute("PRAGMA synchronous = NORMAL")  # Баланс производительности и безопасности
            return conn
    
    def return_connection(self, conn):
        """Возвращает соединение в пул"""
        if conn is None:
            return
        
        with self._lock:
            if len(self._connections) < self.max_connections:
                self._connections.append(conn)
            else:
                conn.close()
                self._stats['closed'] += 1
    
    def get_stats(self) -> Dict:
        """Возвращает статистику пула"""
        with self._lock:
            return {
                'pool_size': len(self._connections),
                'max_size': self.max_connections,
                'hits': self._stats['hits'],
                'misses': self._stats['misses'],
                'created': self._stats['created'],
                'closed': self._stats['closed'],
                'hit_rate': round(self._stats['hits'] / max(1, self._stats['hits'] + self._stats['misses']) * 100, 2)
            }

# Глобальный пул
_pool = ConnectionPool(max_connections=10)

# ============================================================
# КОНТЕКСТНЫЙ МЕНЕДЖЕР
# ============================================================

@contextmanager
def get_db():
    """
    Контекстный менеджер для работы с БД.
    Автоматически возвращает соединение в пул.
    
    Использование:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users")
            return c.fetchall()
    """
    conn = _pool.get_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        _pool.return_connection(conn)

@contextmanager
def get_db_cursor():
    """
    Контекстный менеджер для работы с курсором.
    
    Использование:
        with get_db_cursor() as c:
            c.execute("SELECT * FROM users")
            return c.fetchall()
    """
    with get_db() as conn:
        cursor = conn.cursor()
        yield cursor

# ============================================================
# МИГРАЦИИ
# ============================================================

def get_current_schema_version():
    """Получает текущую версию схемы БД"""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'schema_version'")
            row = c.fetchone()
            if row:
                return int(row[0])
    except:
        pass
    return 0

def set_schema_version(version: int):
    """Устанавливает версию схемы БД"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version', ?)", (str(version),))
        conn.commit()

def migrate_v1_to_v2():
    """Миграция: добавление таблицы user_promocodes"""
    with get_db() as conn:
        c = conn.cursor()
        
        # Проверяем, существует ли таблица
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_promocodes'")
        if not c.fetchone():
            c.execute('''
                CREATE TABLE user_promocodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    promocode_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    discount_percent INTEGER DEFAULT 0,
                    discount_amount REAL DEFAULT 0,
                    is_used INTEGER DEFAULT 0,
                    used_at TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trigger_type TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (promocode_id) REFERENCES promocodes(id)
                )
            ''')
            c.execute('CREATE INDEX idx_user_promocodes_user ON user_promocodes(user_id)')
            c.execute('CREATE INDEX idx_user_promocodes_expires ON user_promocodes(expires_at)')
            c.execute('CREATE INDEX idx_user_promocodes_used ON user_promocodes(is_used)')
            logger.info("✅ Создана таблица user_promocodes")
    
    return True

def migrate_v2_to_v3():
    """Миграция: добавление колонок в users (referrer_id, balance, is_blocked)"""
    with get_db() as conn:
        c = conn.cursor()
        
        # Проверяем существующие колонки
        c.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in c.fetchall()]
        
        if 'referrer_id' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0')
            logger.info("✅ Добавлена колонка referrer_id")
        
        if 'balance' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0')
            logger.info("✅ Добавлена колонка balance")
        
        if 'is_blocked' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN is_blocked BOOLEAN DEFAULT 0')
            logger.info("✅ Добавлена колонка is_blocked")
    
    return True

def migrate_v3_to_v4():
    """Миграция: добавление таблиц для триггеров и достижений"""
    with get_db() as conn:
        c = conn.cursor()
        
        # user_trigger_progress
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_trigger_progress'")
        if not c.fetchone():
            c.execute('''
                CREATE TABLE user_trigger_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    current_value INTEGER DEFAULT 0,
                    target_value INTEGER NOT NULL,
                    is_completed INTEGER DEFAULT 0,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, trigger_type)
                )
            ''')
            c.execute('CREATE INDEX idx_trigger_progress_user ON user_trigger_progress(user_id)')
            c.execute('CREATE INDEX idx_trigger_progress_type ON user_trigger_progress(trigger_type)')
            logger.info("✅ Создана таблица user_trigger_progress")
        
        # Добавляем trigger_type в promocodes
        c.execute("PRAGMA table_info(promocodes)")
        columns = [col[1] for col in c.fetchall()]
        if 'trigger_type' not in columns:
            c.execute('ALTER TABLE promocodes ADD COLUMN trigger_type TEXT DEFAULT "manual"')
            logger.info("✅ Добавлена колонка trigger_type в promocodes")
        
        if 'trigger_params' not in columns:
            c.execute('ALTER TABLE promocodes ADD COLUMN trigger_params TEXT DEFAULT "{}"')
            logger.info("✅ Добавлена колонка trigger_params в promocodes")
        
        c.execute('CREATE INDEX IF NOT EXISTS idx_promocodes_trigger ON promocodes(trigger_type)')
    
    return True

def migrate_v4_to_v5():
    """Миграция: добавление sub_url в subscriptions и servers"""
    with get_db() as conn:
        c = conn.cursor()
        
        # subscriptions
        c.execute("PRAGMA table_info(subscriptions)")
        columns = [col[1] for col in c.fetchall()]
        
        if 'auto_renew' not in columns:
            c.execute('ALTER TABLE subscriptions ADD COLUMN auto_renew INTEGER DEFAULT 0')
            logger.info("✅ Добавлена колонка auto_renew в subscriptions")
        
        if 'sub_url' not in columns:
            c.execute('ALTER TABLE subscriptions ADD COLUMN sub_url TEXT')
            logger.info("✅ Добавлена колонка sub_url в subscriptions")
        
        # servers
        c.execute("PRAGMA table_info(servers)")
        columns = [col[1] for col in c.fetchall()]
        
        if 'sub_url' not in columns:
            c.execute('ALTER TABLE servers ADD COLUMN sub_url TEXT DEFAULT ""')
            logger.info("✅ Добавлена колонка sub_url в servers")
    
    return True

# Список всех миграций в порядке выполнения
MIGRATIONS = [
    ('v1_to_v2', migrate_v1_to_v2),
    ('v2_to_v3', migrate_v2_to_v3),
    ('v3_to_v4', migrate_v3_to_v4),
    ('v4_to_v5', migrate_v4_to_v5),
]

def run_migrations():
    """Запускает все необходимые миграции"""
    current_version = get_current_schema_version()
    logger.info(f"📊 Текущая версия схемы: {current_version}")
    
    if current_version >= SCHEMA_VERSION:
        logger.info("✅ Схема актуальна")
        return
    
    for i, (name, migration_func) in enumerate(MIGRATIONS, 1):
        if i > current_version:
            logger.info(f"🔄 Выполняется миграция: {name}")
            try:
                migration_func()
                set_schema_version(i)
                logger.info(f"✅ Миграция {name} выполнена")
            except Exception as e:
                logger.error(f"❌ Ошибка миграции {name}: {e}")
                raise
    
    # Обновляем до финальной версии
    set_schema_version(SCHEMA_VERSION)
    logger.info(f"✅ Схема обновлена до версии {SCHEMA_VERSION}")

# ============================================================
# БАЗОВЫЕ ФУНКЦИИ
# ============================================================

def init_db():
    """Инициализирует базу данных: создает таблицы и выполняет миграции"""
    try:
        # Создаем директорию, если нужно
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        
        # Проверяем, существует ли БД
        if not os.path.exists(DB_PATH):
            logger.info("📦 Создание новой базы данных...")
        
        # Создаем базовые таблицы
        _create_tables()
        
        # Заполняем настройки по умолчанию
        _init_default_settings()
        
        # Создаем индексы
        _create_indexes()
        
        # Запускаем миграции
        run_migrations()
        
        logger.info("✅ База данных инициализирована")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        raise

def _create_tables():
    """Создает все таблицы, если они не существуют"""
    with get_db() as conn:
        c = conn.cursor()
        
        # Основные таблицы
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price_rub REAL NOT NULL,
            duration_days INTEGER NOT NULL,
            traffic_gb REAL DEFAULT 0,
            ip_limit INTEGER DEFAULT 3,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            referrer_id INTEGER DEFAULT 0,
            balance REAL DEFAULT 0,
            is_blocked BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            tariff_id INTEGER NOT NULL,
            amount_rub REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            tariff_id INTEGER NOT NULL,
            xui_client_uid TEXT NOT NULL,
            xui_email TEXT NOT NULL,
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_date TIMESTAMP NOT NULL,
            traffic_used_gb REAL DEFAULT 0,
            is_active BOOLEAN DEFAULT 1,
            auto_renew INTEGER DEFAULT 0,
            sub_url TEXT
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            api_token TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            sub_url TEXT DEFAULT '',
            last_check TIMESTAMP,
            status TEXT DEFAULT 'unknown',
            load_cpu REAL DEFAULT 0,
            load_memory REAL DEFAULT 0,
            online_count INTEGER DEFAULT 0,
            total_traffic_gb REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_percent INTEGER DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            valid_until TIMESTAMP,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1,
            trigger_type TEXT DEFAULT 'manual',
            trigger_params TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS promocode_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promocode_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            source TEXT DEFAULT 'manual',
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (promocode_id) REFERENCES promocodes(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            ip TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            filter TEXT DEFAULT 'all',
            scheduled_at TIMESTAMP,
            sent_at TIMESTAMP,
            status TEXT DEFAULT 'pending',
            total_sent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS server_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            status TEXT,
            online_count INTEGER,
            load_cpu REAL,
            load_memory REAL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (server_id) REFERENCES servers(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS server_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            server_id INTEGER NOT NULL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY (server_id) REFERENCES servers(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS tax_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            receipt_uuid TEXT NOT NULL,
            receipt_url TEXT,
            amount REAL,
            description TEXT,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            request_count INTEGER DEFAULT 1,
            first_request TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_request TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_blocked INTEGER DEFAULT 0,
            blocked_until TIMESTAMP,
            UNIQUE(user_id, action_type)
        )''')
        
        conn.commit()

def _init_default_settings():
    """Инициализирует настройки по умолчанию"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM settings')
        if c.fetchone()[0] == 0:
            defaults = [
                ('admin_username', 'admin'),
                ('admin_password', 'admin'),
                ('first_login', 'true'),
                ('auto_renew_enabled', 'true'),
                ('tax_enabled', 'false'),
            ]
            for key, value in defaults:
                c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
            logger.info("✅ Настройки по умолчанию созданы")
        
        # Проверяем тарифы
        c.execute('SELECT COUNT(*) FROM tariffs')
        if c.fetchone()[0] == 0:
            c.execute('''
                INSERT INTO tariffs (name, price_rub, duration_days, traffic_gb, ip_limit) 
                VALUES (?, ?, ?, ?, ?)
            ''', ('1 месяц', 150, 30, 0, 3))
            logger.info("✅ Тариф по умолчанию создан")
        
        conn.commit()

def _create_indexes():
    """Создает все необходимые индексы для оптимизации"""
    with get_db() as conn:
        c = conn.cursor()
        
        indexes = [
            ('idx_promocodes_code', 'promocodes', 'code'),
            ('idx_promocode_uses_user', 'promocode_uses', 'user_id'),
            ('idx_admin_logs_admin', 'admin_logs', 'admin_id'),
            ('idx_broadcasts_status', 'broadcasts', 'status'),
            ('idx_server_status_server', 'server_status_history', 'server_id'),
            ('idx_server_assignments_sub', 'server_assignments', 'subscription_id'),
            ('idx_notifications_sub', 'notifications', 'subscription_id'),
            ('idx_notifications_type', 'notifications', 'type'),
            ('idx_tax_receipts_payment', 'tax_receipts', 'payment_id'),
            ('idx_tax_receipts_user', 'tax_receipts', 'user_id'),
            ('idx_rate_limits_user', 'rate_limits', 'user_id, action_type'),
            ('idx_subscriptions_telegram_id', 'subscriptions', 'telegram_id'),
            ('idx_subscriptions_end_date', 'subscriptions', 'end_date'),
            ('idx_subscriptions_is_active', 'subscriptions', 'is_active'),
            ('idx_payments_telegram_id', 'payments', 'telegram_id'),
            ('idx_payments_status', 'payments', 'status'),
            ('idx_payments_created_at', 'payments', 'created_at'),
            ('idx_users_telegram_id', 'users', 'telegram_id'),
            ('idx_users_referrer_id', 'users', 'referrer_id'),
            ('idx_servers_status', 'servers', 'status'),
            ('idx_servers_is_active', 'servers', 'is_active'),
        ]
        
        for idx_name, table, columns in indexes:
            try:
                c.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({columns})')
            except Exception as e:
                logger.warning(f"⚠️ Не удалось создать индекс {idx_name}: {e}")
        
        conn.commit()

# ============================================================
# ОСНОВНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С НАСТРОЙКАМИ
# ============================================================

def get_setting(key: str) -> Optional[str]:
    """Получает значение настройки"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = c.fetchone()
        return row[0] if row else None

def set_setting(key: str, value: str):
    """Устанавливает значение настройки"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

def get_all_settings() -> Dict[str, str]:
    """Возвращает все настройки"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings')
        rows = c.fetchall()
        return {row[0]: row[1] for row in rows}

# ============================================================
# СТАТИСТИКА ПУЛА СОЕДИНЕНИЙ
# ============================================================

def get_pool_stats() -> Dict:
    """Возвращает статистику пула соединений"""
    return _pool.get_stats()

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    print("📦 Инициализация базы данных...")
    init_db()
    
    print("📊 Статистика пула соединений:")
    print(get_pool_stats())
    
    print("\n✅ База данных готова к работе!")
