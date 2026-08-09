"""
Модуль управления серверами и балансировки
"""

import sqlite3
import logging
import json
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import re
from encryption import decrypt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('ServerManager')

DB_PATH = '/opt/vpn-bot/data.db'

class ServerManager:
    def __init__(self):
        self.db_path = DB_PATH

    def get_active_servers(self) -> List[Dict]:
        """Получает список активных серверов"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT id, name, url, api_token, status, online_count, load_cpu, load_memory, total_traffic_gb, sub_url
            FROM servers
            WHERE is_active = 1
        ''')
        rows = c.fetchall()
        conn.close()

        servers = []
        for row in rows:
            # Расшифровываем API токен
            api_token = decrypt(row[3]) if row[3] else None
            servers.append({
                'id': row[0],
                'name': row[1],
                'url': row[2],
                'api_token': api_token,
                'status': row[4] or 'unknown',
                'online_count': row[5] or 0,
                'load_cpu': row[6] or 0,
                'load_memory': row[7] or 0,
                'total_traffic_gb': row[8] or 0,
                'sub_url': row[9] or ''
            })
        return servers

    def get_best_server(self) -> Optional[Dict]:
        """Выбирает лучший сервер для нового клиента (балансировка)"""
        servers = self.get_active_servers()

        if not servers:
            return None

        online_servers = [s for s in servers if s['status'] == 'online']
        if not online_servers:
            online_servers = servers

        best_server = min(online_servers, key=lambda s: s['load_cpu'])

        logger.info(f"✅ Выбран сервер {best_server['name']} (CPU: {best_server['load_cpu']}%)")
        return best_server

    def get_server_sub_url(self, server_id):
        """Возвращает URL для подписки сервера из БД"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT sub_url FROM servers WHERE id = ?', (server_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    def get_sub_url_for_server(self, server_id):
        """Автоматически формирует URL для подписки на основе данных сервера"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT url FROM servers WHERE id = ?', (server_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        url = row[0]
        match = re.search(r'(https?://[^:/]+)(?::\d+)?', url)
        if match:
            base = match.group(1)
            return f"{base}:2096/sub/"

        return None

    def get_sub_url_for_email(self, email):
        """Возвращает URL для подписки по email клиента"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            SELECT s.sub_url
            FROM servers s
            JOIN subscriptions sub ON sub.xui_email = ?
            WHERE s.id = (
                SELECT sa.server_id
                FROM server_assignments sa
                WHERE sa.subscription_id = sub.id
                LIMIT 1
            )
        ''', (email,))
        row = c.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    def check_server_health(self, server_id: int) -> Dict:
        """Проверяет состояние сервера с таймаутом"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT url, api_token, name, sub_url FROM servers WHERE id = ?', (server_id,))
        server = c.fetchone()
        conn.close()

        if not server:
            return {'status': 'error', 'message': 'Сервер не найден'}

        url, api_token, name, sub_url = server

        # Расшифровываем токен
        api_token = decrypt(api_token) if api_token else None

        try:
            session = requests.Session()
            session.verify = False
            session.headers.update({
                'Authorization': f'Bearer {api_token}',
                'Content-Type': 'application/json'
            })

            resp = session.get(f"{url}/panel/api/inbounds/list", timeout=3)

            if resp.status_code == 200:
                data = resp.json()
                if data.get('success'):
                    total_clients = 0
                    for inbound in data.get('obj', []):
                        settings = inbound.get('settings')
                        if isinstance(settings, str):
                            settings = json.loads(settings)
                        clients = settings.get('clients', [])
                        total_clients += len(clients)

                    conn = sqlite3.connect(self.db_path)
                    c = conn.cursor()
                    c.execute('''
                        UPDATE servers
                        SET status = 'online',
                            online_count = ?,
                            last_check = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (total_clients, server_id))
                    conn.commit()
                    conn.close()

                    self.save_status_history(server_id, 'online', total_clients, 0, 0)

                    return {
                        'status': 'online',
                        'online_count': total_clients,
                        'message': '✅ Сервер работает'
                    }

            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE servers SET status = "offline", last_check = CURRENT_TIMESTAMP WHERE id = ?', (server_id,))
            conn.commit()
            conn.close()

            self.save_status_history(server_id, 'offline', 0, 0, 0)

            return {
                'status': 'offline',
                'message': '❌ Сервер не отвечает'
            }

        except requests.exceptions.Timeout:
            logger.warning(f"⏱️ Таймаут при проверке сервера {name}")
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE servers SET status = "timeout", last_check = CURRENT_TIMESTAMP WHERE id = ?', (server_id,))
            conn.commit()
            conn.close()

            self.save_status_history(server_id, 'timeout', 0, 0, 0)

            return {
                'status': 'timeout',
                'message': '⏱️ Сервер не отвечает (таймаут)'
            }

        except requests.exceptions.ConnectionError:
            logger.error(f"🔌 Ошибка соединения с сервером {name}")
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE servers SET status = "offline", last_check = CURRENT_TIMESTAMP WHERE id = ?', (server_id,))
            conn.commit()
            conn.close()

            self.save_status_history(server_id, 'offline', 0, 0, 0)

            return {
                'status': 'offline',
                'message': '🔌 Сервер недоступен'
            }

        except Exception as e:
            logger.error(f"❌ Ошибка проверки сервера {name}: {e}")
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('UPDATE servers SET status = "error", last_check = CURRENT_TIMESTAMP WHERE id = ?', (server_id,))
            conn.commit()
            conn.close()

            self.save_status_history(server_id, 'error', 0, 0, 0)

            return {
                'status': 'error',
                'message': f'❌ Ошибка: {str(e)[:50]}'
            }

    def save_status_history(self, server_id: int, status: str, online_count: int, load_cpu: float, load_memory: float):
        """Сохраняет историю статусов сервера"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''
                INSERT INTO server_status_history (server_id, status, online_count, load_cpu, load_memory)
                VALUES (?, ?, ?, ?, ?)
            ''', (server_id, status, online_count, load_cpu, load_memory))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка сохранения истории: {e}")

    def check_all_servers(self):
        """Проверяет все серверы"""
        servers = self.get_active_servers()
        results = []

        for server in servers:
            result = self.check_server_health(server['id'])
            results.append({
                'id': server['id'],
                'name': server['name'],
                'status': result['status'],
                'online_count': result.get('online_count', 0),
                'message': result.get('message', '')
            })

        return results

    def get_server_stats(self, server_id: int) -> Dict:
        """Получает статистику сервера"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('''
            SELECT status, online_count, load_cpu, load_memory, total_traffic_gb, last_check
            FROM servers WHERE id = ?
        ''', (server_id,))
        server = c.fetchone()

        c.execute('''
            SELECT status, online_count, load_cpu, load_memory, checked_at
            FROM server_status_history
            WHERE server_id = ? AND checked_at > datetime('now', '-24 hours')
            ORDER BY checked_at DESC
            LIMIT 24
        ''', (server_id,))
        history = c.fetchall()

        conn.close()

        return {
            'current': {
                'status': server[0] if server else 'unknown',
                'online_count': server[1] if server else 0,
                'load_cpu': server[2] if server else 0,
                'load_memory': server[3] if server else 0,
                'total_traffic_gb': server[4] if server else 0,
                'last_check': server[5] if server else None
            },
            'history': history
        }

    def assign_user_to_server(self, subscription_id: int, server_id: int) -> bool:
        """Привязывает пользователя к серверу"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''
                INSERT INTO server_assignments (subscription_id, server_id)
                VALUES (?, ?)
            ''', (subscription_id, server_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Ошибка привязки пользователя: {e}")
            return False

server_manager = ServerManager()

if __name__ == '__main__':
    print("🔄 Проверка серверов...")
    results = server_manager.check_all_servers()
    for r in results:
        print(f"{r['name']}: {r['status']} ({r.get('online_count', 0)} клиентов)")

    print("\n📊 Лучший сервер:")
    best = server_manager.get_best_server()
    if best:
        print(f"{best['name']} (CPU: {best['load_cpu']}%)")
