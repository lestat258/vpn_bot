import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from balance_ledger import (register_deposit,credit_verified,deposit_metadata,record_deposit_change,
    admin_adjust,validate_deposit_amount,kopecks)
from refund_fulfillment import scope


class BalanceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=str(Path(tmp.name)/"test.db")
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
            CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,balance REAL,is_blocked INTEGER);
            CREATE TABLE payments(id INTEGER PRIMARY KEY,payment_id TEXT UNIQUE,telegram_id INTEGER,
                tariff_id INTEGER,amount_rub REAL,status TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,is_legacy INTEGER DEFAULT 0,
                payment_state TEXT,vpn_state TEXT,tax_state TEXT,refund_state TEXT DEFAULT 'none',
                subscription_id INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO users VALUES(42,0,0),(43,0,0);
            INSERT INTO settings VALUES('tax_enabled','false');
            INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status) VALUES('old',42,0,10,'pending');
            """)
        self.meta=deposit_metadata(42,1050)
        register_deposit(self.path,"new",42,1050)

    def read(self,sql):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql).fetchall()

    def credit(self):
        return credit_verified(self.path,"new",self.meta)

    def test_registration_is_idempotent_and_scope_explicit(self):
        register_deposit(self.path,"new",42,1050)
        self.assertEqual(scope(self.path,"new"),"new")
        self.assertEqual(scope(self.path,"old"),"historical")
        self.assertEqual(self.read("SELECT count(*) FROM payments"),[(2,)])
        with self.assertRaises(ValueError):
            register_deposit(self.path,"new",43,1050)

    def test_credit_and_duplicate(self):
        self.assertEqual(self.credit(),("credited",42,1050))
        self.assertEqual(self.credit(),("done",42,1050))
        self.assertEqual(self.read("SELECT balance FROM users WHERE telegram_id=42"),[(10.5,)])
        self.assertEqual(self.read("SELECT delta_kopecks FROM balance_operations"),[(1050,)])
        self.assertEqual(self.read("SELECT status FROM payments WHERE payment_id='new'"),[("paid",)])
        self.assertEqual(self.read("SELECT vpn_state,tax_state,subscription_id FROM payment_processing"),[("done","skipped",None)])

    def test_concurrent_credit(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes=list(pool.map(lambda _:self.credit()[0],range(12)))
        self.assertEqual(outcomes.count("credited"),1)
        self.assertEqual(outcomes.count("done"),11)

    def test_wrong_metadata_and_historical_no_credit(self):
        for meta in ({**self.meta,"telegram_id":"43"},{**self.meta,"amount":"11.00"},{**self.meta,"action":"renew"}):
            with self.assertRaises(ValueError):
                credit_verified(self.path,"new",meta)
        with self.assertRaises(ValueError):
            credit_verified(self.path,"old",self.meta)
        self.assertEqual(self.read("SELECT balance FROM users WHERE telegram_id=42"),[(0.0,)])

    def test_refund_before_credit(self):
        record_deposit_change(self.path,"new",self.meta,refund="10.50")
        self.assertEqual(self.credit()[0],"held")
        self.assertEqual(self.read("SELECT count(*) FROM balance_operations"),[(0,)])

    def test_refund_after_credit_holds_spending(self):
        self.credit()
        record_deposit_change(self.path,"new",self.meta,refund="2.00")
        record_deposit_change(self.path,"new",self.meta,refund="10.50")
        self.assertEqual(self.credit()[0],"held")
        self.assertEqual(self.read("SELECT refund_state FROM payment_processing"),[("full_review",)])
        with self.assertRaises(ValueError):
            admin_adjust(self.path,"w",42,99,"1",withdraw=True)
        self.assertEqual(self.read("SELECT balance FROM users WHERE telegram_id=42"),[(10.5,)])

    def test_cancel(self):
        self.assertEqual(record_deposit_change(self.path,"new",self.meta,canceled=True),"done")
        self.assertEqual(record_deposit_change(self.path,"new",self.meta,canceled=True),"done")
        with self.assertRaises(ValueError):
            self.credit()

    def test_admin_adjustment_idempotence_and_atomic_withdraw(self):
        self.credit()
        self.assertEqual(admin_adjust(self.path,"a",42,99,"0.50"),(1100,True))
        self.assertEqual(admin_adjust(self.path,"a",42,99,"0.50"),(1100,False))
        def withdraw(i):
            try:
                return admin_adjust(self.path,"w"+str(i),42,99,"10",withdraw=True)[1]
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(withdraw,range(5))),1)
        self.assertEqual(self.read("SELECT balance FROM users WHERE telegram_id=42"),[(1.0,)])

    def test_invalid_amounts(self):
        for value in ("NaN","Infinity","-1","0","9.99","10.001","100000.01"):
            with self.assertRaises(ValueError):
                validate_deposit_amount(value)
        self.assertEqual(validate_deposit_amount("10.25"),1025)
        self.assertEqual(kopecks("0.01"),1)

    def test_tax_requires_review_not_automatic_receipt(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE settings SET value='true'")
        self.credit()
        self.assertEqual(self.read("SELECT tax_state FROM payment_processing"),[("needs_review",)])
