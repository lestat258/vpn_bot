#!/usr/bin/env python3
"""
Вспомогательные функции для тестов
"""
import sqlite3
import os
import tempfile


def create_test_db() -> str:
    """Создает временную БД с полной схемой"""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    db_path = temp_db.name
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
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
    
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('admin_username', 'admin'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('admin_password', 'admin'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('first_login', 'true'))
    
    conn.commit()
    conn.close()
    
    return db_path


def create_test_user(db_path: str, telegram_id: int = 12345, username: str = 'test_user', first_name: str = 'Test User'):
    """Создает тестового пользователя в БД"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)',
              (telegram_id, username, first_name))
    conn.commit()
    conn.close()
    return telegram_id


def create_test_tariff(db_path: str, name: str = 'Тестовый тариф', price: float = 100, 
                       days: int = 30, traffic: float = 10, ip_limit: int = 3) -> int:
    """Создает тестовый тариф в БД"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('INSERT INTO tariffs (name, price_rub, duration_days, traffic_gb, ip_limit) VALUES (?, ?, ?, ?, ?)',
              (name, price, days, traffic, ip_limit))
    tariff_id = c.lastrowid
    conn.commit()
    conn.close()
    return tariff_id
