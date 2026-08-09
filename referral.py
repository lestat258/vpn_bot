"""
Модуль многоуровневой реферальной системы
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
DB_PATH = '/opt/vpn-bot/data.db'

class ReferralManager:
    def __init__(self):
        self.db_path = DB_PATH
        # Настройки бонусов (в днях)
        self.bonus_levels = {
            1: 30,  # 1 уровень: +30 дней
            2: 15,  # 2 уровень: +15 дней
            3: 5    # 3 уровень: +5 дней
        }
    
    def get_referral_chain(self, user_id: int) -> List[int]:
        """Получает цепочку рефералов пользователя"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT referrer_path FROM users WHERE telegram_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        
        if not row or not row[0]:
            return []
        
        return [int(x) for x in row[0].split('|') if x]
    
    def add_referral(self, user_id: int, referrer_id: int) -> bool:
        """Добавляет реферальную связь"""
        if user_id == referrer_id:
            return False
        
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT referrer_id FROM users WHERE telegram_id = ?', (user_id,))
        existing = c.fetchone()
        
        if existing and existing[0]:
            conn.close()
            return False
        
        c.execute('SELECT referrer_path FROM users WHERE telegram_id = ?', (referrer_id,))
        referrer_path = c.fetchone()
        
        if referrer_path and referrer_path[0]:
            path = f"{referrer_path[0]}|{referrer_id}"
        else:
            path = str(referrer_id)
        
        c.execute('''
            UPDATE users 
            SET referrer_id = ?, referrer_path = ?
            WHERE telegram_id = ?
        ''', (referrer_id, path, user_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Реферал {user_id} добавлен к {referrer_id}, путь: {path}")
        return True
    
    def get_referrals_by_level(self, user_id: int) -> Dict[int, List[int]]:
        """Получает рефералов по уровням"""
        result = {1: [], 2: [], 3: []}
        
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT telegram_id, referrer_path
            FROM users 
            WHERE referrer_path IS NOT NULL AND referrer_path != ''
        ''')
        users = c.fetchall()
        conn.close()
        
        for uid, path in users:
            chain = [int(x) for x in path.split('|') if x]
            if user_id in chain:
                level = chain.index(user_id) + 1
                if level <= 3:
                    result[level].append(uid)
        
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
        
        # Подсчёт бонусных дней
        for level in [1, 2, 3]:
            count = len(referrals.get(level, []))
            if level in self.bonus_levels:
                stats['bonus_days'] += count * self.bonus_levels[level]
        
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
        """Применяет реферальные бонусы к подписке"""
        chain = self.get_referral_chain(user_id)
        if not chain:
            return days
        
        total_bonus = 0
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        for level, referrer_id in enumerate(chain, 1):
            if level > 3:
                break
            
            bonus_days = self.bonus_levels.get(level, 0)
            if bonus_days > 0:
                total_bonus += bonus_days
                
                c.execute('''
                    INSERT INTO referral_bonuses (user_id, referrer_id, level, bonus_days, subscription_id)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, referrer_id, level, bonus_days, subscription_id))
                
                logger.info(f"✅ Бонус {bonus_days} дней (уровень {level}) для {user_id} от {referrer_id}")
        
        conn.commit()
        conn.close()
        
        return days + total_bonus
    
    def get_user_referral_link(self, user_id: int, bot_username: str) -> str:
        """Генерирует реферальную ссылку"""
        return f"https://t.me/{bot_username}?start=ref_{user_id}"

# Глобальный экземпляр
referral_manager = ReferralManager()
