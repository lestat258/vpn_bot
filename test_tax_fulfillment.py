import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tax_fulfillment import process_tax_receipt
from payment_ledger import PaymentLedger


class TaxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.cleanup)
        self.path = str(Path(self.tmp.name) / "test.db")
        with sqlite3.connect(self.path) as db:
            db.executescript("""
            CREATE TABLE payments(payment_id TEXT PRIMARY KEY, telegram_id INTEGER,
                amount_rub REAL, status TEXT);
            CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,
                is_legacy INTEGER DEFAULT 0, payment_state TEXT,
                vpn_state TEXT, tax_state TEXT, refund_state TEXT DEFAULT 'none',
                subscription_id INTEGER, receipt_uuid TEXT UNIQUE, receipt_url TEXT,
                last_error TEXT, updated_at TIMESTAMP);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE tax_receipts(payment_id TEXT, receipt_uuid TEXT,
                receipt_url TEXT, amount REAL, description TEXT,user_id INTEGER);
            INSERT INTO settings VALUES ('tax_enabled','true'),
                ('tax_description_template','Настройка доступа к сети');
            INSERT INTO payments VALUES ('new',100,1,'pending'),
                ('old',100,1,'paid');
            INSERT INTO payment_processing(payment_id,payment_state,vpn_state,tax_state)
                VALUES ('old','verified','done','not_started');
            """)
        self.calls = 0
        self.messages = 0

    async def cleanup(self):
        self.tmp.cleanup()

    async def issuer(self, path, amount, description):
        self.calls += 1
        self.assertEqual(amount, 1)
        self.assertEqual(description, "Настройка доступа к сети")
        await asyncio.sleep(.01)
        return "test-receipt", "https://example.invalid/receipt"

    async def notifier(self, user, url):
        self.messages += 1

    def queue(self):
        ledger = PaymentLedger(self.path)
        self.assertEqual(ledger.claim("new"), "claimed")
        ledger.complete("new", 1)

    def state(self, pid="new"):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT tax_state FROM payment_processing WHERE payment_id=?",
                              (pid,)).fetchone()[0]

    async def run_tax(self, pid="new", issuer=None, notifier=None):
        return await process_tax_receipt(pid, db_path=self.path,
                                         issuer=issuer or self.issuer,
                                         notifier=notifier or self.notifier)

    async def test_parallel_and_repeat(self):
        self.queue()
        self.assertEqual(self.state(), "queued")
        results = await asyncio.gather(*(self.run_tax() for _ in range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.messages, 1)
        self.assertEqual(self.state(), "done")
        self.assertFalse(await self.run_tax())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tax_receipts").fetchone()[0], 1)

    async def test_historical_not_reissued(self):
        self.assertFalse(await self.run_tax("old"))
        self.assertEqual(self.state("old"), "not_started")
        self.assertEqual(self.calls, 0)

    async def test_uncertain_result_never_retried(self):
        self.queue()
        async def fail(*args):
            self.calls += 1
            raise TimeoutError()
        self.assertFalse(await self.run_tax(issuer=fail))
        self.assertEqual(self.state(), "needs_review")
        self.assertFalse(await self.run_tax())
        self.assertEqual(self.calls, 1)

    async def test_disabled(self):
        self.queue()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE settings SET value='false' WHERE key='tax_enabled'")
        self.assertFalse(await self.run_tax())
        self.assertEqual(self.state(), "skipped")
        self.assertEqual(self.calls, 0)

    async def test_notification_failure_no_repeat(self):
        self.queue()
        async def fail(*args):
            raise RuntimeError()
        self.assertTrue(await self.run_tax(notifier=fail))
        self.assertEqual(self.state(), "done")
        self.assertFalse(await self.run_tax())
        self.assertEqual(self.calls, 1)

    async def test_existing_receipt_held(self):
        self.queue()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO tax_receipts(payment_id,receipt_uuid) VALUES ('new','existing')")
        self.assertFalse(await self.run_tax())
        self.assertEqual(self.state(), "needs_review")
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
