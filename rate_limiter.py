"""
Модуль защиты от спама и брутфорса
Ограничивает количество запросов от пользователей
С поддержкой:
- Кэширования в памяти
- Фоновой очистки старых записей
- Настраиваемых лимитов
- Статистики
"""
import time
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)
DB_PATH = '/opt/vpn-bot/data.db'

# ============================================================
# НАСТРОЙКИ
# ============================================================

@dataclass
class RateLimitConfig:
    """Конфигурация лимитов для разных типов действий"""
    max_requests: int = 10
    time_window: int = 60  # секунд
    block_duration: int = 300  # секунд
    cleanup_interval: int = 3600  # секунд (1 час)


class RateLimiterConfig:
    """Глобальная конфигурация rate limiter'a"""
    
    def __init__(self):
        self.limits = {
            'command': RateLimitConfig(
                max_requests=10,
                time_window=60,
                block_duration=300
            ),
            'callback': RateLimitConfig(
                max_requests=30,
                time_window=60,
                block_duration=300
            ),
            'message': RateLimitConfig(
                max_requests=20,
                time_window=60,
                block_duration=600
            ),
            'payment': RateLimitConfig(
                max_requests=5,
                time_window=300,
                block_duration=1800
            ),
            'admin': RateLimitConfig(
                max_requests=100,
                time_window=60,
                block_duration=60
            ),
        }
    
    def get(self, action_type: str) -> RateLimitConfig:
        """Получает конфигурацию для типа действия"""
        return self.limits.get(action_type, self.limits['message'])


# ============================================================
# ОСНОВНОЙ КЛАСС
# ============================================================

class RateLimiter:
    """
    Защита от спама и брутфорса
    """
    
    def __init__(self):
        self.config = RateLimiterConfig()
        
        # Кэш в памяти для быстрого доступа
        # user_id -> {action_type -> data}
        self._cache: Dict[int, Dict[str, Dict]] = defaultdict(dict)
        self._cache_lock = threading.Lock()
        
        # Статистика
        self._stats = {
            'checks': 0,
            'allowed': 0,
            'blocked': 0,
            'cache_hits': 0,
            'cache_misses': 0,
        }
        self._stats_lock = threading.Lock()
        
        # Создаем таблицу
        self._init_db()
        
        # Запускаем фоновую очистку
        self._cleanup_task = None
        self._start_cleanup()
    
    # ============================================================
    # ИНИЦИАЛИЗАЦИЯ БД
    # ============================================================
    
    def _init_db(self):
        """Инициализирует таблицу для хранения лимитов"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS rate_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    request_count INTEGER DEFAULT 1,
                    first_request TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_request TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_blocked INTEGER DEFAULT 0,
                    blocked_until TIMESTAMP,
                    UNIQUE(user_id, action_type)
                )
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_rate_limits_user
                ON rate_limits(user_id, action_type)
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_rate_limits_blocked
                ON rate_limits(is_blocked, blocked_until)
            ''')
            conn.commit()
            conn.close()
            logger.info("✅ Таблица rate_limits создана/проверена")
        except Exception as e:
            logger.error(f"❌ Ошибка создания таблицы rate_limits: {e}")
    
    # ============================================================
    # ФОНОВАЯ ОЧИСТКА
    # ============================================================
    
    def _start_cleanup(self):
        """Запускает фоновую задачу очистки"""
        def cleanup_loop():
            while True:
                try:
                    time.sleep(self.config.get('command').cleanup_interval)
                    self._cleanup_old_records()
                except Exception as e:
                    logger.error(f"❌ Ошибка в фоновой очистке: {e}")
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()
        logger.info("✅ Фоновая очистка rate_limiter запущена")
    
    def _cleanup_old_records(self):
        """Очищает старые записи (старше 24 часов)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Удаляем старые неблокированные записи
            c.execute('''
                DELETE FROM rate_limits
                WHERE is_blocked = 0 
                AND last_request < datetime('now', '-1 day')
            ''')
            deleted = c.rowcount
            
            # Снимаем истекшие блокировки
            c.execute('''
                UPDATE rate_limits
                SET is_blocked = 0, blocked_until = NULL
                WHERE is_blocked = 1 
                AND blocked_until < datetime('now')
            ''')
            unblocked = c.rowcount
            
            conn.commit()
            conn.close()
            
            if deleted > 0 or unblocked > 0:
                logger.info(f"🧹 Очистка: удалено {deleted} записей, снято {unblocked} блокировок")
            
            # Очищаем кэш
            with self._cache_lock:
                self._cache.clear()
                
        except Exception as e:
            logger.error(f"❌ Ошибка очистки: {e}")
    
    # ============================================================
    # РАБОТА С ДАННЫМИ
    # ============================================================
    
    def _get_user_limits(self, user_id: int, action_type: str) -> Optional[Dict]:
        """Получает лимиты пользователя из кэша или БД"""
        # Проверяем кэш
        with self._cache_lock:
            if user_id in self._cache and action_type in self._cache[user_id]:
                self._stats['cache_hits'] += 1
                return self._cache[user_id][action_type]
        
        self._stats['cache_misses'] += 1
        
        # Загружаем из БД
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT request_count, first_request, last_request, is_blocked, blocked_until
            FROM rate_limits
            WHERE user_id = ? AND action_type = ?
        ''', (user_id, action_type))
        row = c.fetchone()
        conn.close()
        
        if row:
            data = {
                'count': row[0],
                'first_request': row[1],
                'last_request': row[2],
                'is_blocked': row[3],
                'blocked_until': row[4]
            }
            # Сохраняем в кэш
            with self._cache_lock:
                self._cache[user_id][action_type] = data
            return data
        return None
    
    def _save_user_limits(self, user_id: int, action_type: str, data: Dict):
        """Сохраняет лимиты пользователя в БД и кэш"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute('''
            INSERT OR REPLACE INTO rate_limits 
            (user_id, action_type, request_count, first_request, last_request, is_blocked, blocked_until)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            action_type,
            data['count'],
            data.get('first_request'),
            data.get('last_request'),
            data.get('is_blocked', 0),
            data.get('blocked_until')
        ))
        
        conn.commit()
        conn.close()
        
        # Обновляем кэш
        with self._cache_lock:
            if user_id not in self._cache:
                self._cache[user_id] = {}
            self._cache[user_id][action_type] = data
    
    def _delete_user_limits(self, user_id: int, action_type: str = None):
        """Удаляет лимиты пользователя"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        if action_type:
            c.execute('DELETE FROM rate_limits WHERE user_id = ? AND action_type = ?', (user_id, action_type))
        else:
            c.execute('DELETE FROM rate_limits WHERE user_id = ?', (user_id,))
        
        conn.commit()
        conn.close()
        
        # Очищаем кэш
        with self._cache_lock:
            if user_id in self._cache:
                if action_type:
                    self._cache[user_id].pop(action_type, None)
                    if not self._cache[user_id]:
                        del self._cache[user_id]
                else:
                    del self._cache[user_id]
    
    # ============================================================
    # ОСНОВНАЯ ЛОГИКА
    # ============================================================
    
    def is_allowed(self, user_id: int, action_type: str = 'message') -> Tuple[bool, Optional[str]]:
        """
        Проверяет, разрешено ли действие пользователю
        
        Returns:
            (разрешено, сообщение_об_ошибке)
        """
        self._stats['checks'] += 1
        
        # Получаем конфигурацию для данного типа действия
        config = self.config.get(action_type)
        
        # Получаем текущие лимиты пользователя
        user_limits = self._get_user_limits(user_id, action_type)
        current_time = datetime.now()
        
        if user_limits:
            # Проверяем блокировку
            if user_limits.get('is_blocked') and user_limits.get('blocked_until'):
                blocked_until = datetime.fromisoformat(user_limits['blocked_until'])
                if current_time < blocked_until:
                    remaining = int((blocked_until - current_time).total_seconds())
                    self._stats['blocked'] += 1
                    return False, f"⛔ Вы временно заблокированы. Осталось: {remaining} сек."
                else:
                    # Снимаем блокировку
                    user_limits['is_blocked'] = 0
                    user_limits['blocked_until'] = None
                    user_limits['count'] = 0
                    self._save_user_limits(user_id, action_type, user_limits)
            
            # Проверяем окно
            first_request = datetime.fromisoformat(user_limits['first_request'])
            time_since_first = (current_time - first_request).total_seconds()
            
            if time_since_first > config.time_window:
                # Новое окно
                user_limits['count'] = 1
                user_limits['first_request'] = current_time.isoformat()
                user_limits['last_request'] = current_time.isoformat()
                self._save_user_limits(user_id, action_type, user_limits)
                self._stats['allowed'] += 1
                return True, None
            else:
                # Проверяем количество запросов
                if user_limits['count'] >= config.max_requests:
                    # Блокируем
                    blocked_until = current_time + timedelta(seconds=config.block_duration)
                    user_limits['is_blocked'] = 1
                    user_limits['blocked_until'] = blocked_until.isoformat()
                    self._save_user_limits(user_id, action_type, user_limits)
                    
                    self._stats['blocked'] += 1
                    return False, f"⛔ Слишком много запросов! Блокировка на {config.block_duration} сек."
                else:
                    # Увеличиваем счетчик
                    user_limits['count'] += 1
                    user_limits['last_request'] = current_time.isoformat()
                    self._save_user_limits(user_id, action_type, user_limits)
                    self._stats['allowed'] += 1
                    return True, None
        else:
            # Первый запрос
            data = {
                'count': 1,
                'first_request': current_time.isoformat(),
                'last_request': current_time.isoformat(),
                'is_blocked': 0,
                'blocked_until': None
            }
            self._save_user_limits(user_id, action_type, data)
            self._stats['allowed'] += 1
            return True, None
    
    # ============================================================
    # УПРАВЛЕНИЕ
    # ============================================================
    
    def reset_limits(self, user_id: int, action_type: str = None):
        """Сбрасывает лимиты пользователя"""
        self._delete_user_limits(user_id, action_type)
        logger.info(f"✅ Лимиты сброшены для пользователя {user_id}" + (f" ({action_type})" if action_type else ""))
    
    def get_user_status(self, user_id: int) -> Dict:
        """Получает статус пользователя по лимитам"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT action_type, request_count, is_blocked, blocked_until
            FROM rate_limits
            WHERE user_id = ?
        ''', (user_id,))
        rows = c.fetchall()
        conn.close()
        
        status = {}
        for row in rows:
            action_type, count, is_blocked, blocked_until = row
            status[action_type] = {
                'count': count,
                'is_blocked': bool(is_blocked),
                'blocked_until': blocked_until
            }
        return status
    
    def get_stats(self) -> Dict:
        """Получает статистику работы rate limiter'a"""
        with self._stats_lock:
            stats = self._stats.copy()
            stats['hit_rate'] = round(
                stats['cache_hits'] / max(1, stats['cache_hits'] + stats['cache_misses']) * 100, 2
            )
            stats['block_rate'] = round(
                stats['blocked'] / max(1, stats['checks']) * 100, 2
            )
            return stats
    
    def get_blocked_users(self, limit: int = 20) -> list:
        """Получает список заблокированных пользователей"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT DISTINCT user_id, blocked_until
            FROM rate_limits
            WHERE is_blocked = 1
            ORDER BY blocked_until DESC
            LIMIT ?
        ''', (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    
    def force_unblock(self, user_id: int):
        """Принудительно разблокирует пользователя"""
        self.reset_limits(user_id)
        logger.info(f"🔓 Принудительно разблокирован пользователь {user_id}")
    
    def update_config(self, action_type: str, **kwargs):
        """Обновляет конфигурацию для типа действия"""
        if action_type in self.config.limits:
            config = self.config.limits[action_type]
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            logger.info(f"⚙️ Обновлена конфигурация для {action_type}: {kwargs}")
        else:
            logger.warning(f"⚠️ Неизвестный тип действия: {action_type}")


# ============================================================
# ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР
# ============================================================

rate_limiter = RateLimiter()


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    print("🛡️ Rate Limiter тест")
    print("=" * 40)
    
    # Тестируем
    user_id = 12345
    
    for i in range(15):
        allowed, msg = rate_limiter.is_allowed(user_id, 'command')
        print(f"Запрос {i+1}: {'✅' if allowed else '❌'} {msg or ''}")
        
        if not allowed:
            break
    
    print("\n📊 Статистика:")
    print(rate_limiter.get_stats())
    
    print("\n🔓 Сброс лимитов...")
    rate_limiter.reset_limits(user_id)
    
    print("\n✅ Готово!")
