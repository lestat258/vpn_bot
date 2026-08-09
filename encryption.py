"""
Модуль шифрования чувствительных данных
Использует Fernet (симметричное шифрование) из библиотеки cryptography
"""
import os
import base64
import logging
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from database import get_setting, set_setting

logger = logging.getLogger(__name__)

class EncryptionManager:
    def __init__(self):
        self.key = None
        self.cipher = None
        self.load_or_generate_key()
    
    def load_or_generate_key(self):
        """Загружает ключ шифрования из БД или генерирует новый"""
        try:
            # Пытаемся загрузить ключ из БД
            encrypted_key = get_setting('encryption_key')
            
            if encrypted_key:
                # Если ключ есть - расшифровываем его
                # Ключ хранится в base64, декодируем
                self.key = base64.urlsafe_b64decode(encrypted_key)
                self.cipher = Fernet(self.key)
                logger.info("✅ Ключ шифрования загружен из БД")
            else:
                # Генерируем новый ключ
                self.generate_key()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки ключа: {e}")
            # Генерируем новый ключ в случае ошибки
            self.generate_key()
    
    def generate_key(self):
        """Генерирует новый ключ шифрования"""
        # Генерируем случайный ключ
        self.key = Fernet.generate_key()
        self.cipher = Fernet(self.key)
        
        # Сохраняем ключ в БД в base64
        encrypted_key = base64.urlsafe_b64encode(self.key).decode()
        set_setting('encryption_key', encrypted_key)
        
        logger.info("✅ Новый ключ шифрования сгенерирован и сохранён")
        return self.key
    
    def encrypt(self, data):
        """Шифрует данные"""
        if not data:
            return data
        
        try:
            if isinstance(data, str):
                data = data.encode()
            encrypted = self.cipher.encrypt(data)
            return base64.urlsafe_b64encode(encrypted).decode()
        except Exception as e:
            logger.error(f"❌ Ошибка шифрования: {e}")
            return data
    
    def decrypt(self, encrypted_data):
        """Расшифровывает данные"""
        if not encrypted_data:
            return encrypted_data
        
        try:
            # Декодируем из base64
            encrypted = base64.urlsafe_b64decode(encrypted_data)
            decrypted = self.cipher.decrypt(encrypted)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"❌ Ошибка дешифрования: {e}")
            return encrypted_data
    
    def is_encrypted(self, data):
        """Проверяет, зашифрованы ли данные (проверка по формату base64)"""
        if not data:
            return False
        
        try:
            # Пробуем декодировать из base64
            base64.urlsafe_b64decode(data)
            # Проверяем, что это валидный base64 и длина соответствует зашифрованным данным
            return True
        except:
            return False
    
    def migrate_old_data(self):
        """Миграция старых незашифрованных данных"""
        import sqlite3
        from database import DB_PATH
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Миграция API токенов в таблице servers
        c.execute('SELECT id, api_token FROM servers WHERE api_token IS NOT NULL AND api_token != ""')
        servers = c.fetchall()
        
        migrated_count = 0
        for server_id, api_token in servers:
            # Проверяем, не зашифрован ли уже
            if not self.is_encrypted(api_token):
                encrypted_token = self.encrypt(api_token)
                c.execute('UPDATE servers SET api_token = ? WHERE id = ?', (encrypted_token, server_id))
                migrated_count += 1
        
        # Миграция паролей в таблице users (если есть)
        # Проверяем наличие колонки password
        c.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in c.fetchall()]
        
        if 'password' in columns:
            c.execute('SELECT telegram_id, password FROM users WHERE password IS NOT NULL AND password != ""')
            users = c.fetchall()
            
            for telegram_id, password in users:
                if not self.is_encrypted(password):
                    encrypted_password = self.encrypt(password)
                    c.execute('UPDATE users SET password = ? WHERE telegram_id = ?', (encrypted_password, telegram_id))
                    migrated_count += 1
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Миграция завершена: зашифровано {migrated_count} записей")
        return migrated_count

# Глобальный экземпляр
encryption_manager = EncryptionManager()

# Функции для удобного импорта
def encrypt(data):
    return encryption_manager.encrypt(data)

def decrypt(data):
    return encryption_manager.decrypt(data)

# Автоматическая миграция при импорте
try:
    encryption_manager.migrate_old_data()
except Exception as e:
    logger.error(f"❌ Ошибка миграции: {e}")
