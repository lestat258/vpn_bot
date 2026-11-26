import sqlite3
import unittest
from unittest.mock import AsyncMock
from payment_receipt_resend import resend_order
from payment_admin_check import check_order
import test_payment_admin_view


class ResendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = test_payment_admin_view.JournalTests()
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        self.path = fixture.path
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE tax_receipts(payment_id TEXT,receipt_uuid TEXT,amount REAL,user_id INTEGER)")
            db.execute("INSERT INTO tax_receipts VALUES ('refund','receipt1',1,1)")
            db.execute("CREATE TABLE settings(key TEXT,value TEXT)")
            db.execute("INSERT INTO settings VALUES ('tax_inn','123456789012')")
            db.execute("UPDATE payment_processing SET tax_state='done',refund_state='none' WHERE payment_id='refund'")
        self.provider = AsyncMock(return_value={
            "id":"refund","status":"succeeded","paid":True,
            "amount":{"currency":"RUB","value":"1.00"},
            "metadata":{"telegram_id":"1","tariff_id":"1"},
            "refunded_amount":{"currency":"RUB","value":"0.00"}})
        self.fiscal = AsyncMock(return_value="active")
        self.sender = AsyncMock()

    async def resend(self, order=9):
        return await resend_order(self.path,order,provider=self.provider,fiscal=self.fiscal,sender=self.sender)

    async def test_existing_receipt_only(self):
        before=self.path.read_bytes()
        self.assertTrue((await self.resend())["ok"])
        self.sender.assert_awaited_once_with(self.path,1,
            "https://lknpd.nalog.ru/api/v1/receipt/123456789012/receipt1/print")
        self.assertEqual(before,self.path.read_bytes())

    async def test_history_pending_free_and_missing_blocked(self):
        for order in (1,8,12,999):
            self.assertFalse((await self.resend(order))["ok"])
        self.provider.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_returned_or_canceled_blocked(self):
        self.provider.return_value["refunded_amount"]["value"]="1.00"
        self.assertFalse((await self.resend())["ok"])
        self.provider.return_value["refunded_amount"]["value"]="0.00"
        self.fiscal.return_value="refund"
        self.assertFalse((await self.resend())["ok"])
        self.sender.assert_not_awaited()

    async def test_wrong_recipient_or_link_blocked(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tax_receipts SET user_id=2")
        self.assertFalse((await self.resend())["ok"])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tax_receipts SET user_id=1")
            db.execute("UPDATE payment_processing SET receipt_url='https://evil.invalid' WHERE payment_id='refund'")
        self.assertFalse((await self.resend())["ok"])
        self.sender.assert_not_awaited()

    async def test_changed_during_check_blocked(self):
        async def changed(*args):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE payment_processing SET refund_state='full_queued' WHERE payment_id='refund'")
            return "active"
        self.fiscal.side_effect=changed
        self.assertFalse((await self.resend())["ok"])
        self.sender.assert_not_awaited()

    async def test_ambiguous_delivery_not_retried(self):
        self.sender.side_effect=TimeoutError()
        self.assertFalse((await self.resend())["ok"])
        self.sender.assert_awaited_once()

    async def test_summary_checks_full_amount_and_fns_reason(self):
        self.assertTrue((await check_order(self.path,9,provider=self.provider,fiscal=self.fiscal))["ok"])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payment_processing SET refund_state='full_done',tax_state='canceled' WHERE payment_id='refund'")
        self.fiscal.return_value="refund"
        self.assertFalse((await check_order(self.path,9,provider=self.provider,fiscal=self.fiscal))["ok"])
        self.provider.return_value["refunded_amount"]["value"]="1.00"
        self.assertTrue((await check_order(self.path,9,provider=self.provider,fiscal=self.fiscal))["ok"])
        self.fiscal.return_value="other_canceled"
        self.assertFalse((await check_order(self.path,9,provider=self.provider,fiscal=self.fiscal))["ok"])
