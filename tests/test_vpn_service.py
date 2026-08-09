#!/usr/bin/env python3
"""
Unit-тесты для VPN сервиса
"""
import unittest
import asyncio
import sqlite3
import os
import sys
import importlib
sys.path.insert(0, '/opt/vpn-bot/tests')
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock

sys.path.insert(0, '/opt/vpn-bot')

from test_helpers import create_test_db, create_test_user, create_test_tariff
from services.vpn_service import VPNService, vpn_service


class TestVPNService(unittest.TestCase):
    """Тесты для VPN сервиса"""

    def setUp(self):
        """Подготовка перед каждым тестом"""
        self.temp_db = create_test_db()

        # Патчим DB_PATH на тестовую БД
        self.db_patch = patch('services.vpn_service.DB_PATH', self.temp_db)
        self.db_patch.start()

        # Создаем тестового пользователя
        self.user_id = create_test_user(self.temp_db)

        # Создаем тестовый тариф
        self.tariff_id = create_test_tariff(self.temp_db)

    def tearDown(self):
        """Очистка после каждого теста"""
        self.db_patch.stop()
        if os.path.exists(self.temp_db):
            os.unlink(self.temp_db)

    def test_generate_short_email(self):
        """Тест генерации email"""
        from utils import generate_short_email

        email1 = generate_short_email(12345, 'test_user')
        email2 = generate_short_email(12345, 'test_user')

        self.assertTrue(email1.startswith('user_test_user_'))
        self.assertNotEqual(email1, email2)

    @patch('services.vpn_service.server_manager')
    @patch('services.vpn_service.XUIClient')
    def test_create_subscription_success(self, mock_xui, mock_server_manager):
        """Тест успешного создания подписки"""
        # Настраиваем мок сервера
        mock_server_manager.get_best_server.return_value = {
            'id': 1,
            'name': 'Test Server',
            'url': 'https://test.server',
            'api_token': 'test_token'
        }
        
        # Мокаем get_server_sub_url
        mock_server_manager.get_server_sub_url.return_value = 'https://test.server/sub/'
        mock_server_manager.get_sub_url_for_server.return_value = 'https://test.server/sub/'
        
        # Настраиваем мок XUI
        mock_xui_instance = Mock()
        mock_xui_instance.get_inbounds.return_value = [
            {'id': 1, 'enable': True, 'settings': '{"clients": []}'}
        ]
        mock_xui_instance.add_client.return_value = {
            'subId': 'test_subid',
            'id': 'test_uuid'
        }
        mock_xui.return_value = mock_xui_instance

        result = asyncio.run(
            vpn_service.create_subscription(
                telegram_id=self.user_id,
                tariff_id=self.tariff_id,
                days=30,
                traffic_gb=10,
                ip_limit=3
            )
        )

        self.assertTrue(result['success'])
        self.assertIn('test_subid', result['link'])
        mock_xui_instance.add_client.assert_called_once()

    @patch('services.vpn_service.server_manager')
    @patch('services.vpn_service.XUIClient')
    def test_create_subscription_no_server(self, mock_xui, mock_server_manager):
        """Тест ошибки при отсутствии серверов"""
        mock_server_manager.get_best_server.return_value = None

        result = asyncio.run(
            vpn_service.create_subscription(
                telegram_id=self.user_id,
                tariff_id=self.tariff_id,
                days=30
            )
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'Нет доступных серверов')

    @patch('services.vpn_service.server_manager')
    @patch('services.vpn_service.XUIClient')
    def test_create_subscription_no_inbounds(self, mock_xui, mock_server_manager):
        """Тест ошибки при отсутствии инбаундов"""
        mock_server_manager.get_best_server.return_value = {
            'id': 1,
            'url': 'https://test.server',
            'api_token': 'test_token'
        }

        mock_xui_instance = Mock()
        mock_xui_instance.get_inbounds.return_value = []
        mock_xui.return_value = mock_xui_instance

        result = asyncio.run(
            vpn_service.create_subscription(
                telegram_id=self.user_id,
                tariff_id=self.tariff_id,
                days=30
            )
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'Нет инбаундов на сервере')

    @patch('services.vpn_service.server_manager')
    @patch('services.vpn_service.XUIClient')
    def test_extend_subscription(self, mock_xui, mock_server_manager):
        """Тест продления подписки"""
        # Создаем подписку напрямую в БД
        conn = sqlite3.connect(self.temp_db)
        c = conn.cursor()
        end_date = (datetime.now() + timedelta(days=30)).isoformat()
        c.execute('''
            INSERT INTO subscriptions
            (telegram_id, tariff_id, xui_client_uid, xui_email, end_date, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (self.user_id, self.tariff_id, 'test_uuid', 'test@email.com', end_date))
        subscription_id = c.lastrowid
        conn.commit()
        conn.close()

        mock_server_manager.get_best_server.return_value = {
            'id': 1,
            'url': 'https://test.server',
            'api_token': 'test_token'
        }

        mock_xui_instance = Mock()
        mock_xui_instance.update_client_expiry.return_value = True
        mock_xui.return_value = mock_xui_instance

        result = asyncio.run(
            vpn_service.extend_subscription(
                subscription_id=subscription_id,
                days_to_add=30,
                tariff_id=self.tariff_id
            )
        )

        self.assertTrue(result['success'])

        # Проверяем, что дата изменилась
        conn = sqlite3.connect(self.temp_db)
        c = conn.cursor()
        c.execute('SELECT end_date FROM subscriptions WHERE id = ?', (subscription_id,))
        new_end_date = c.fetchone()[0]
        conn.close()

        self.assertTrue(
            datetime.fromisoformat(new_end_date) >
            datetime.fromisoformat(end_date) + timedelta(days=29)
        )


@unittest.skip("Интеграционные тесты требуют реальной БД и будут запускаться отдельно")
class TestVPNServiceIntegration(unittest.TestCase):
    """Интеграционные тесты для VPN сервиса"""

    def setUp(self):
        self.temp_db = create_test_db()
        self.user_id = create_test_user(self.temp_db)
        self.tariff_id = create_test_tariff(self.temp_db)

        # Патчим DB_PATH
        self.db_patch = patch('database.DB_PATH', self.temp_db)
        self.db_patch.start()
        
        # Перезагружаем модули, чтобы подхватили новый DB_PATH
        importlib.reload(sys.modules['utils.helpers'])
        importlib.reload(sys.modules['utils'])

    def tearDown(self):
        self.db_patch.stop()
        
        # Возвращаем модули в исходное состояние
        importlib.reload(sys.modules['utils.helpers'])
        importlib.reload(sys.modules['utils'])
        
        if os.path.exists(self.temp_db):
            os.unlink(self.temp_db)

    def test_get_tariff_by_id(self):
        """Тест получения тарифа по ID"""
        from utils import get_tariff_by_id

        tariff = get_tariff_by_id(self.tariff_id)
        self.assertIsNotNone(tariff)
        self.assertEqual(tariff[0], self.tariff_id)
        self.assertEqual(tariff[1], 'Тестовый тариф')

    def test_get_user_by_telegram_id(self):
        """Тест получения пользователя по telegram_id"""
        from utils import get_user_by_telegram_id

        user = get_user_by_telegram_id(self.user_id)
        self.assertIsNotNone(user)
        self.assertEqual(user[1], self.user_id)


if __name__ == '__main__':
    unittest.main()
