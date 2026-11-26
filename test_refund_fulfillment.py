import asyncio
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
from pathlib import Path
from decimal import Decimal
from refund_fulfillment import (
    record_canceled, record_refund, scope, claim_cancel, drain_refunds,
    receipt_cancellation, cancel_and_confirm, reconcile_refund
)
import test_tax_fulfillment

# Reuse actual schema fixture without inheriting its test cases.
class RefundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_tax_fulfillment.TaxTests.asyncSetUp(self)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payments SET id=8,created_at='2026-11-26 12:16:00' WHERE payment_id='new'")
            db.execute("UPDATE payments SET created_at='2026-11-26 12:14:00' WHERE payment_id='old'")
        self.cancels = 0

    async def cleanup(self):
        self.tmp.cleanup()

    def state(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT refund_state,tax_state FROM payment_processing WHERE payment_id='new'").fetchone()

    def paid(self):
        from payment_ledger import PaymentLedger
        from tax_fulfillment import claim, complete
        ledger = PaymentLedger(self.path)
        self.assertEqual(ledger.claim("new"), "claimed")
        ledger.complete("new", 1)
        self.assertIsNotNone(claim(self.path,"new"))
        complete(self.path,"new",100,Decimal("1"),"test","receipt-1","https://example.invalid")

    async def verifier(self, pid):
        return pid == "new"

    async def canceler(self, path, uid, amount):
        self.assertEqual(uid,"receipt-1")
        self.assertEqual(amount,Decimal("1"))
        self.cancels += 1

    async def test_cancel_and_repeat(self):
        self.assertEqual(record_canceled(self.path,"new"),"done")
        self.assertEqual(record_canceled(self.path,"new"),"done")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM payments WHERE payment_id='new'").fetchone()[0],"canceled")

    async def test_historical_is_immutable(self):
        self.assertEqual(scope(self.path,"old"),"historical")
        self.assertEqual(record_refund(self.path,"old",1),"ignored")
        self.assertEqual(record_canceled(self.path,"old"),"ignored")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT refund_state,tax_state FROM payment_processing WHERE payment_id='old'").fetchone(),("none","not_started"))

    async def test_full_refund_and_replay(self):
        self.paid()
        self.assertEqual(record_refund(self.path,"new",1),"full_queued")
        await asyncio.gather(*(drain_refunds(self.path,self.verifier,canceler=self.canceler) for _ in range(4)))
        self.assertEqual(self.state(),("full_done","canceled"))
        self.assertEqual(record_refund(self.path,"new",1),"full_done")
        await drain_refunds(self.path,self.verifier,canceler=self.canceler)
        self.assertEqual(self.cancels,1)

    async def test_partial_then_full_held(self):
        self.paid()
        self.assertEqual(record_refund(self.path,"new","0.50"),"partial_review")
        self.assertEqual(record_refund(self.path,"new",1),"full_review")
        await drain_refunds(self.path,self.verifier,canceler=self.canceler)
        self.assertEqual(self.cancels,0)

    async def test_refund_before_receipt(self):
        self.assertEqual(record_refund(self.path,"new",1),"full_review")
        from payment_ledger import PaymentLedger
        self.assertEqual(PaymentLedger(self.path).claim("new"),"held")
        self.assertEqual(self.state(),("full_review","not_started"))

    async def test_uncertain_cancellation_never_repeated(self):
        self.paid()
        record_refund(self.path,"new",1)
        async def fail(*args):
            self.cancels += 1
            raise TimeoutError()
        await drain_refunds(self.path,self.verifier,canceler=fail)
        await drain_refunds(self.path,self.verifier,canceler=self.canceler)
        self.assertEqual(self.cancels,1)
        self.assertEqual(self.state(),("full_review","done"))

    async def test_crash_after_claim_never_repeated(self):
        self.paid()
        record_refund(self.path,"new",1)
        self.assertIsNotNone(claim_cancel(self.path,"new"))
        await drain_refunds(self.path,self.verifier,canceler=self.canceler)
        self.assertEqual(self.state(),("full_processing","done"))
        self.assertEqual(self.cancels,0)

    def receipt_data(self, cancellation=None):
        return {"receiptId":"receipt-1","inn":"123456789012",
                "totalAmount":1,"cancellationInfo":cancellation}

    def mock_client(self, before, after):
        client = Mock()
        client.receipt.return_value.json = AsyncMock(side_effect=[before,after])
        client.income.return_value.cancel = AsyncMock(return_value={"unrelated":True})
        client.income.return_value.http._handle_401_response = AsyncMock()
        return client

    async def test_confirmation_requires_uuid_and_reason(self):
        good = self.receipt_data({"comment":"Возврат средств"})
        self.assertEqual(receipt_cancellation(good,"receipt-1",Decimal("1"),"123456789012"),
                         {"comment":"Возврат средств"})
        for bad in ({**good,"receiptId":"other"}, {**good,"totalAmount":2},
                    {**good,"inn":"999999999999"}, {"receiptId":"receipt-1"}):
            with self.assertRaises((RuntimeError, ValueError)):
                receipt_cancellation(bad,"receipt-1",Decimal("1"),"123456789012")

    async def test_authenticated_read_before_and_after(self):
        client = self.mock_client(self.receipt_data(),
                                  self.receipt_data({"comment":"Возврат средств"}))
        await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
        self.assertEqual(client.receipt.return_value.json.await_count,2)
        client.income.return_value.cancel.assert_awaited_once_with("receipt-1","refund")
        self.assertIsNone(await client.income.return_value.http._handle_401_response())

    async def test_previously_canceled_never_posted(self):
        client = self.mock_client(self.receipt_data({"comment":"Возврат средств"}), self.receipt_data())
        with self.assertRaises(RuntimeError):
            await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
        client.income.return_value.cancel.assert_not_awaited()

    async def test_missing_or_wrong_confirmation_never_reposted(self):
        for result in (None, {}, {"comment":"Чек сформирован ошибочно"}, {"unknown":"value"}):
            client = self.mock_client(self.receipt_data(),self.receipt_data(result))
            with self.assertRaises(RuntimeError):
                await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
            client.income.return_value.cancel.assert_awaited_once()

    async def test_post_timeout_then_confirmed_by_get(self):
        client = self.mock_client(self.receipt_data(),
                                  self.receipt_data({"comment":"Возврат средств"}))
        client.income.return_value.cancel.side_effect = TimeoutError()
        await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
        self.assertEqual(client.receipt.return_value.json.await_count,2)
        self.assertEqual(client.income.return_value.http.timeout,90.0)
        client.income.return_value.cancel.assert_awaited_once()

    async def test_post_timeout_get_retries_only(self):
        client = self.mock_client(self.receipt_data(),self.receipt_data())
        client.income.return_value.cancel.side_effect = TimeoutError()
        client.receipt.return_value.json.side_effect = [
            self.receipt_data(), TimeoutError(), self.receipt_data(),
            self.receipt_data({"comment":"Возврат средств"})]
        await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,0,0))
        self.assertEqual(client.receipt.return_value.json.await_count,4)
        client.income.return_value.cancel.assert_awaited_once()

    async def test_post_timeout_active_no_replay(self):
        client = self.mock_client(self.receipt_data(),self.receipt_data())
        client.income.return_value.cancel.side_effect = TimeoutError()
        with self.assertRaises(RuntimeError):
            await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
        client.income.return_value.cancel.assert_awaited_once()

    async def test_precheck_timeout_never_posts(self):
        client = self.mock_client(TimeoutError(), self.receipt_data())
        with self.assertRaises(TimeoutError):
            await cancel_and_confirm(client,"receipt-1",Decimal("1"),"123456789012","refund",delays=(0,))
        client.income.return_value.cancel.assert_not_awaited()

    async def test_read_only_reconciliation(self):
        self.paid()
        record_refund(self.path,"new",1)
        self.assertIsNotNone(claim_cancel(self.path,"new"))
        checker = AsyncMock(return_value=False)
        self.assertFalse(await reconcile_refund(self.path,"new",self.verifier,checker=checker))
        self.assertEqual(self.state(),("full_processing","done"))
        checker.return_value = True
        self.assertTrue(await reconcile_refund(self.path,"new",self.verifier,checker=checker))
        self.assertEqual(self.state(),("full_done","canceled"))
        self.assertFalse(await reconcile_refund(self.path,"old",self.verifier,checker=checker))
        self.assertEqual(checker.await_count,2)

    async def test_date_boundary(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payments SET created_at='2026-11-26 12:14:59' WHERE payment_id='new'")
        self.assertEqual(scope(self.path,"new"),"historical")

