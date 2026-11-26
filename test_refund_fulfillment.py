import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from refund_fulfillment import (
    record_canceled, record_refund, scope, claim_cancel, drain_refunds,
    confirm_cancel_response
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

    async def test_confirmation_requires_uuid_and_reason(self):
        good = {"incomeInfo":{"approvedReceiptUuid":"receipt-1","cancellationInfo":{"comment":"Возврат средств"}}}
        self.assertTrue(confirm_cancel_response(good,"receipt-1"))
        self.assertFalse(confirm_cancel_response(good,"receipt-2"))
        self.assertFalse(confirm_cancel_response({"ok":True},"receipt-1"))

    async def test_date_boundary(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE payments SET created_at='2026-11-26 12:14:59' WHERE payment_id='new'")
        self.assertEqual(scope(self.path,"new"),"historical")

