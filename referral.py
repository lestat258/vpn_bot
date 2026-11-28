"""
Регистрация рефералов и совместимость старой статистики
"""
from runtime_config import DB_PATH
import sqlite3
from contextlib import closing
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class ReferralManager:
    def __init__(self):
        self.db_path = DB_PATH
    
    @staticmethod
    def _chain(db, user_id):
        """Follow the canonical parent IDs, nearest referrer first."""
        seen = {user_id}
        chain = []
        current = user_id
        while True:
            row = db.execute("SELECT referrer_id FROM users WHERE telegram_id=?", (current,)).fetchone()
            if not row or not row[0]:
                return chain, True
            parent = row[0]
            if parent in seen or not db.execute(
                    "SELECT 1 FROM users WHERE telegram_id=?", (parent,)).fetchone():
                return chain, False
            chain.append(parent)
            seen.add(parent)
            current = parent

    def get_referral_chain(self, user_id: int) -> List[int]:
        with closing(sqlite3.connect(self.db_path)) as db:
            chain, valid = self._chain(db, user_id)
            if not valid:
                logger.warning("Invalid referral chain for user %s", user_id)
                return []
            return chain

    def add_referral(self, user_id: int, referrer_id: int) -> bool:
        """Register an existing, unassigned user under an existing referrer."""
        if user_id <= 0 or referrer_id <= 0 or user_id == referrer_id:
            return False
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT referrer_id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not row or row[0]:
                return False
            if not db.execute("SELECT 1 FROM users WHERE telegram_id=?", (referrer_id,)).fetchone():
                return False
            chain, valid = self._chain(db, referrer_id)
            if not valid or user_id in chain:
                return False
            updated = db.execute("""
                UPDATE users SET referrer_id=?
                WHERE telegram_id=? AND (referrer_id IS NULL OR referrer_id=0)
            """, (referrer_id,user_id)).rowcount
            return updated == 1

    def get_referrals_by_level(self, user_id: int) -> Dict[int, List[int]]:
        """Traverse at most three levels through the indexed parent column."""
        result = {1: [], 2: [], 3: []}
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM users WHERE telegram_id=?", (user_id,)).fetchone():
                return result
            seen = {user_id}
            parents = [user_id]
            for level in (1, 2, 3):
                children = []
                for parent in parents:
                    rows = db.execute(
                        "SELECT telegram_id FROM users WHERE referrer_id=? ORDER BY telegram_id", (parent,))
                    for (uid,) in rows:
                        if uid not in seen:
                            seen.add(uid)
                            children.append(uid)
                result[level] = children
                parents = children
                if not parents:
                    break
        return result

    def get_referral_stats(self, user_id: int) -> Dict:
        """Получает статистику рефералов"""
        referrals = self.get_referrals_by_level(user_id)
        
        stats = {
            'total': 0,
            'level_1': len(referrals.get(1, [])),
            'level_2': len(referrals.get(2, [])),
            'level_3': len(referrals.get(3, [])),
            'bonus_days': 0
        }
        stats['total'] = stats['level_1'] + stats['level_2'] + stats['level_3']
        
        # Registration counts are not earned bonus days.
        from referral_accrual import confirmed_days
        stats['bonus_days'] = confirmed_days(self.db_path, user_id)
        
        return stats
    
    def get_referral_chain_text(self, user_id: int) -> str:
        """Возвращает текстовое представление цепочки рефералов"""
        chain = self.get_referral_chain(user_id)
        if not chain:
            return "Нет реферера"
        
        levels = []
        for i, ref_id in enumerate(chain, 1):
            level_name = {1: '1 уровень', 2: '2 уровень', 3: '3 уровень'}.get(i, f'{i} уровень')
            levels.append(f"{level_name}: {ref_id}")
        
        return " → ".join(levels)
    
    def apply_referral_bonus(self, user_id: int, subscription_id: int, days: int) -> int:
        """Legacy API cannot create or spend awards."""
        raise RuntimeError("Legacy referral bonus API disabled")

    def get_user_referral_link(self, user_id: int, bot_username: str) -> str:
        """Генерирует реферальную ссылку"""
        return f"https://t.me/{bot_username}?start=ref_{user_id}"

# Глобальный экземпляр
referral_manager = ReferralManager()
