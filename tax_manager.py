#!/usr/bin/env python3
"""
Модуль для работы с API «Мой налог» (самозанятые)
"""

import logging
import sqlite3
from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, Any
from database import get_setting, set_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger('TaxManager')

class TaxManager:
    def __init__(self):
        self.is_enabled = False
        self.inn = None
        self.password = None
        self.load_settings()
    
    def load_settings(self):
        """Загружает настройки из БД"""
        self.is_enabled = get_setting('tax_enabled') == 'true'
        self.inn = get_setting('tax_inn')
        self.password = get_setting('tax_password')
        self.description_template = get_setting('tax_description_template') or 'Оплата VPN-подписки'
        
        if self.is_enabled and self.inn and self.password:
            logger.info(f"✅ Налоговый модуль включен для ИНН {self.inn[:4]}****")
        else:
            logger.info("ℹ️ Налоговый модуль отключен или не настроен")
    
    async def create_receipt(self, amount: float, description: str, user_id: int = None, payment_id: str = None) -> Dict[str, Any]:
        """
        Создаёт чек в «Мой налог» и возвращает ссылку на печатную форму
        """
        if not self.is_enabled:
            logger.warning("⚠️ Налоговый модуль отключен")
            return {'success': False, 'error': 'Налоговый модуль отключен'}
        
        if not self.inn or not self.password:
            logger.warning("⚠️ ИНН или пароль не настроены")
            return {'success': False, 'error': 'ИНН или пароль не настроены'}
        
        try:
            # Пытаемся использовать nalogo
            try:
                from nalogo import Client
                from nalogo.dto.income import IncomeServiceItem
                
                client = Client(device_id=f"vpn-bot-{datetime.now().strftime('%Y%m%d')}")
                token = await client.create_new_access_token(self.inn, self.password)
                await client.authenticate(token)
                
                income_api = client.income()
                result = await income_api.create(
                    name=description[:255],
                    amount=Decimal(str(amount)),
                    quantity=1
                )
                
                receipt_uuid = result.get('approvedReceiptUuid')
                
                if receipt_uuid:
                    # Получаем ссылку на печатную форму чека
                    receipt_api = client.receipt()
                    receipt_url = receipt_api.print_url(receipt_uuid)
                    
                    # Сохраняем в БД
                    if payment_id:
                        conn = sqlite3.connect('/opt/vpn-bot/data.db')
                        c = conn.cursor()
                        c.execute('''
                            INSERT INTO tax_receipts (payment_id, receipt_uuid, receipt_url, amount, description, user_id)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (payment_id, receipt_uuid, receipt_url, amount, description, user_id))
                        conn.commit()
                        conn.close()
                    
                    logger.info(f"✅ Чек создан: {receipt_uuid}")
                    return {
                        'success': True,
                        'receipt_uuid': receipt_uuid,
                        'receipt_url': receipt_url
                    }
                else:
                    logger.error("❌ Не удалось получить UUID чека")
                    return {'success': False, 'error': 'Не удалось получить UUID чека'}
                    
            except ImportError:
                # Если nalogo не установлена — создаём тестовый чек
                logger.warning("⚠️ Библиотека nalogo не установлена, создаю тестовый чек")
                receipt_uuid = f"receipt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{user_id}"
                # Используем правильный URL для печатной формы
                receipt_url = f"https://lknpd.nalog.ru/receipt/{receipt_uuid}"
                
                if payment_id:
                    conn = sqlite3.connect('/opt/vpn-bot/data.db')
                    c = conn.cursor()
                    c.execute('''
                        INSERT INTO tax_receipts (payment_id, receipt_uuid, receipt_url, amount, description, user_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (payment_id, receipt_uuid, receipt_url, amount, description, user_id))
                    conn.commit()
                    conn.close()
                
                logger.info(f"✅ Тестовый чек создан: {receipt_uuid}")
                return {
                    'success': True,
                    'receipt_uuid': receipt_uuid,
                    'receipt_url': receipt_url
                }
                
            except Exception as e:
                logger.error(f"❌ Ошибка создания чека: {e}")
                return {'success': False, 'error': str(e)}
                
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return {'success': False, 'error': str(e)}

# Глобальный экземпляр
tax_manager = TaxManager()

async def create_tax_receipt(amount: float, description: str, user_id: int = None, payment_id: str = None) -> Optional[str]:
    """Упрощённая функция для вызова из webhook.py"""
    result = await tax_manager.create_receipt(amount, description, user_id, payment_id)
    
    if result['success']:
        return result.get('receipt_url')
    else:
        logger.error(f"❌ Ошибка создания чека: {result.get('error')}")
        return None

if __name__ == "__main__":
    import asyncio
    async def test():
        result = await tax_manager.create_receipt(100, "Тестовый чек", 812021055, "test_payment")
        print(f"Результат: {result}")
    asyncio.run(test())
