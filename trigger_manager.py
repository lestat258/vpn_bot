"""
Модуль управления триггерами для промокодов
"""
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)
DB_PATH = '/opt/vpn-bot/data.db'

class TriggerManager:
    def __init__(self):
        self.db_path = DB_PATH
        self.trigger_types = {
            'referrals': {
                'name': 'Рефералы',
                'description': 'Количество приглашённых друзей',
                'icon': '👥'
            },
            'purchases': {
                'name': 'Покупки',
                'description': 'Количество совершённых покупок',
                'icon': '🛒'
            },
            'subscription_days': {
                'name': 'Дни подписки',
                'description': 'Общее количество дней с активной подпиской',
                'icon': '📅'
            },
            'first_payment': {
                'name': 'Первая оплата',
                'description': 'Первая оплата подписки',
                'icon': '💳'
            }
        }
    
    def _get_user_progress(self, user_id: int, trigger_type: str) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT current_value, target_value, is_completed, completed_at
            FROM user_trigger_progress
            WHERE user_id = ? AND trigger_type = ?
        ''', (user_id, trigger_type))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                'current_value': row[0],
                'target_value': row[1],
                'is_completed': bool(row[2]),
                'completed_at': row[3]
            }
        return None
    
    def _update_user_progress(self, user_id: int, trigger_type: str, current_value: int, target_value: int):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            INSERT INTO user_trigger_progress 
            (user_id, trigger_type, current_value, target_value, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, trigger_type) DO UPDATE SET
                current_value = ?,
                target_value = ?,
                updated_at = CURRENT_TIMESTAMP,
                is_completed = CASE WHEN ? >= ? THEN 1 ELSE is_completed END,
                completed_at = CASE WHEN ? >= ? AND is_completed = 0 THEN CURRENT_TIMESTAMP ELSE completed_at END
        ''', (user_id, trigger_type, current_value, target_value, current_value, target_value, current_value, target_value, current_value, target_value))
        conn.commit()
        conn.close()
    
    def get_active_trigger_promocodes(self, trigger_type: str) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT id, code, discount_percent, discount_amount, 
                   trigger_type, trigger_params, max_uses, used_count
            FROM promocodes
            WHERE is_active = 1
                AND trigger_type = ?
                AND (valid_until IS NULL OR valid_until > datetime('now'))
                AND used_count < max_uses
        ''', (trigger_type,))
        rows = c.fetchall()
        conn.close()
        promocodes = []
        for row in rows:
            promocodes.append({
                'id': row[0],
                'code': row[1],
                'discount_percent': row[2],
                'discount_amount': row[3],
                'trigger_type': row[4],
                'trigger_params': json.loads(row[5]) if row[5] else {},
                'max_uses': row[6],
                'used_count': row[7]
            })
        return promocodes
    
    def _get_user_referral_count(self, user_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users WHERE referrer_id = ?', (user_id,))
        count = c.fetchone()[0]
        conn.close()
        return count
    
    def _get_user_purchase_count(self, user_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM payments WHERE telegram_id = ? AND status = "paid"', (user_id,))
        count = c.fetchone()[0]
        conn.close()
        return count
    
    def _get_user_subscription_days(self, user_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT SUM(julianday(end_date) - julianday(start_date))
            FROM subscriptions
            WHERE telegram_id = ? AND is_active = 1
        ''', (user_id,))
        result = c.fetchone()
        conn.close()
        return int(result[0]) if result and result[0] else 0
    
    def _get_user_first_payment_status(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) FROM payments 
            WHERE telegram_id = ? AND status = "paid" 
            LIMIT 1
        ''', (user_id,))
        count = c.fetchone()[0]
        conn.close()
        return count > 0
    
    def _get_trigger_value(self, user_id: int, trigger_type: str, params: Dict = None) -> int:
        if trigger_type == 'referrals':
            return self._get_user_referral_count(user_id)
        elif trigger_type == 'purchases':
            return self._get_user_purchase_count(user_id)
        elif trigger_type == 'subscription_days':
            return self._get_user_subscription_days(user_id)
        elif trigger_type == 'first_payment':
            return 1 if self._get_user_first_payment_status(user_id) else 0
        return 0
    
    def _give_promocode_to_user(self, user_id: int, promocode: Dict, trigger_type: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT id FROM user_promocodes
            WHERE user_id = ? AND promocode_id = ? AND is_used = 0
        ''', (user_id, promocode['id']))
        existing = c.fetchone()
        
        if existing:
            conn.close()
            return False
        
        expires_at = datetime.now() + timedelta(days=90)
        
        c.execute('''
            INSERT INTO user_promocodes 
            (user_id, promocode_id, code, discount_percent, discount_amount, 
             expires_at, trigger_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            promocode['id'],
            promocode['code'],
            promocode['discount_percent'],
            promocode['discount_amount'],
            expires_at.isoformat(),
            trigger_type
        ))
        
        c.execute('''
            UPDATE promocodes SET used_count = used_count + 1
            WHERE id = ?
        ''', (promocode['id'],))
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Промокод {promocode['code']} выдан пользователю {user_id}")
        return True
    
    def check_and_apply_triggers(self, user_id: int, event_type: str = None) -> List[Dict]:
        activated = []
        
        for trigger_type in ['referrals', 'purchases', 'subscription_days', 'first_payment']:
            promocodes = self.get_active_trigger_promocodes(trigger_type)
            if not promocodes:
                continue
            
            current_value = self._get_trigger_value(user_id, trigger_type)
            
            for promo in promocodes:
                target_value = promo['trigger_params'].get('target_value', 0)
                if target_value == 0:
                    continue
                
                if current_value >= target_value:
                    conn = sqlite3.connect(self.db_path)
                    c = conn.cursor()
                    c.execute('''
                        SELECT id FROM user_promocodes
                        WHERE user_id = ? AND promocode_id = ? AND is_used = 0
                    ''', (user_id, promo['id']))
                    existing = c.fetchone()
                    conn.close()
                    
                    if not existing:
                        if self._give_promocode_to_user(user_id, promo, trigger_type):
                            activated.append({
                                'promocode': promo,
                                'trigger_type': trigger_type,
                                'current_value': current_value,
                                'target_value': target_value
                            })
                            self._update_user_progress(user_id, trigger_type, current_value, target_value)
        
        return activated
    
    def get_user_promocodes(self, user_id: int) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT id, code, discount_percent, discount_amount, 
                   is_used, expires_at, created_at, trigger_type
            FROM user_promocodes
            WHERE user_id = ? AND is_used = 0 AND expires_at > datetime('now')
            ORDER BY expires_at ASC
        ''', (user_id,))
        rows = c.fetchall()
        conn.close()
        
        promocodes = []
        for row in rows:
            promocodes.append({
                'id': row[0],
                'code': row[1],
                'discount_percent': row[2],
                'discount_amount': row[3],
                'is_used': bool(row[4]),
                'expires_at': row[5],
                'created_at': row[6],
                'trigger_type': row[7]
            })
        return promocodes
    
    def get_user_used_promocodes(self, user_id: int) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT id, code, discount_percent, discount_amount, 
                   used_at, trigger_type
            FROM user_promocodes
            WHERE user_id = ? AND is_used = 1
            ORDER BY used_at DESC
        ''', (user_id,))
        rows = c.fetchall()
        conn.close()
        
        promocodes = []
        for row in rows:
            promocodes.append({
                'id': row[0],
                'code': row[1],
                'discount_percent': row[2],
                'discount_amount': row[3],
                'used_at': row[4],
                'trigger_type': row[5]
            })
        return promocodes
    
    def use_promocode(self, user_id: int, promocode_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT id, code, discount_percent, discount_amount, is_used, expires_at
            FROM user_promocodes
            WHERE id = ? AND user_id = ? AND is_used = 0
        ''', (promocode_id, user_id))
        promo = c.fetchone()
        
        if not promo:
            conn.close()
            return False
        
        expires_at = datetime.fromisoformat(promo[5])
        if datetime.now() > expires_at:
            conn.close()
            return False
        
        c.execute('''
            UPDATE user_promocodes 
            SET is_used = 1, used_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (promocode_id,))
        
        c.execute('''
            INSERT INTO promocode_uses (promocode_id, user_id, source)
            VALUES (?, ?, ?)
        ''', (promo[0], user_id, f'user_promocode_{promo[1]}'))
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Промокод {promo[1]} использован пользователем {user_id}")
        return True
    
    def is_promocode_applied(self, user_id: int, promocode_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT id FROM user_promocodes
            WHERE user_id = ? AND promocode_id = ? AND is_used = 1
        ''', (user_id, promocode_id))
        result = c.fetchone()
        conn.close()
        return result is not None
    
    def get_user_trigger_status(self, user_id: int) -> Dict:
        status = {}
        
        for trigger_type in ['referrals', 'purchases', 'subscription_days', 'first_payment']:
            promocodes = self.get_active_trigger_promocodes(trigger_type)
            if not promocodes:
                continue
            
            progress = self._get_user_progress(user_id, trigger_type)
            current_value = self._get_trigger_value(user_id, trigger_type)
            
            max_target = 0
            for promo in promocodes:
                target = promo['trigger_params'].get('target_value', 0)
                if target > max_target:
                    max_target = target
            
            if max_target > 0:
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute('''
                    SELECT promocode_id FROM user_promocodes
                    WHERE user_id = ? AND is_used = 0
                ''', (user_id,))
                received = [row[0] for row in c.fetchall()]
                conn.close()
                
                available_promos = []
                for promo in promocodes:
                    if promo['id'] not in received:
                        available_promos.append(promo)
                
                status[trigger_type] = {
                    'current': current_value,
                    'target': max_target,
                    'is_completed': current_value >= max_target,
                    'promocodes': available_promos,
                    'received_count': len(received),
                    'info': self.trigger_types.get(trigger_type, {})
                }
        
        return status
    
    def cleanup_expired_promocodes(self, user_id: int = None):
        """Очищает истёкшие промокоды"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        if user_id:
            c.execute('''
                DELETE FROM user_promocodes
                WHERE user_id = ? AND expires_at < datetime('now') AND is_used = 0
            ''', (user_id,))
        else:
            c.execute('''
                DELETE FROM user_promocodes
                WHERE expires_at < datetime('now') AND is_used = 0
            ''')
        
        deleted = c.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            logger.info(f"✅ Удалено {deleted} истёкших промокодов")
        return deleted

trigger_manager = TriggerManager()
