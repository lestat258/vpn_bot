import sqlite3
import unittest
from unittest.mock import AsyncMock
from decimal import Decimal
from payment_admin_check import check_order,provider_state
from payment_admin_view import load_journal,display_date
import test_payment_admin_view

class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture=test_payment_admin_view.JournalTests()
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        self.path=fixture.path
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE tax_receipts(payment_id TEXT,receipt_uuid TEXT,amount REAL,user_id INTEGER)")
            db.execute("INSERT INTO tax_receipts VALUES ('refund','receipt1',1,1)")
        self.provider=AsyncMock(return_value={
            "id":"refund","status":"succeeded","paid":True,"amount":{"currency":"RUB","value":"1.00"},
            "metadata":{"telegram_id":"1","tariff_id":"1"},
            "refunded_amount":{"currency":"RUB","value":"1.00"}})
        self.fiscal=AsyncMock(return_value="refund")

    async def check(self,order=9):
        return await check_order(self.path,order,provider=self.provider,fiscal=self.fiscal)

    async def test_confirmed_refund_no_changes(self):
        before=self.path.read_bytes()
        d=await self.check()
        labels={x["label"]:x["value"] for x in d["lines"]}
        self.assertEqual(labels["Возвращено в ЮKassa"],"1.00 ₽")
        self.assertEqual(labels["ФНС"],"Аннулирован: возврат средств")
        self.assertEqual(before,self.path.read_bytes())

    async def test_historical_and_free_never_contact_api(self):
        for order in (1,12,999):
            await self.check(order)
        self.provider.assert_not_awaited()
        self.fiscal.assert_not_awaited()

    async def test_wrong_payment_identity(self):
        self.provider.return_value["id"]="wrong"
        d=await self.check()
        self.assertIn("Не удалось подтвердить данные",[x["value"] for x in d["lines"]])

    async def test_missing_receipt_does_not_claim_no_income(self):
        d=await self.check(8)
        self.fiscal.assert_not_awaited()
        self.assertTrue(any("не подтверждено" in x["value"] for x in d["lines"]))

    async def test_mismatched_local_receipt_blocks_fns(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tax_receipts SET amount=2")
        await self.check()
        self.fiscal.assert_not_awaited()

    async def test_fiscal_unavailable_and_mismatch(self):
        self.fiscal.side_effect=TimeoutError()
        d=await self.check()
        self.assertIn("Не удалось подтвердить состояние чека",[x["value"] for x in d["lines"]])
        self.fiscal.side_effect=None
        self.fiscal.return_value="active"
        d=await self.check()
        self.assertIn("Расхождение с локальным статусом",[x["value"] for x in d["lines"]])

    async def test_mobile_date_and_returned_status(self):
        d=load_journal(self.path,"refund")["payments"][0]
        self.assertEqual(d["payment_display"]["label"],"Возвращён")
        self.assertEqual(d["display_date"],"26.11.2026 15:33:00 МСК")
        self.assertIn("из базы",display_date("2026-11-25 12:00:00",False))

