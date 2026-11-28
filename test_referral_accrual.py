"""Offline first-paid referral journal regressions."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from payment_ledger import PaymentLedger
from referral_accrual import confirmed_days, record_completed_purchase


class ReferralAccrualTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=str(Path(tmp.name)/"test.db")
        self.ledger=PaymentLedger(self.path)
        env=patch.dict(os.environ,{"VPN4US_SAFE_REFERRAL_ACCRUAL":"1"})
        env.start()
        self.addCleanup(env.stop)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO settings VALUES('referral_accrual_enabled','true');
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,referrer_id INTEGER,is_blocked INTEGER);
                INSERT INTO users VALUES(1,0,0),(2,1,0),(3,2,0),(4,3,0),(5,4,0);
                CREATE TABLE payments(payment_id TEXT PRIMARY KEY,telegram_id INTEGER,tariff_id INTEGER,
                    amount_rub REAL,status TEXT);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,is_legacy INTEGER DEFAULT 0,
                    payment_state TEXT,vpn_state TEXT,tax_state TEXT,refund_state TEXT DEFAULT 'none',
                    subscription_id INTEGER,last_error TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,telegram_id INTEGER);
                INSERT INTO subscriptions VALUES(40,4),(41,4),(50,5),(10,1);
                INSERT INTO payments VALUES('first',4,1,120,'pending'),('second',4,1,120,'pending');
            """)

    def sql(self,q,*params):
        with closing(sqlite3.connect(self.path)) as db,db:
            return db.execute(q,params).fetchall()

    def finish(self,pid="first",sid=40):
        self.assertEqual(self.ledger.claim(pid),"claimed")
        self.ledger.complete(pid,sid)

    def state(self):
        return self.sql("SELECT payer_id,state,reason FROM referral_first_purchases")

    def awards(self):
        return self.sql("SELECT recipient_id,level,days FROM referral_day_awards ORDER BY level")

    def test_first_purchase_rewards_referrers_not_payer(self):
        self.finish()
        self.assertEqual(self.awards(),[(3,1,30),(2,2,15),(1,3,5)])
        self.assertEqual([confirmed_days(self.path,i) for i in (1,2,3,4)], [5,15,30,0])
        self.assertEqual(self.state(),[(4,"awarded","")])

    def test_duplicate_and_second_purchase_never_award_again(self):
        self.finish()
        self.assertEqual(self.ledger.claim("first"),"done")
        self.finish("second",41)
        self.assertEqual(len(self.awards()),3)
        self.assertEqual(len(self.state()),1)

    def test_historical_paid_purchase_excludes_new_award(self):
        self.sql("INSERT INTO payments VALUES('historical',4,1,120,'paid')")
        self.finish()
        self.assertEqual(self.awards(),[])
        self.assertEqual(self.state(),[])

    def test_trial_and_deposit_are_not_prior_paid_subscription(self):
        self.sql("INSERT INTO payments VALUES('trial',4,1,0,'paid'),('deposit',4,0,120,'paid')")
        self.finish()
        self.assertEqual(len(self.awards()),3)

    def test_first_purchase_without_referrer_is_recorded(self):
        self.sql("UPDATE users SET referrer_id=0 WHERE telegram_id=4")
        self.finish()
        self.assertEqual(self.state(),[(4,"no_referrer","")])
        self.assertEqual(self.awards(),[])

    def test_gate_requires_both_switches(self):
        with patch.dict(os.environ,{"VPN4US_SAFE_REFERRAL_ACCRUAL":"0"}):
            self.finish()
        self.assertEqual(confirmed_days(self.path,3),0)
        self.sql("UPDATE settings SET value='false'")
        self.finish("second",41)
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE name='referral_day_awards'"),[])

    def test_disabled_history_never_replayed_on_activation(self):
        with patch.dict(os.environ,{"VPN4US_SAFE_REFERRAL_ACCRUAL":"0"}):
            self.finish()
        self.finish("second",41)
        self.assertEqual(self.awards(),[])

    def test_refund_before_completion_requires_review(self):
        self.assertEqual(self.ledger.claim("first"),"claimed")
        self.sql("UPDATE payment_processing SET refund_state='partial_review'")
        self.ledger.complete("first",40)
        self.assertEqual(self.state(),[(4,"needs_review","refund_present")])
        self.assertEqual(self.awards(),[])

    def test_refund_after_award_holds_days_without_erasing_history(self):
        self.finish()
        self.sql("UPDATE payment_processing SET refund_state='full_review'")
        self.assertEqual(confirmed_days(self.path,3),0)
        self.assertEqual(len(self.awards()),3)
        self.finish("second",41)
        self.assertEqual(len(self.awards()),3)

    def test_parallel_verified_orders_are_not_double_awarded(self):
        self.assertEqual(self.ledger.claim("first"),"claimed")
        self.assertEqual(self.ledger.claim("second"),"claimed")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda x:self.ledger.complete(*x),[("first",40),("second",41)]))
        self.assertEqual(self.state(),[(4,"needs_review","another_verified_purchase_unresolved")])
        self.assertEqual(self.awards(),[])

    def test_corrupt_chain_or_blocked_recipient_held(self):
        self.sql("UPDATE users SET referrer_id=4 WHERE telegram_id=1")
        self.finish()
        self.assertEqual(self.state(),[(4,"needs_review","invalid_referral_chain")])
        self.assertEqual(self.awards(),[])

    def test_blocked_recipient_held(self):
        self.sql("UPDATE users SET is_blocked=1 WHERE telegram_id=2")
        self.finish()
        self.assertEqual(self.state(),[(4,"needs_review","recipient_unavailable")])
        self.assertEqual(self.awards(),[])

    def test_wrong_subscription_owner_held(self):
        self.finish(sid=10)
        self.assertEqual(self.state(),[(4,"needs_review","subscription_ownership_unconfirmed")])
        self.assertEqual(self.awards(),[])

    def test_prior_balance_renewal_held(self):
        self.sql("CREATE TABLE auto_renewal_operations(telegram_id INTEGER)")
        self.sql("INSERT INTO auto_renewal_operations VALUES(4)")
        self.finish()
        self.assertEqual(self.state(),[(4,"needs_review","prior_balance_renewal_requires_review")])
        self.assertEqual(self.awards(),[])

    def test_unverified_payment_is_not_awarded(self):
        with self.ledger._tx() as db:
            self.assertEqual(record_completed_purchase(db,"first"),"ineligible")
        self.assertEqual(confirmed_days(self.path,3),0)
