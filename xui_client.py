#!/usr/bin/env python3
"""
Клиент для работы с 3X-UI API
С поддержкой retry, SSL, кэширования и улучшенной обработкой ошибок
"""
import requests
import json
import logging
import time
import ssl
from typing import Optional, Dict, List, Any
from urllib3.exceptions import InsecureRequestWarning
from encryption import decrypt
import uuid

# Настройка логирования
logger = logging.getLogger(__name__)


class XUIClient:
    """Клиент для взаимодействия с 3X-UI API"""
    
    def __init__(self, url: str, api_token: Optional[str] = None, 
                 timeout: int = 30, max_retries: int = 3, 
                 retry_delay: int = 2, verify_ssl: bool = True):
        """
        Инициализация клиента 3X-UI
        
        Args:
            url: URL панели 3X-UI
            api_token: API токен (будет расшифрован)
            timeout: Таймаут запросов в секундах
            max_retries: Максимальное количество повторов при ошибке
            retry_delay: Задержка между повторами в секундах
            verify_ssl: Проверять SSL сертификаты
        """
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.verify_ssl = verify_ssl
        
        # Расшифровываем токен
        if api_token:
            try:
                self.api_token = decrypt(api_token)
                logger.info(f"✅ API токен расшифрован для {self.url}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось расшифровать API токен: {e}")
                self.api_token = api_token
        else:
            self.api_token = None
        
        # Создаем сессию с настройками
        self.session = requests.Session()
        
        # Настройка SSL
        if verify_ssl:
            self.session.verify = True
        else:
            logger.warning(f"⚠️ SSL проверка ОТКЛЮЧЕНА для {self.url}")
            self.session.verify = False
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        
        # Заголовки
        if self.api_token:
            self.session.headers.update({
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_token}'
            })
        else:
            self.session.headers.update({
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            })
        
        # Кэш для инбаундов
        self._inbounds_cache = None
        self._cache_time = 0
        self._cache_ttl = 60
    
    def _request(self, method: str, path: str, data: Optional[Dict] = None, 
                 retries: Optional[int] = None) -> Optional[Dict]:
        """
        Выполняет HTTP запрос к API с автоматическими повторами
        """
        if retries is None:
            retries = self.max_retries
        
        if path.startswith('/'):
            url = f"{self.url}{path}"
        else:
            url = f"{self.url}/{path}"
        
        last_error = None
        
        for attempt in range(retries):
            try:
                logger.debug(f"📤 {method} {url} (попытка {attempt+1}/{retries})")
                
                if method.upper() == 'GET':
                    resp = self.session.get(url, timeout=self.timeout)
                elif method.upper() == 'POST':
                    resp = self.session.post(url, json=data, timeout=self.timeout)
                else:
                    logger.error(f"❌ Неподдерживаемый метод: {method}")
                    return None
                
                if resp.status_code in [200, 201]:
                    try:
                        result = resp.json()
                        if result.get('success', False) or result.get('obj') is not None:
                            logger.debug(f"✅ Успешный ответ от {path}")
                            return result
                        else:
                            logger.warning(f"⚠️ API вернул success=False: {result.get('msg', 'Unknown error')}")
                            return result
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Ошибка парсинга JSON: {e}")
                        return None
                
                if resp.status_code == 401:
                    logger.error(f"❌ Ошибка авторизации (401) для {path}")
                    return None
                elif resp.status_code == 404:
                    logger.error(f"❌ Ресурс не найден (404) для {path}")
                    return None
                elif resp.status_code >= 500:
                    logger.error(f"❌ Ошибка сервера ({resp.status_code}) для {path}")
                    raise requests.exceptions.RequestException(f"Server error: {resp.status_code}")
                else:
                    logger.error(f"❌ Ошибка HTTP {resp.status_code} для {path}")
                    return None
                
            except requests.exceptions.Timeout as e:
                last_error = f"Timeout: {e}"
                logger.warning(f"⏱️ Таймаут (попытка {attempt+1}/{retries}): {path}")
            except requests.exceptions.ConnectionError as e:
                last_error = f"Connection error: {e}"
                logger.warning(f"🔌 Ошибка соединения (попытка {attempt+1}/{retries}): {path}")
            except requests.exceptions.SSLError as e:
                last_error = f"SSL error: {e}"
                logger.error(f"🔒 Ошибка SSL (попытка {attempt+1}/{retries}): {path}")
                return None
            except Exception as e:
                last_error = str(e)
                logger.error(f"❌ Неизвестная ошибка (попытка {attempt+1}/{retries}): {e}")
            
            if attempt < retries - 1:
                wait_time = self.retry_delay * (attempt + 1)
                logger.debug(f"⏳ Ожидание {wait_time}с перед повторным запросом...")
                time.sleep(wait_time)
        
        logger.error(f"❌ Все попытки ({retries}) провалились для {path}. Последняя ошибка: {last_error}")
        return None
    
    def _clear_cache(self):
        """Очищает кэш инбаундов"""
        self._inbounds_cache = None
        self._cache_time = 0
    
    def get_inbounds(self, use_cache: bool = True) -> List[Dict]:
        """Получает список всех инбаундов с кэшированием"""
        if use_cache and self._inbounds_cache is not None:
            cache_age = time.time() - self._cache_time
            if cache_age < self._cache_ttl:
                logger.debug(f"📦 Используем кэш инбаундов (возраст: {cache_age:.1f}с)")
                return self._inbounds_cache
        
        result = self._request('GET', '/panel/api/inbounds/list')
        
        if result and result.get('success'):
            inbounds = result.get('obj', [])
            self._inbounds_cache = inbounds
            self._cache_time = time.time()
            logger.info(f"✅ Получено {len(inbounds)} инбаундов")
            return inbounds
        
        logger.error("❌ Не удалось получить список инбаундов")
        return []
    
    def get_inbound(self, inbound_id: int) -> Optional[Dict]:
        """Получает информацию об инбаунде по ID"""
        result = self._request('GET', f'/panel/api/inbounds/get/{inbound_id}')
        
        if result and result.get('success'):
            return result.get('obj')
        
        logger.error(f"❌ Не удалось получить инбаунд {inbound_id}")
        return None
    
    def get_client_by_email(self, email: str) -> Optional[Dict]:
        """Получает клиента по email"""
        try:
            inbounds = self.get_inbounds()
            if not inbounds:
                logger.warning(f"⚠️ Нет инбаундов для поиска клиента {email}")
                return None
            
            for inbound in inbounds:
                settings = inbound.get('settings')
                if isinstance(settings, str):
                    try:
                        settings = json.loads(settings)
                    except json.JSONDecodeError:
                        continue
                
                clients = settings.get('clients', [])
                for client in clients:
                    if client.get('email') == email:
                        client['inbound_id'] = inbound.get('id')
                        client['subUrl'] = inbound.get('subUrl')
                        logger.info(f"✅ Найден клиент {email} в инбаунде {inbound.get('id')}")
                        return client
            
            logger.warning(f"⚠️ Клиент {email} не найден ни в одном инбаунде")
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка получения клиента {email}: {e}")
            return None
    
    def add_client(self, inbound_id: int, email: str, total_gb: float = 0, 
                   expiry_time: int = 0, limit_ip: int = 3) -> Optional[Dict]:
        """Добавляет нового клиента в инбаунд"""
        new_uuid = str(uuid.uuid4())
        
        client_data = {
            "client": {
                "id": new_uuid,
                "email": email,
                "limitIp": int(limit_ip),
                "totalGB": int(total_gb),
                "expiryTime": int(expiry_time),
                "enable": True,
                "flow": "xtls-rprx-vision",
                "subId": new_uuid
            },
            "inboundIds": [int(inbound_id)]
        }
        
        logger.info(f"📤 Добавление клиента {email} в инбаунд {inbound_id}")
        logger.debug(f"Данные клиента: {json.dumps(client_data, indent=2)}")
        
        result = self._request("POST", "/panel/api/clients/add", client_data)
        
        if result and result.get("success"):
            data = result.get("data", {})
            subid = data.get("subId", new_uuid)
            
            inbound = self.get_inbound(inbound_id)
            sub_url = inbound.get("subUrl") if inbound else None
            
            if not sub_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.url)
                base = f"{parsed.scheme}://{parsed.netloc.split(':')[0]}"
                sub_url = f"{base}:2096/sub/"
                logger.warning(f"⚠️ subUrl не найден в инбаунде, используем: {sub_url}")
            
            logger.info(f"✅ Клиент {email} создан с subId {subid}")
            
            self._clear_cache()
            
            return {
                "id": new_uuid,
                "email": email,
                "subId": subid,
                "subUrl": sub_url
            }
        
        logger.error(f"❌ Ошибка создания клиента {email}: {result}")
        return None
    
    def attach_client_to_inbounds(self, email: str, inbound_ids: list) -> bool:
        """
        Прикрепляет существующего клиента ко всем инбаундам
        
        Args:
            email: Email клиента
            inbound_ids: Список ID инбаундов для прикрепления
        
        Returns:
            True в случае успеха
        """
        try:
            # Получаем клиента
            client = self.get_client_by_email(email)
            if not client:
                logger.error(f"❌ Клиент {email} не найден")
                return False
            
            client_uuid = client.get('id')
            if not client_uuid:
                logger.error(f"❌ Не найден UUID клиента {email}")
                return False
            
            # Прикрепляем к каждому инбаунду
            success_count = 0
            for inbound_id in inbound_ids:
                try:
                    # Получаем инбаунд
                    inbound = self.get_inbound(inbound_id)
                    if not inbound:
                        continue
                    
                    # Добавляем клиента в инбаунд
                    settings = inbound.get('settings')
                    if isinstance(settings, str):
                        settings = json.loads(settings)
                    
                    clients = settings.get('clients', [])
                    
                    # Проверяем, есть ли уже клиент
                    exists = any(c.get('email') == email for c in clients)
                    if exists:
                        success_count += 1
                        continue
                    
                    # Создаём копию клиента для этого инбаунда
                    new_client = client.copy()
                    # Удаляем subId чтобы создать новый
                    new_client.pop('subId', None)
                    
                    clients.append(new_client)
                    settings['clients'] = clients
                    inbound['settings'] = json.dumps(settings)
                    
                    # Обновляем инбаунд
                    result = self._request('POST', f'/panel/api/inbounds/update/{inbound_id}', inbound)
                    if result and result.get('success'):
                        success_count += 1
                        logger.info(f"✅ Клиент {email} прикреплён к инбаунду {inbound_id}")
                    else:
                        logger.warning(f"⚠️ Не удалось прикрепить клиента {email} к инбаунду {inbound_id}")
                            
                except Exception as e:
                    logger.error(f"❌ Ошибка прикрепления к инбаунду {inbound_id}: {e}")
            
            if success_count > 0:
                logger.info(f"✅ Клиент {email} прикреплён к {success_count} инбаундам")
                return True
            else:
                logger.warning(f"⚠️ Клиент {email} не прикреплён ни к одному инбаунду")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка прикрепления клиента {email}: {e}")
            return False
    
    def update_client_expiry(self, email: str, new_expiry_time: int) -> bool:
        """Обновляет время истечения клиента"""
        try:
            client = self.get_client_by_email(email)
            if not client:
                logger.error(f"❌ Клиент {email} не найден в 3X-UI")
                return False
            
            inbound_id = client.get('inbound_id')
            if not inbound_id:
                logger.error(f"❌ Не найден inbound_id для клиента {email}")
                return False
            
            logger.info(f"📋 Обновление expiryTime для {email} в инбаунде {inbound_id}")
            
            inbound = self.get_inbound(inbound_id)
            if not inbound:
                logger.error(f"❌ Инбаунд {inbound_id} не найден")
                return False
            
            settings = inbound.get('settings')
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except json.JSONDecodeError:
                    logger.error(f"❌ Ошибка парсинга settings для инбаунда {inbound_id}")
                    return False
            
            clients = settings.get('clients', [])
            updated = False
            for client in clients:
                if client.get('email') == email:
                    client['expiryTime'] = int(new_expiry_time)
                    updated = True
                    break
            
            if not updated:
                logger.error(f"❌ Клиент {email} не найден в настройках инбаунда")
                return False
            
            settings['clients'] = clients
            inbound['settings'] = json.dumps(settings)
            
            result = self._request('POST', f'/panel/api/inbounds/update/{inbound_id}', inbound)
            
            if result and result.get('success'):
                logger.info(f"✅ expiryTime для {email} обновлён до {new_expiry_time}")
                self._clear_cache()
                return True
            else:
                logger.error(f"❌ Ошибка обновления: {result}")
                return False
        
        except Exception as e:
            logger.error(f"❌ Ошибка update_client_expiry: {e}")
            return False
    
    def delete_client(self, email: str) -> bool:
        """Удаляет клиента по email"""
        try:
            result = self._request('POST', f'/panel/api/clients/del/{email}')
            if result and result.get('success'):
                logger.info(f"✅ Клиент {email} удален через API")
                self._clear_cache()
                return True
            
            client = self.get_client_by_email(email)
            if not client:
                logger.error(f"❌ Клиент {email} не найден")
                return False
            
            inbound_id = client.get('inbound_id')
            if not inbound_id:
                logger.error(f"❌ Не найден inbound_id для клиента {email}")
                return False
            
            inbound = self.get_inbound(inbound_id)
            if not inbound:
                logger.error(f"❌ Инбаунд {inbound_id} не найден")
                return False
            
            settings = inbound.get('settings')
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except json.JSONDecodeError:
                    logger.error(f"❌ Ошибка парсинга settings для инбаунда {inbound_id}")
                    return False
            
            clients = settings.get('clients', [])
            client_uuid = client.get('id')
            
            clients = [c for c in clients if c.get('id') != client_uuid]
            settings['clients'] = clients
            inbound['settings'] = json.dumps(settings)
            
            result = self._request('POST', f'/panel/api/inbounds/update/{inbound_id}', inbound)
            
            if result and result.get('success'):
                logger.info(f"✅ Клиент {email} удален из инбаунда {inbound_id}")
                self._clear_cache()
                return True
            
            logger.error(f"❌ Не удалось удалить клиента {email}")
            return False
        
        except Exception as e:
            logger.error(f"❌ Ошибка удаления клиента {email}: {e}")
            return False
    
    def test_connection(self) -> bool:
        """Проверяет соединение с API"""
        result = self._request('GET', '/panel/api/inbounds/list', retries=1)
        success = result is not None and result.get('success', False)
        if success:
            logger.info(f"✅ Соединение с {self.url} успешно")
        else:
            logger.error(f"❌ Не удалось подключиться к {self.url}")
        return success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    client = XUIClient(
        url="https://node4.vpn4us.ru:51189/bhHvXWOW0yBnem6VKM",
        api_token="F6QWvX33XlGg1o3LpJXT6YRetJaCpXPFdPc0UXWH6n8XOC1r",
        verify_ssl=True,
        timeout=30,
        max_retries=3
    )
    
    print("🔍 Тестирование соединения...")
    if client.test_connection():
        print("✅ Соединение установлено!")
        
        print("\n📋 Получение инбаундов...")
        inbounds = client.get_inbounds()
        for inbound in inbounds[:3]:
            print(f"  📦 Инбаунд {inbound.get('id')}: {inbound.get('remark', 'Без имени')}")
        
        print(f"\n📊 Всего инбаундов: {len(inbounds)}")
    else:
        print("❌ Не удалось подключиться к 3X-UI")
