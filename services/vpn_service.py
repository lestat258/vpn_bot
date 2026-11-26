"""
Сервис для работы с VPN (создание, продление, управление подписками)
"""
import fcntl
import logging
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
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


def _client_state(api, email, uid, subid):
    reply = api._request('GET', '/panel/api/clients/get/' + quote(email, safe=''))
    if not reply or reply.get('success') is not True:
        raise ValueError('Не удалось прочитать клиента панели')
    payload = reply['obj']
    client = payload['client']
    if (not uid or not subid or client.get('email') != email
            or uid not in (client.get('uuid'), client.get('id'))
            or client.get('subId') != subid or client.get('enable') is not True
            or not payload.get('inboundIds')):
        raise ValueError('Проверка владельца и состояния ключа не пройдена')
    return int(client['expiryTime']), sorted(payload['inboundIds'])


def _renew(subscription_id, days, tariff_id):
    if type(days) is not int or days <= 0:
        raise ValueError('Срок продления должен быть положительным')
    # Serialize renewals of this key across bot and webhook processes.
    lock_path = Path(DB_PATH).with_name(f'.renew-{int(subscription_id)}.lock')
    with lock_path.open('a') as lock, closing(sqlite3.connect(DB_PATH)) as db:
        fcntl.flock(lock, fcntl.LOCK_EX)
        row = db.execute("""
            SELECT s.end_date, s.xui_email, s.xui_client_uid, s.xui_subid,
                   v.url, v.api_token
              FROM subscriptions s JOIN servers v ON v.id=s.server_id
             WHERE s.id=?
        """, (subscription_id,)).fetchone()
        if not row:
            raise ValueError('Подписка или её сервер не найдены')
        end, email, uid, subid, url, token = row
        api = XUIClient(url=url, api_token=token)
        old_ms, inbounds = _client_state(api, email, uid, subid)
        local_end = datetime.fromisoformat(end)
        if old_ms <= 0 or abs(old_ms - int(local_end.timestamp() * 1000)) > 1:
            raise ValueError('Срок в панели требует сверки')
        target_ms = old_ms + days * 86400000
        reply = api._request('POST', '/panel/api/clients/bulkAdjust',
                             {'emails': [email], 'addDays': days}, retries=1)
        after = _client_state(api, email, uid, subid)
        if (not reply or reply.get('success') is not True
                or (reply.get('obj') or {}).get('adjusted') != 1
                or after != (target_ms, inbounds)):
            raise ValueError('Панель не подтвердила продление, требуется сверка')
        new_end = datetime.fromtimestamp(target_ms / 1000, tz=local_end.tzinfo)
        with db:
            updated = db.execute("""
                UPDATE subscriptions SET end_date=?, tariff_id=COALESCE(?, tariff_id)
                 WHERE id=? AND end_date=? AND xui_client_uid=? AND xui_subid=?
            """, (new_end.isoformat(), tariff_id, subscription_id, end, uid, subid))
            if updated.rowcount != 1:
                raise ValueError('Подписка изменилась во время продления, требуется сверка')
        return new_end


class VPNService:
    """Сервис для управления VPN подписками"""

    @staticmethod
    async def create_subscription(
        telegram_id: int,
        tariff_id: int,
        days: int,
        traffic_gb: float = 0,
        ip_limit: int = 3,
        server_id: Optional[int] = None, *, allow_additional_key: bool = False
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

            # vpn4us stage5: explicit subscription base (or legacy auto)
            from subscription_links import validate_subscription_base, build_subscription_link
            with sqlite3.connect(DB_PATH) as _stage5_db:
                _stage5_row = _stage5_db.execute(
                    "SELECT sub_url FROM servers WHERE id = ?", (best_server['id'],)
                ).fetchone()
            _stage5_base = (_stage5_row[0] or '').strip() if _stage5_row else ''
            if _stage5_base:
                validate_subscription_base(_stage5_base)  # before writing to 3x-ui
            xui = XUIClient(url=best_server['url'], api_token=best_server['api_token'])

            # Получаем все инбаунды
            inbounds = await asyncio.to_thread(xui.get_inbounds)
            if not inbounds:
                return {'success': False, 'error': 'Нет инбаундов на сервере'}

            end_date = datetime.now() + timedelta(days=days)
            expiry_timestamp = int(end_date.timestamp() * 1000)

            # Получаем ВСЕ активные инбаунды
            # vpn4us stage4b: configured inbound policy (fail closed)
            from inbound_policy import select_for_server
            inbound_ids = select_for_server(DB_PATH, best_server["id"], inbounds)

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
            from vpn_creation_guard import gb_to_bytes, assert_absent_on_all_inbounds
            client_data = {
                "client": {
                    "email": email,
                    "totalGB": gb_to_bytes(traffic_gb),
                    "expiryTime": expiry_timestamp,
                    "limitIp": int(ip_limit or 3),
                    "enable": True
                },
                "inboundIds": inbound_ids
            }

            logger.info(f"📤 Отправляем запрос в /panel/api/clients/add")
            # vpn4us stage7c: single create attempt, fail closed
            _stage7c_snapshot = await asyncio.to_thread(xui.get_inbounds, use_cache=False)
            assert_absent_on_all_inbounds(_stage7c_snapshot, email)
            # vpn4us stage7d: persist issue BEFORE first network write
            from vpn_issue_journal import start_issue
            _vpn4us_issue_id = start_issue(DB_PATH, telegram_id, int(best_server['id']), email, tariff_id,
                                            allow_additional_key=allow_additional_key)
            result = await asyncio.to_thread(xui._request, 'POST', '/panel/api/clients/add', client_data)

            # stage7c: no fallback, no whole-inbound writes, never auto-retry
            from vpn_client_verify import confirm_vless_client
            try:
                _stage7c_verified = await asyncio.to_thread(
                    confirm_vless_client, xui, DB_PATH, best_server['id'], inbound_ids, email,
                    minimum_expiry_ms=expiry_timestamp,
                )
            except Exception as _stage7c_exc:
                raise RuntimeError(
                    '3x-ui не подтвердила выдачу; повторно НЕ создавать, требуется сверка'
                ) from _stage7c_exc
            real_uuid = _stage7c_verified['uuid']
            subid = _stage7c_verified['subid']

            if not subid:
                raise RuntimeError("3x-ui не подтвердила SubID клиента")
                logger.warning(f"⚠️ subId не найден, генерируем: {subid}")

            logger.info(f"✅ Клиент {email} создан на {len(inbound_ids)} инбаундах")

            # ============ ФОРМИРУЕМ ПРАВИЛЬНУЮ ССЫЛКУ ============
            # Берем базовый домен из URL сервера
            base_url = best_server['url']
            
            # Извлекаем протокол и домен с портом
            # Пример: https://panel.example.invalid:443/admin-path
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
            if _stage5_base:
                sub_url = build_subscription_link(_stage5_base, subid)
            else:
                sub_url = f"{domain_part}{sub_path}{subid}"
            logger.info("📌 Сформирована HTTPS-ссылка подписки")

            # Сохраняем подписку в БД
            # vpn4us stage4c: verify actual client on every selected inbound
            from vpn_client_verify import confirm_vless_client
            confirmed = await asyncio.to_thread(
                confirm_vless_client, xui, DB_PATH, best_server['id'], inbound_ids, email,
                expected_uuid=real_uuid, expected_subid=subid,
                minimum_expiry_ms=expiry_timestamp,
            )
            real_uuid, subid = confirmed['uuid'], confirmed['subid']
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                INSERT INTO subscriptions (telegram_id, tariff_id, xui_client_uid, xui_email, end_date, is_active, sub_url)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            ''', (telegram_id, tariff_id, real_uuid or subid, email, end_date.isoformat(), sub_url))
            # vpn4us stage6a: mark new subscription as product-owned
            _vpn4us_sub_pk = c.lastrowid
            if not isinstance(_vpn4us_sub_pk, int) or _vpn4us_sub_pk <= 0:
                raise RuntimeError('Subscription insert did not return a valid row ID')
            _vpn4us_sid = int(best_server['id'])
            if _vpn4us_sid <= 0 or not isinstance(subid, str) or not subid:
                raise RuntimeError('Subscription ownership metadata unavailable')
            _vpn4us_initial = sorted(set(int(_iid) for _iid in inbound_ids))
            if not _vpn4us_initial or any(_iid <= 0 for _iid in _vpn4us_initial):
                raise RuntimeError('Initial managed inbound list invalid')
            c.execute(
                'UPDATE subscriptions SET server_id=?, managed_by_vpn4us=1, xui_subid=? WHERE id=?',
                (_vpn4us_sid, subid, _vpn4us_sub_pk)
            )
            if c.rowcount != 1:
                raise RuntimeError('Could not persist subscription ownership')
            c.executemany(
                'INSERT INTO subscription_inbound_memberships '
                '(subscription_id, server_id, inbound_id) VALUES (?,?,?)',
                [(_vpn4us_sub_pk, _vpn4us_sid, _iid) for _iid in _vpn4us_initial]
            )
            subscription_id = _vpn4us_sub_pk
            # vpn4us stage7d: finalize in same subscription transaction
            from vpn_issue_journal import finalize_issue
            finalize_issue(c, _vpn4us_issue_id, int(best_server['id']), telegram_id,
                           email, _vpn4us_sub_pk, real_uuid, subid)
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
            new_end = await asyncio.to_thread(_renew, subscription_id, days_to_add, tariff_id)
            return {'success': True, 'new_end_date': new_end}
        except Exception as exc:
            logger.error("Продление подписки %s требует сверки: %s", subscription_id, type(exc).__name__)
            return {'success': False, 'error': 'Требуется сверка продления'}

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
