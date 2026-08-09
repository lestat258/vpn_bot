"""
Клиент для работы с 3X-UI API
"""
import requests
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class XUIClient:
    """Клиент для взаимодействия с 3X-UI панелью"""
    
    def __init__(self, url: str, api_token: str, verify_ssl: bool = False):
        self.url = url.rstrip('/')
        self.api_token = api_token
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        if not verify_ssl:
            self.session.verify = False
    
    def _request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Optional[Dict]:
        """Выполняет запрос к API"""
        try:
            url = f"{self.url}{endpoint}"
            logger.debug(f"API Request: {method} {url}")
            
            if data:
                logger.debug(f"Request data: {json.dumps(data, indent=2)}")
            
            response = self.session.request(method, url, json=data)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    return result.get('obj') or result
                else:
                    logger.error(f"API error: {result.get('msg', 'Unknown error')}")
                    return None
            else:
                logger.error(f"HTTP error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Request error: {e}")
            return None
    
    def get_inbounds(self) -> List[Dict]:
        """Получает список всех инбаундов"""
        result = self._request('GET', '/panel/api/inbounds/list')
        if result and isinstance(result, dict):
            return result.get('inbounds', [])
        return []
    
    def add_client_to_all_inbounds(
        self,
        inbound_ids: List[int],
        email: str,
        traffic_gb: float = 0,
        expiry_time: int = 0,
        ip_limit: int = 0
    ) -> Optional[Dict]:
        """
        Добавляет клиента на все указанные инбаунды
        
        Args:
            inbound_ids: Список ID инбаундов
            email: Email клиента (уникальный)
            traffic_gb: Трафик в ГБ (0 = безлимит)
            expiry_time: Время истечения в миллисекундах (0 = безлимит)
            ip_limit: Лимит IP (0 = безлимит)
        
        Returns:
            Dict с id и subId клиента
        """
        if not inbound_ids:
            logger.error("❌ Список инбаундов пуст")
            return None
        
        try:
            import uuid
            client_uuid = str(uuid.uuid4())
            
            logger.info(f"📝 Создаём клиента {email} на {len(inbound_ids)} инбаундах")
            logger.info(f"📝 Инбаунды: {inbound_ids}")
            
            success_count = 0
            sub_id = email
            
            # Создаем клиента на каждом инбаунде
            for inbound_id in inbound_ids:
                try:
                    # Получаем текущий инбаунд
                    inbound = self._request('GET', f'/panel/api/inbounds/get/{inbound_id}')
                    if not inbound:
                        logger.warning(f"⚠️ Инбаунд {inbound_id} не найден, пропускаем")
                        continue
                    
                    # Проверяем, есть ли уже клиент с таким email
                    settings = inbound.get('settings', {})
                    if isinstance(settings, str):
                        settings = json.loads(settings)
                    
                    clients = settings.get('clients', [])
                    client_exists = any(c.get('email') == email for c in clients)
                    
                    if client_exists:
                        logger.info(f"ℹ️ Клиент {email} уже существует в инбаунде {inbound_id}")
                        success_count += 1
                        continue
                    
                    # Создаем клиента для этого инбаунда
                    client_data = {
                        'id': client_uuid,
                        'email': email,
                        'enable': True,
                        'totalGB': traffic_gb,
                        'expiryTime': expiry_time,
                        'limitIp': ip_limit
                    }
                    
                    clients.append(client_data)
                    settings['clients'] = clients
                    
                    update_data = {
                        'id': inbound_id,
                        'port': inbound.get('port'),
                        'protocol': inbound.get('protocol'),
                        'settings': json.dumps(settings),
                        'streamSettings': inbound.get('streamSettings'),
                        'sniffing': inbound.get('sniffing'),
                        'remark': inbound.get('remark', ''),
                        'enable': inbound.get('enable', True)
                    }
                    
                    update_result = self._request('POST', f'/panel/api/inbounds/update/{inbound_id}', update_data)
                    
                    if update_result:
                        logger.info(f"✅ Клиент {email} добавлен в инбаунд {inbound_id}")
                        success_count += 1
                    else:
                        logger.warning(f"⚠️ Не удалось добавить клиента в инбаунд {inbound_id}")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка добавления клиента в инбаунд {inbound_id}: {e}")
                    continue
            
            if success_count > 0:
                logger.info(f"✅ Клиент {email} успешно создан на {success_count} из {len(inbound_ids)} инбаундов")
                return {
                    'id': client_uuid,
                    'subId': sub_id
                }
            else:
                logger.error(f"❌ Не удалось создать клиента ни на одном инбаунде")
                return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания клиента: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def add_client(
        self,
        inbound_id: int,
        email: str,
        traffic_gb: float = 0,
        expiry_time: int = 0,
        ip_limit: int = 0
    ) -> Optional[Dict]:
        """
        Добавляет клиента в один инбаунд
        """
        return self.add_client_to_all_inbounds(
            [inbound_id],
            email,
            traffic_gb,
            expiry_time,
            ip_limit
        )
    
    def attach_client_to_inbounds(self, email: str, inbound_ids: List[int]) -> bool:
        """
        Прикрепляет существующего клиента к инбаундам
        
        Использует API /panel/api/clients/{email}/attach
        """
        if not inbound_ids:
            return True
        
        try:
            data = {'inboundIds': inbound_ids}
            result = self._request('POST', f'/panel/api/clients/{email}/attach', data)
            return result is not None
        except Exception as e:
            logger.error(f"❌ Ошибка прикрепления клиента: {e}")
            return False
    
    def update_client_expiry(self, email: str, new_expiry_time: int) -> bool:
        """Обновляет время истечения клиента"""
        try:
            # Получаем информацию о клиенте
            client_info = self.get_client_by_email(email)
            if not client_info:
                logger.error(f"❌ Клиент {email} не найден")
                return False
            
            # Формируем данные для обновления
            update_data = {
                'email': email,
                'expiryTime': new_expiry_time,
                'enable': True
            }
            
            result = self._request('POST', f'/panel/api/clients/update/{email}', update_data)
            return result is not None
            
        except Exception as e:
            logger.error(f"❌ Ошибка обновления expiryTime: {e}")
            return False
    
    def get_client_by_email(self, email: str) -> Optional[Dict]:
        """Получает информацию о клиенте по email"""
        try:
            result = self._request('GET', f'/panel/api/clients/get/{email}')
            return result
        except Exception as e:
            logger.error(f"❌ Ошибка получения клиента: {e}")
            return None
    
    def get_client_traffic(self, email: str) -> Optional[Dict]:
        """Получает трафик клиента"""
        try:
            result = self._request('GET', f'/panel/api/clients/traffic/{email}')
            return result
        except Exception as e:
            logger.error(f"❌ Ошибка получения трафика: {e}")
            return None
    
    def reset_client_traffic(self, email: str) -> bool:
        """Сбрасывает трафик клиента"""
        try:
            result = self._request('POST', f'/panel/api/clients/resetTraffic/{email}')
            return result is not None
        except Exception as e:
            logger.error(f"❌ Ошибка сброса трафика: {e}")
            return False
    
    def delete_client(self, email: str, keep_traffic: bool = False) -> bool:
        """Удаляет клиента"""
        try:
            keep_param = '1' if keep_traffic else '0'
            result = self._request('POST', f'/panel/api/clients/del/{email}?keepTraffic={keep_param}')
            return result is not None
        except Exception as e:
            logger.error(f"❌ Ошибка удаления клиента: {e}")
            return False

