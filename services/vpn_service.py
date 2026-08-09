"""
Сервис для работы с VPN (создание, продление, управление подписками)
"""
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
import uuid
import re

from xui_client import XUIClient
from server_manager import server_manager
from utils import generate_short_email, find_subid_by_email

logger = logging.getLogger(__name__)
DB_PATH = '/opt/vpn-bot/data.db'


class VPNService:
    """Сервис для управления VPN подписками"""

    @staticmethod
    async def create_subscription(
        telegram_id: int,
        tariff_id: int,
        days: int,
        traffic_gb: float = 0,
        ip_limit: int = 3,
        server_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Создает новую VPN подписку на ВСЕХ активных инбаундах сервера"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT username FROM users WHERE telegram_id = ?', (telegram_id,))
            user_row = c.fetchone()
            username = user_row[0] if user_row else None
            conn.close()

            email = generate_short_email(telegram_id, username)

            # Получаем сервер
            if server_id:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('SELECT id, url, api_token FROM servers WHERE id = ? AND is_active = 1', (server_id,))
                server = c.fetchone()
                conn.close()
                if not server:
                    return {'success': False, 'error': 'Сервер не найден или неактивен'}
                best_server = {'id': server[0], 'url': server[1], 'api_token': server[2]}
            else:
                best_server = await asyncio.to_thread(server_manager.get_best_server)
                if not best_server:
                    return {'success': False, 'error': 'Нет доступных серверов'}

            xui = XUIClient(url=best_server['url'], api_token=best_server['api_token'])

            # Получаем все инбаунды
            inbounds = await asyncio.to_thread(xui.get_inbounds)
            if not inbounds:
                return {'success': False, 'error': 'Нет инбаундов на сервере'}

            end_date = datetime.now() + timedelta(days=days)
            expiry_timestamp = int(end_date.timestamp() * 1000)

            # Получаем ВСЕ активные инбаунды
            inbound_ids = []
            for inbound in inbounds:
                if inbound.get('enable', True):
                    inbound_id = inbound.get('id')
                    if inbound_id:
                        inbound_ids.append(inbound_id)

            if not inbound_ids:
                return {'success': False, 'error': 'Нет активных инбаундов'}

            logger.info(f"📋 Найдено активных инбаундов: {len(inbound_ids)}")
            logger.info(f"📋 ID инбаундов: {inbound_ids}")

            # ============ ПОЛУЧАЕМ НАСТРОЙКИ ПОДПИСКИ ИЗ ПАНЕЛИ ============
            settings = await asyncio.to_thread(xui._request, 'POST', '/panel/api/setting/all')
            sub_path = '/pod/'
            sub_port = 2096
            
            if settings and settings.get('success'):
                obj = settings.get('obj', {})
                sub_path = obj.get('subPath', '/pod/')
                sub_port = obj.get('subPort', 2096)
                logger.info(f"📌 Настройки подписки из панели:")
                logger.info(f"   subPath: {sub_path}")
                logger.info(f"   subPort: {sub_port}")

            # ============ СОЗДАЕМ КЛИЕНТА ============
            client_data = {
                "client": {
                    "email": email,
                    "totalGB": int(traffic_gb or 0),
                    "expiryTime": expiry_timestamp,
                    "limitIp": int(ip_limit or 3),
                    "enable": True
                },
                "inboundIds": inbound_ids
            }

            logger.info(f"📤 Отправляем запрос в /panel/api/clients/add")
            result = await asyncio.to_thread(xui._request, 'POST', '/panel/api/clients/add', client_data)

            if not result or not result.get('success'):
                logger.warning("⚠️ Метод /clients/add не сработал, пробуем альтернативный")
                result = await asyncio.to_thread(
                    xui.add_client,
                    inbound_ids[0], email, traffic_gb or 0, expiry_timestamp, ip_limit or 3
                )
                if result and len(inbound_ids) > 1:
                    await asyncio.to_thread(xui.attach_client_to_inbounds, email, inbound_ids[1:])
                
                if not result:
                    return {'success': False, 'error': 'Не удалось создать клиента'}
                
                subid = result.get('subId')
                real_uuid = result.get('id')
            else:
                # Получаем информацию о созданном клиенте
                logger.info(f"📤 Получаем информацию о клиенте {email}")
                client_info = await asyncio.to_thread(xui._request, 'GET', f'/panel/api/clients/get/{email}')
                
                if client_info and client_info.get('success'):
                    obj = client_info.get('obj', {})
                    client = obj.get('client', {})
                    
                    subid = client.get('subId')
                    real_uuid = client.get('uuid')
                    
                    logger.info(f"✅ Клиент создан: subId={subid}, uuid={real_uuid}")
                else:
                    subid = str(uuid.uuid4())
                    real_uuid = str(uuid.uuid4())
                    logger.warning(f"⚠️ Не удалось получить информацию о клиенте, генерируем UUID")

            if not subid:
                subid = str(uuid.uuid4())
                logger.warning(f"⚠️ subId не найден, генерируем: {subid}")

            logger.info(f"✅ Клиент {email} создан на {len(inbound_ids)} инбаундах")
            logger.info(f"📌 subId: {subid}")

            # ============ ФОРМИРУЕМ ПРАВИЛЬНУЮ ССЫЛКУ ============
            # Берем базовый домен из URL сервера
            base_url = best_server['url']
            
            # Извлекаем протокол и домен с портом
            # Пример: https://panel2.vpn4us.ru:31363/0F25PelLC9wnVflbcB
            match = re.match(r'(https?://[^/:]+(?::\d+)?)', base_url)
            if match:
                domain_part = match.group(1)
                # Заменяем порт панели на порт подписки
                domain_part = re.sub(r':\d+$', f':{sub_port}', domain_part)
                logger.info(f"📌 Домен с портом подписки: {domain_part}")
            else:
                # Если не удалось распарсить, берем как есть
                domain_part = base_url.split('/panel')[0].split('/api')[0].rstrip('/')
                domain_part = re.sub(r':\d+$', f':{sub_port}', domain_part)
                logger.warning(f"⚠️ Не удалось распарсить URL, используем: {domain_part}")
            
            # Формируем путь подписки
            if not sub_path.startswith('/'):
                sub_path = '/' + sub_path
            if not sub_path.endswith('/'):
                sub_path = sub_path + '/'
            
            # Собираем полную ссылку
            sub_url = f"{domain_part}{sub_path}{subid}"
            logger.info(f"📌 Сформирована ссылка подписки: {sub_url}")

            # Сохраняем подписку в БД
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                INSERT INTO subscriptions (telegram_id, tariff_id, xui_client_uid, xui_email, end_date, is_active, sub_url)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            ''', (telegram_id, tariff_id, real_uuid or subid, email, end_date.isoformat(), sub_url))
            subscription_id = c.lastrowid
            conn.commit()
            conn.close()

            return {
                'success': True,
                'subscription_id': subscription_id,
                'link': sub_url,
                'email': email,
                'end_date': end_date,
                'client_id': real_uuid or subid,
                'subid': subid,
                'inbound_count': len(inbound_ids),
                'inbound_ids': inbound_ids
            }

        except Exception as e:
            logger.error(f"❌ Ошибка создания подписки: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {'success': False, 'error': str(e)}

    @staticmethod
    async def extend_subscription(
        subscription_id: int,
        days_to_add: int,
        tariff_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Продлевает существующую подписку"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT end_date, xui_email FROM subscriptions WHERE id = ?', (subscription_id,))
            sub = c.fetchone()

            if not sub:
                conn.close()
                return {'success': False, 'error': 'Подписка не найдена'}

            end_date, xui_email = sub
            current_end_date = datetime.fromisoformat(end_date)
            new_end_date = current_end_date + timedelta(days=days_to_add)

            if tariff_id:
                c.execute('UPDATE subscriptions SET end_date = ?, tariff_id = ? WHERE id = ?',
                         (new_end_date.isoformat(), tariff_id, subscription_id))
            else:
                c.execute('UPDATE subscriptions SET end_date = ? WHERE id = ?',
                         (new_end_date.isoformat(), subscription_id))
            conn.commit()
            conn.close()

            logger.info(f"✅ Подписка {subscription_id} продлена до {new_end_date}")

            # Обновляем expiryTime на сервере
            best_server = await asyncio.to_thread(server_manager.get_best_server)
            if best_server:
                xui = XUIClient(url=best_server['url'], api_token=best_server['api_token'])
                new_expiry_time = int(new_end_date.timestamp() * 1000)
                success = await asyncio.to_thread(xui.update_client_expiry, xui_email, new_expiry_time)
                if success:
                    logger.info(f"✅ Обновлён expiryTime в 3X-UI для {xui_email}")
                else:
                    logger.warning(f"⚠️ Не удалось обновить expiryTime в 3X-UI для {xui_email}")

            return {
                'success': True,
                'new_end_date': new_end_date
            }

        except Exception as e:
            logger.error(f"❌ Ошибка продления подписки: {e}")
            return {'success': False, 'error': str(e)}

    @staticmethod
    async def get_subscription_link(subscription_id: int) -> Optional[str]:
        """Получает ссылку для подключения по ID подписки"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                SELECT s.xui_email, s.xui_client_uid, s.sub_url, t.name
                FROM subscriptions s
                JOIN tariffs t ON t.id = s.tariff_id
                WHERE s.id = ?
            ''', (subscription_id,))
            sub = c.fetchone()
            conn.close()

            if not sub:
                return None

            _, _, sub_url, _ = sub
            return sub_url

        except Exception as e:
            logger.error(f"❌ Ошибка получения ссылки: {e}")
            return None


# Глобальный экземпляр
vpn_service = VPNService()
