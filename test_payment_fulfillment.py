import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from payment_fulfillment import process_verified_payment


class FakeVpn:
    def __init__(self, *, fail=False, raise_error=False, existing_id=7):
        self.create_count = 0
        self.extend_count = 0
        self.fail = fail
        self.raise_error = raise_error
        self.existing_id = existing_id

    async def create_subscription(self, **kwargs):
        self.create_count += 1
        self.allow_additional_key = kwargs.get("allow_additional_key", False)
        if self.raise_error:
            raise TimeoutError('Unknown remote result')
        if self.fail:
            return {'success': False, 'error': 'remote error'}
        return {'success': True, 'subscription_id': self.existing_id,
                'link': 'https://example.invalid/sub/test'}

    async def extend_subscription(self, **kwargs):
        self.extend_count += 1
        if self.raise_error:
            raise TimeoutError('Unknown remote result')
        if self.fail:
            return {'success': False}
        return {'success': True, 'new_end_date': '2026-12-24'}

    async def get_subscription_link(self, subscription_id):
        return 'https://example.invalid/sub/test'


class PaymentFulfillmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'data.db')
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE payments (payment_id TEXT PRIMARY KEY,
                   telegram_id INTEGER, tariff_id INTEGER, amount_rub REAL,
                   status TEXT DEFAULT 'pending');
                CREATE TABLE tariffs (id INTEGER PRIMARY KEY, name TEXT,
                   duration_days INTEGER, traffic_gb REAL, ip_limit INTEGER);
                CREATE TABLE subscriptions (id INTEGER PRIMARY KEY,
                   telegram_id INTEGER, is_active INTEGER, end_date TEXT);
                CREATE TABLE payment_processing (
                   payment_id TEXT PRIMARY KEY, is_legacy INTEGER NOT NULL DEFAULT 0,
                   payment_state TEXT NOT NULL DEFAULT 'pending',
                   vpn_state TEXT NOT NULL DEFAULT 'not_started',
                   tax_state TEXT NOT NULL DEFAULT 'not_started',
                   refund_state TEXT NOT NULL DEFAULT 'none',
                   subscription_id INTEGER, receipt_uuid TEXT UNIQUE,
                   receipt_url TEXT, last_error TEXT,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO tariffs VALUES (5,'120 руб',30,0,3);
                INSERT INTO payments VALUES ('new1',1001,5,120,'pending');
                INSERT INTO payments VALUES ('renew1',1001,5,120,'pending');
                INSERT INTO payments VALUES ('fail1',1001,5,120,'pending');
                INSERT INTO payments VALUES ('notify1',1001,5,120,'pending');
                INSERT INTO payments VALUES ('bad1',1001,5,120,'pending');
                INSERT INTO subscriptions VALUES (7,1001,1,'2026-12-01');
            ''')
        self.messages = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def notify(self, user_id, text):
        self.messages.append((user_id, text))

    def payload(self, payment_id, action='new_key', sub_id=None):
        md = {'telegram_id': '1001', 'tariff_id': '5', 'action': action}
        if sub_id is not None:
            md['subscription_id'] = sub_id
        return {'object': {'id': payment_id, 'metadata': md}}

    async def run_payment(self, pid, payload, vpn, notify=None):
        return await process_verified_payment(
            pid, payload, db_path=self.path, vpn_api=vpn,
            notifier=notify or self.notify,
        )

    def row(self, pid):
        with sqlite3.connect(self.path) as db:
            return db.execute('''SELECT p.status, l.vpn_state, l.subscription_id
                FROM payments p LEFT JOIN payment_processing l
                  ON p.payment_id=l.payment_id WHERE p.payment_id=?''', (pid,)).fetchone()

    async def test_new_and_duplicate_webhook(self):
        vpn = FakeVpn(existing_id=8)
        self.assertTrue(await self.run_payment('new1', self.payload('new1'), vpn))
        self.assertTrue(vpn.allow_additional_key)
        self.assertEqual(self.row('new1'), ('paid', 'done', 8))
        self.assertTrue(await self.run_payment('new1', self.payload('new1'), vpn))
        self.assertEqual(vpn.create_count, 1)
        self.assertEqual(len(self.messages), 1)

    async def test_renewal_with_known_subscription(self):
        vpn = FakeVpn()
        self.assertTrue(await self.run_payment('renew1', self.payload('renew1','renew',7), vpn))
        self.assertEqual(vpn.extend_count, 1)
        self.assertEqual(vpn.create_count, 0)
        self.assertEqual(self.row('renew1'), ('paid', 'done', 7))

    async def test_renewal_without_explicit_sub_uses_active(self):
        vpn = FakeVpn()
        self.assertTrue(await self.run_payment('renew1', self.payload('renew1','renew'), vpn))
        self.assertEqual(vpn.extend_count, 1)

    async def test_error_marks_review_and_never_retries(self):
        vpn = FakeVpn(raise_error=True)
        self.assertFalse(await self.run_payment('fail1', self.payload('fail1'), vpn))
        self.assertEqual(self.row('fail1'), ('pending', 'needs_review', None))
        self.assertFalse(await self.run_payment('fail1', self.payload('fail1'), vpn))
        self.assertEqual(vpn.create_count, 1)

    async def test_failure_result_marks_review(self):
        vpn = FakeVpn(fail=True)
        self.assertFalse(await self.run_payment('fail1', self.payload('fail1'), vpn))
        self.assertEqual(self.row('fail1')[1], 'needs_review')

    async def test_notification_failure_does_not_repeat_vpn(self):
        async def bad_notify(*args):
            raise RuntimeError('Telegram unavailable')
        vpn = FakeVpn()
        self.assertTrue(await self.run_payment('notify1', self.payload('notify1'), vpn, bad_notify))
        self.assertEqual(self.row('notify1'), ('paid','done',7))
        self.assertTrue(await self.run_payment('notify1', self.payload('notify1'), vpn))
        self.assertEqual(vpn.create_count, 1)

    async def test_metadata_mismatch_rejected_before_claim(self):
        vpn = FakeVpn()
        payload = self.payload('bad1')
        payload['object']['metadata']['telegram_id'] = '123'
        self.assertFalse(await self.run_payment('bad1', payload, vpn))
        self.assertEqual(vpn.create_count, 0)
        self.assertEqual(self.row('bad1'), ('pending', None, None))

    async def test_other_users_subscription_rejected(self):
        vpn = FakeVpn()
        self.assertFalse(await self.run_payment('renew1', self.payload('renew1','renew',9), vpn))
        self.assertEqual(self.row('renew1')[1], 'needs_review')
        self.assertEqual(vpn.extend_count, 0)


if __name__ == '__main__':
    unittest.main()
