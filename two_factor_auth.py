"""
Модуль двухфакторной аутентификации (2FA) для админки
Использует TOTP (Time-based One-Time Password)
"""
import pyotp
import qrcode
import sqlite3
import logging
from io import BytesIO
from base64 import b64encode
from datetime import datetime
from database import get_setting, set_setting

DB_PATH = '/opt/vpn-bot/data.db'
logger = logging.getLogger(__name__)

class TwoFactorAuth:
    def __init__(self):
        self.secret_key = None
        self.is_enabled = False
        self.load_settings()
    
    def load_settings(self):
        """Загружает настройки 2FA из БД"""
        self.secret_key = get_setting('2fa_secret_key')
        self.is_enabled = get_setting('2fa_enabled') == 'true'
        
        # Если секретного ключа нет - генерируем
        if not self.secret_key:
            self.generate_secret()
    
    def generate_secret(self):
        """Генерирует новый секретный ключ для 2FA"""
        self.secret_key = pyotp.random_base32()
        set_setting('2fa_secret_key', self.secret_key)
        logger.info("✅ Новый секретный ключ 2FA сгенерирован")
        return self.secret_key
    
    def get_qr_code(self, username=None):
        """Генерирует QR-код для настройки 2FA"""
        if not self.secret_key:
            self.generate_secret()
        
        # Создаём URI для TOTP
        issuer = "VPN4US Bot"
        account_name = username or "admin"
        totp_uri = pyotp.totp.TOTP(self.secret_key).provisioning_uri(
            name=account_name,
            issuer_name=issuer
        )
        
        # Генерируем QR-код
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(totp_uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Конвертируем в base64 для отображения в HTML
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = b64encode(buffered.getvalue()).decode()
        
        return {
            'qr_code': img_str,
            'secret_key': self.secret_key,
            'totp_uri': totp_uri
        }
    
    def verify_code(self, code):
        """Проверяет введённый код 2FA"""
        if not self.secret_key:
            return False
        
        totp = pyotp.TOTP(self.secret_key)
        return totp.verify(code)
    
    def enable_2fa(self):
        """Включает 2FA"""
        set_setting('2fa_enabled', 'true')
        self.is_enabled = True
        logger.info("✅ 2FA включена")
    
    def disable_2fa(self):
        """Выключает 2FA"""
        set_setting('2fa_enabled', 'false')
        self.is_enabled = False
        logger.info("❌ 2FA выключена")
    
    def get_status(self):
        """Возвращает статус 2FA"""
        return {
            'enabled': self.is_enabled,
            'has_secret': bool(self.secret_key)
        }

# Глобальный экземпляр
two_factor_auth = TwoFactorAuth()
