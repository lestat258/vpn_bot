"""
Вспомогательные функции для бота
"""
import hashlib
import sqlite3
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

from xui_client import XUIClient
from server_manager import server_manager

DB_PATH = '/opt/vpn-bot/data.db'

def generate_short_email(telegram_id, username=None):
    """Генерирует короткий email для клиента"""
    if username:
        name_part = username[:15]
        name_part = ''.join(c for c in name_part if c.isalnum() or c == '_')
    else:
        name_part = str(telegram_id)

    if not name_part:
        name_part = str(telegram_id)

    hash_input = f"{telegram_id}_{datetime.now().timestamp()}"
    hash_short = hashlib.md5(hash_input.encode()).hexdigest()[:5]

    return f"user_{name_part}_{hash_short}"

def find_subid_by_email(email):
    """Ищет subId клиента по email на всех активных серверах"""
    logging.info(f"🔍 Ищем subId для email: {email}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, url, api_token FROM servers WHERE is_active = 1')
    servers = c.fetchall()
    conn.close()

    if not servers:
        logging.warning("⚠️ Нет активных серверов в БД")
        return None

    for server_id, server_name, url, api_token in servers:
        try:
            xui = XUIClient(url=url, api_token=api_token)
            inbounds = xui.get_inbounds()
            for inbound in inbounds:
                settings = inbound.get('settings')
                if isinstance(settings, str):
                    settings = json.loads(settings)
                for client in settings.get('clients', []):
                    if client.get('email') == email:
                        subid = client.get('subId')
                        if subid:
                            logging.info(f"✅ Найден subId {subid} на сервере {server_name}")
                            return subid
        except Exception as e:
            logging.error(f"❌ Ошибка на сервере {server_name}: {e}")

    return None

def has_used_trial(telegram_id):
    """Проверяет, использовал ли пользователь пробный тариф"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT COUNT(*) FROM payments
        WHERE telegram_id = ? AND tariff_id IN (SELECT id FROM tariffs WHERE name LIKE '%Пробный%' OR name LIKE '%Тестовый%') AND status = 'paid'
    ''', (telegram_id,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def get_active_subscriptions(telegram_id):
    """Возвращает список активных подписок пользователя"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, s.tariff_id, s.xui_email, s.end_date, t.name
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.telegram_id = ? AND s.is_active = 1 AND datetime(s.end_date) > datetime('now')
        ORDER BY s.end_date DESC
    ''', (telegram_id,))
    subs = c.fetchall()
    conn.close()
    return subs

def get_user_balance(telegram_id):
    """Получает баланс пользователя"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT balance FROM users WHERE telegram_id = ?', (telegram_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def create_vpn_client(telegram_id, tariff_id, days, traffic_gb, ip_limit, server_id=None):
    """
    Создаёт VPN клиента на сервере
    
    Returns:
        dict: {success: bool, subscription_id: int, link: str, error: str}
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT username FROM users WHERE telegram_id = ?', (telegram_id,))
        user_row = c.fetchone()
        username = user_row[0] if user_row and user_row[0] else None
        conn.close()
        
        email = generate_short_email(telegram_id, username)
        
        if server_id:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT id, name, url, api_token FROM servers WHERE id = ? AND is_active = 1', (server_id,))
            server = c.fetchone()
            conn.close()
            if not server:
                return {'success': False, 'error': 'Сервер не найден или неактивен'}
            best_server = {'id': server[0], 'name': server[1], 'url': server[2], 'api_token': server[3]}
        else:
            best_server = server_manager.get_best_server()
            if not best_server:
                return {'success': False, 'error': 'Нет доступных серверов'}
        
        xui = XUIClient(url=best_server['url'], api_token=best_server['api_token'])
        inbounds = xui.get_inbounds()
        
        if not inbounds:
            return {'success': False, 'error': 'Нет инбаундов на сервере'}
        
        end_date = datetime.now() + timedelta(days=days)
        expiry_timestamp = int(end_date.timestamp() * 1000)
        
        inbound_id = None
        for inbound in inbounds:
            if inbound.get('enable', True):
                inbound_id = inbound.get('id')
                break
        
        if not inbound_id:
            return {'success': False, 'error': 'Нет активных инбаундов'}
        
        result = xui.add_client(
            inbound_id=inbound_id,
            email=email,
            total_gb=traffic_gb or 0,
            expiry_time=expiry_timestamp,
            limit_ip=ip_limit or 3
        )
        
        if not result:
            return {'success': False, 'error': 'Не удалось создать клиента на сервере'}
        
        subid = result.get('subId')
        real_uuid = result.get('id')
        
        # Сохраняем подписку
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO subscriptions (telegram_id, tariff_id, xui_client_uid, xui_email, end_date, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (telegram_id, tariff_id, real_uuid, email, end_date.isoformat()))
        subscription_id = c.lastrowid
        conn.commit()
        conn.close()
        
        # Получаем ссылку
        link = get_server_link(best_server['id'], subid)
        
        return {
            'success': True,
            'subscription_id': subscription_id,
            'link': link,
            'email': email,
            'end_date': end_date,
            'client_id': real_uuid
        }
        
    except Exception as e:
        logging.error(f"Ошибка создания клиента: {e}")
        return {'success': False, 'error': str(e)}

def get_server_link(server_id, subid):
    """Получает ссылку для подписки с сервера"""
    sub_url = server_manager.get_server_sub_url(server_id)
    if not sub_url:
        sub_url = server_manager.get_sub_url_for_server(server_id)
        if not sub_url:
            sub_url = "https://node5.vpn4us.ru:2096/sub/"
    return f"{sub_url}{subid}"

def extend_subscription(subscription_id, days_to_add):
    """Продлевает подписку"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT end_date, xui_email FROM subscriptions WHERE id = ?', (subscription_id,))
    sub = c.fetchone()
    
    if not sub:
        conn.close()
        return {'success': False, 'error': 'Подписка не найдена'}
    
    end_date, xui_email = sub
    new_end_date = datetime.fromisoformat(end_date) + timedelta(days=days_to_add)
    
    c.execute('UPDATE subscriptions SET end_date = ? WHERE id = ?', (new_end_date.isoformat(), subscription_id))
    conn.commit()
    conn.close()
    
    # Обновляем в 3X-UI
    from xui_client import XUIClient
    server = server_manager.get_best_server()
    if server:
        xui = XUIClient(url=server['url'], api_token=server['api_token'])
        xui.update_client_expiry(xui_email, int(new_end_date.timestamp() * 1000))
    
    return {
        'success': True,
        'new_end_date': new_end_date
    }

def get_subscription_details(subscription_id):
    """Получает детали подписки"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, s.xui_email, s.end_date, t.name, s.xui_client_uid, 
               s.traffic_used_gb, s.tariff_id, s.start_date, s.telegram_id
        FROM subscriptions s
        JOIN tariffs t ON t.id = s.tariff_id
        WHERE s.id = ?
    ''', (subscription_id,))
    sub = c.fetchone()
    conn.close()
    return sub

def get_tariff_by_id(tariff_id):
    """Получает информацию о тарифе по ID"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days, traffic_gb, ip_limit FROM tariffs WHERE id = ?', (tariff_id,))
    tariff = c.fetchone()
    conn.close()
    return tariff

def get_user_by_telegram_id(telegram_id):
    """Получает информацию о пользователе"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, telegram_id, username, first_name, created_at, is_blocked, balance FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()
    conn.close()
    return user

def get_promocode_discount(user_id):
    """Получает скидку по промокоду для пользователя"""
    from database import get_setting
    promocode_data = get_setting(f'promocode_user_{user_id}')
    discount_percent = 0
    discount_amount = 0

    if promocode_data:
        try:
            parts = promocode_data.split('|')
            discount_percent = int(parts[0]) if len(parts) > 0 else 0
            discount_amount = float(parts[1]) if len(parts) > 1 else 0
        except:
            pass

    return discount_percent, discount_amount
