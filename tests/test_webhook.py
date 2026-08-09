#!/usr/bin/env python3
"""
Тесты для вебхука
"""
import unittest
import json
import asyncio
import sqlite3
import os
import sys
sys.path.insert(0, '/opt/vpn-bot/tests')
from datetime import datetime, timedelta
from unittest.mock import patch, Mock, AsyncMock, ANY

sys.path.insert(0, '/opt/vpn-bot')

from test_helpers import create_test_db, create_test_user, create_test_tariff
from webhook import handle_webhook, process_payment_success


class TestWebhook(unittest.IsolatedAsyncioTestCase):
    """Тесты для вебхука"""
    
    async def asyncSetUp(self):
        """Подготовка перед тестами"""
        self.temp_db = create_test_db()
        self.user_id = create_test_user(self.temp_db)
        self.tariff_id = create_test_tariff(self.temp_db)
        
        # Патчим DB_PATH
        self.db_patch = patch('webhook.DB_PATH', self.temp_db)
        self.db_patch.start()
        
        # Создаем тестовый платеж
        conn = sqlite3.connect(self.temp_db)
        c = conn.cursor()
        c.execute('''
            INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status)
            VALUES (?, ?, ?, ?, ?)
        ''', ('test_payment_123', self.user_id, self.tariff_id, 100, 'pending'))
        conn.commit()
        conn.close()
    
    async def asyncTearDown(self):
        """Очистка после тестов"""
        self.db_patch.stop()
        if os.path.exists(self.temp_db):
            os.unlink(self.temp_db)
    
    async def test_handle_webhook_invalid_json(self):
        """Тест обработки невалидного JSON"""
        request = Mock()
        request.json = AsyncMock(side_effect=json.JSONDecodeError('Invalid JSON', '', 0))
        
        response = await handle_webhook(request)
        self.assertEqual(response.status, 400)
    
    async def test_handle_webhook_wrong_event(self):
        """Тест игнорирования не payment.succeeded событий"""
        request = Mock()
        request.json = AsyncMock(return_value={'event': 'payment.canceled'})
        
        response = await handle_webhook(request)
        self.assertEqual(response.status, 200)
    
    async def test_handle_webhook_no_payment_id(self):
        """Тест отсутствия payment_id"""
        request = Mock()
        request.json = AsyncMock(return_value={'event': 'payment.succeeded', 'object': {}})
        
        response = await handle_webhook(request)
        self.assertEqual(response.status, 400)
    
    @patch('webhook.vpn_service')
    @patch('database.get_setting')
    async def test_process_payment_success_new_key(self, mock_get_setting, mock_vpn_service):
        """Тест обработки успешного платежа (новый ключ)"""
        mock_get_setting.return_value = None
        
        mock_vpn_service.create_subscription = AsyncMock(return_value={
            'success': True,
            'end_date': datetime.now() + timedelta(days=30),
            'link': 'https://test.link/subid'
        })
        
        data = {
            'event': 'payment.succeeded',
            'object': {
                'id': 'test_payment_123',
                'metadata': {'action': 'new_key'}
            }
        }
        
        result = await process_payment_success('test_payment_123', data)
        self.assertTrue(result)
        
        # Проверяем статус платежа
        conn = sqlite3.connect(self.temp_db)
        c = conn.cursor()
        c.execute('SELECT status FROM payments WHERE payment_id = ?', ('test_payment_123',))
        status = c.fetchone()[0]
        conn.close()
        self.assertEqual(status, 'paid')
    
    @patch('webhook.vpn_service')
    @patch('database.get_setting')
    async def test_process_payment_idempotency(self, mock_get_setting, mock_vpn_service):
        """Тест idempotency - повторный вебхук не должен обрабатываться"""
        mock_get_setting.return_value = None
        
        mock_vpn_service.create_subscription = AsyncMock(return_value={
            'success': True,
            'end_date': datetime.now() + timedelta(days=30),
            'link': 'https://test.link/subid'
        })
        
        data = {
            'event': 'payment.succeeded',
            'object': {
                'id': 'test_payment_456',
                'metadata': {'action': 'new_key'}
            }
        }
        
        # Создаем платеж в БД
        conn = sqlite3.connect(self.temp_db)
        c = conn.cursor()
        c.execute('''
            INSERT INTO payments (payment_id, telegram_id, tariff_id, amount_rub, status)
            VALUES (?, ?, ?, ?, ?)
        ''', ('test_payment_456', self.user_id, self.tariff_id, 100, 'pending'))
        conn.commit()
        conn.close()
        
        # Первый вызов
        await process_payment_success('test_payment_456', data)
        
        # Второй вызов (должен вернуть True без обработки)
        result = await process_payment_success('test_payment_456', data)
        self.assertTrue(result)
        
        # Проверяем, что create_subscription вызван только 1 раз
        self.assertEqual(mock_vpn_service.create_subscription.call_count, 1)


if __name__ == '__main__':
    unittest.main()
