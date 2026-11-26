import sqlite3
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from payment_ledger import PaymentLedger


class PaymentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'test.db')
        with sqlite3.connect(self.db) as con:
            con.executescript('''
                CREATE TABLE payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    tariff_id INTEGER NOT NULL,
                    amount_rub REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE payment_processing (
                    payment_id TEXT PRIMARY KEY,
                    is_legacy INTEGER NOT NULL DEFAULT 0,
                    payment_state TEXT NOT NULL DEFAULT 'pending',
                    vpn_state TEXT NOT NULL DEFAULT 'not_started',
                    tax_state TEXT NOT NULL DEFAULT 'not_started',
                    refund_state TEXT NOT NULL DEFAULT 'none',
                    subscription_id INTEGER,
                    receipt_uuid TEXT UNIQUE,
                    receipt_url TEXT,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(payment_id) REFERENCES payments(payment_id)
                );
                INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status)
                    VALUES ('p1',100,1,120,'pending'), ('free1',100,1,0,'paid');
            ''')
        self.ledger = PaymentLedger(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_and_complete(self):
        self.assertEqual(self.ledger.claim('p1'), 'claimed')
        self.assertEqual(self.ledger.claim('p1'), 'held')
        self.ledger.complete('p1', 23)
        self.assertEqual(self.ledger.claim('p1'), 'done')
        with sqlite3.connect(self.db) as con:
            row = con.execute('SELECT status FROM payments WHERE payment_id=?', ('p1',)).fetchone()
            self.assertEqual(row[0], 'paid')

    def test_concurrent_claim_only_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.ledger.claim, ['p1', 'p1']))
        self.assertEqual(sorted(results), ['claimed', 'held'])

    def test_uncertain_not_retried(self):
        self.assertEqual(self.ledger.claim('p1'), 'claimed')
        self.ledger.mark_review('p1', 'Timeout')
        self.assertEqual(self.ledger.claim('p1'), 'held')
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT status FROM payments WHERE payment_id='p1'").fetchone()[0], 'pending')

    def test_never_claim_free_or_unknown(self):
        self.assertEqual(self.ledger.claim('free1'), 'held')
        self.assertEqual(self.ledger.claim('notthere'), 'missing')

    def test_complete_before_claim_rejected(self):
        with self.assertRaises(RuntimeError):
            self.ledger.complete('p1', 1)

    def test_cannot_complete_without_subscription(self):
        self.assertEqual(self.ledger.claim('p1'), 'claimed')
        with self.assertRaises(ValueError):
            self.ledger.complete('p1', 0)


if __name__ == '__main__':
    unittest.main()
