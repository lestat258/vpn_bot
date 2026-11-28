"""NPD advances: only isolated SQLite and mocked receipt calls."""
import asyncio
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock
import test_balance_ledger as prior
from balance_ledger import register_deposit, credit_verified, record_deposit_change, admin_adjust
from tax_fulfillment import claim, queue_verified, drain_queue, queued_ids, process_tax_receipt


class DepositFiscalTests(unittest.TestCase):
    def setUp(self):
        self.f=prior.BalanceTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.path=self.f.path
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                ALTER TABLE payment_processing ADD COLUMN receipt_uuid TEXT;
                ALTER TABLE payment_processing ADD COLUMN receipt_url TEXT;
                ALTER TABLE payment_processing ADD COLUMN created_at TEXT;
                CREATE TABLE tax_receipts(payment_id TEXT,receipt_uuid TEXT,receipt_url TEXT,
                    amount REAL,description TEXT,user_id INTEGER);
                INSERT INTO settings VALUES('tax_deposit_policy','npd_prepayment');
                UPDATE settings SET value='true' WHERE key='tax_enabled';
            """)

    def sql(self,sql,*params):
        with sqlite3.connect(self.path) as db:
            return db.execute(sql,params).fetchall()

    def state(self):
        return self.sql("SELECT tax_state FROM payment_processing WHERE payment_id='new'")[0][0]

    def test_verified_credit_and_queue_once(self):
        self.assertEqual(self.f.credit()[0],"credited")
        self.assertEqual(self.f.credit()[0],"done")
        self.assertEqual(self.state(),"queued")
        self.assertEqual(queued_ids(self.path),["new"])
        self.assertEqual(self.sql("SELECT count(*) FROM balance_operations"),[(1,)])

    def test_disabled_history_never_requeued(self):
        self.sql("UPDATE settings SET value='false' WHERE key='tax_enabled'")
        self.f.credit()
        self.sql("UPDATE settings SET value='true' WHERE key='tax_enabled'")
        self.f.credit()
        self.assertEqual(self.state(),"skipped")
        self.assertEqual(queued_ids(self.path),[])

    def test_policy_missing_holds_for_review(self):
        self.sql("DELETE FROM settings WHERE key='tax_deposit_policy'")
        self.f.credit()
        self.assertEqual(self.state(),"needs_review")
        self.assertEqual(queued_ids(self.path),[])

    def test_policy_revoked_after_credit(self):
        self.f.credit()
        self.sql("DELETE FROM settings WHERE key='tax_deposit_policy'")
        self.assertIsNone(claim(self.path,"new"))
        self.assertEqual(self.state(),"needs_review")

    def test_uncredited_or_unknown_order_not_queued(self):
        queue_verified(self.path,"new")
        queue_verified(self.path,"old")
        self.assertEqual(queued_ids(self.path),[])
        self.assertEqual(self.sql("SELECT count(*) FROM payment_processing"),[(0,)])

    def test_ledger_identity_mismatch_holds(self):
        self.f.credit()
        self.sql("UPDATE balance_operations SET telegram_id=43")
        self.assertIsNone(claim(self.path,"new"))
        self.assertEqual(self.state(),"needs_review")

    def test_refund_before_claim_moves_queue_to_review(self):
        self.f.credit()
        record_deposit_change(self.path,"new",self.f.meta,refund="2.00")
        self.assertEqual(self.state(),"needs_review")
        self.assertEqual(queued_ids(self.path),[])
        self.assertEqual(self.sql("SELECT refund_state FROM payment_processing"),[("partial_review",)])
        self.assertIsNone(claim(self.path,"new"))

    def test_bonus_never_creates_receipt_queue(self):
        admin_adjust(self.path,"gift",42,99,"50")
        self.assertEqual(queued_ids(self.path),[])
        self.assertEqual(self.sql("SELECT count(*) FROM payment_processing"),[(0,)])

    def test_worker_uses_original_capture_date_and_one_receipt(self):
        self.f.credit()
        issuer=AsyncMock(return_value=("synthetic-receipt","https://example.invalid/receipt"))
        notifier=AsyncMock()
        verifier=AsyncMock(return_value="2026-11-28T12:00:00Z")
        for _ in range(2):
            asyncio.run(drain_queue(self.path,verifier,issuer=issuer,notifier=notifier))
        issuer.assert_awaited_once_with(self.path,Decimal("10.5"),"Предоплата услуг VPN4US")
        notifier.assert_awaited_once()
        verifier.assert_awaited_once_with("new")
        self.assertEqual(self.state(),"done")
        self.assertEqual(self.sql("SELECT amount,description FROM tax_receipts"),[(10.5,"Предоплата услуг VPN4US")])

    def test_ambiguous_issue_not_retried(self):
        self.f.credit()
        issuer=AsyncMock(side_effect=TimeoutError())
        verifier=AsyncMock(return_value="2026-11-28T12:00:00Z")
        for _ in range(2):
            asyncio.run(drain_queue(self.path,verifier,issuer=issuer,notifier=AsyncMock()))
        issuer.assert_awaited_once()
        self.assertEqual(self.state(),"needs_review")
        self.assertEqual(self.sql("SELECT count(*) FROM tax_receipts"),[(0,)])

    def test_refund_after_receipt_preserves_receipt_for_manual_review(self):
        self.f.credit()
        issuer=AsyncMock(return_value=("synthetic-receipt","https://example.invalid/receipt"))
        self.assertTrue(asyncio.run(process_tax_receipt("new",db_path=self.path,issuer=issuer,notifier=AsyncMock())))
        record_deposit_change(self.path,"new",self.f.meta,refund="10.50")
        self.assertEqual(self.state(),"done")
        self.assertEqual(self.sql("SELECT refund_state FROM payment_processing"),[("full_review",)])
        self.assertEqual(self.sql("SELECT count(*) FROM tax_receipts"),[(1,)])
