"""Offline direct referral achievement regressions."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from payment_ledger import PaymentLedger
from referral_accrual import snapshot, record_completed_purchase, levels, tier
from referral_screen import screen, days


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

    def finish(self,pid="first",sid=40,duration=30,action="new_key"):
        self.assertEqual(self.ledger.claim(pid),"claimed")
        self.ledger.complete(pid,sid,duration_days=duration,action=action)

    def awards(self):
        return self.sql("SELECT recipient_id,qualifying_count,rate_percent,centidays FROM referral_purchase_awards ORDER BY payment_id")

    def review(self):
        return self.sql("SELECT payment_id,reason FROM referral_accrual_reviews")

    def add_friend(self,uid):
        self.sql("INSERT INTO users VALUES(?,3,0)",uid)
        self.sql("INSERT INTO subscriptions VALUES(?,?)",uid*10,uid)
        self.sql("INSERT INTO payments VALUES(?,?,1,120,'pending')",str(uid),uid)

    def test_only_direct_referrer_receives_reward(self):
        self.finish()
        self.assertEqual(self.awards(),[(3,1,10,300)])
        self.assertEqual(snapshot(self.path,2)["centidays"],0)
        self.assertEqual(snapshot(self.path,3)["centidays"],300)
        self.assertEqual(snapshot(self.path,4)["centidays"],0)

    def test_every_purchase_and_renewal_earns_once(self):
        self.finish()
        self.assertEqual(self.ledger.claim("first"),"done")
        self.finish("second",41,90,"renew")
        self.assertEqual(self.awards(),[(3,1,10,300),(3,1,10,900)])
        self.assertEqual(snapshot(self.path,3)["paid"],1)

    def test_fifth_friend_upgrades_that_purchase(self):
        self.finish()
        for uid in range(11,15):
            self.add_friend(uid)
            self.finish(str(uid),uid*10)
        s=snapshot(self.path,3)
        self.assertEqual(s["paid"],5)
        self.assertEqual(s["current"],(5,"Партнёр",20))
        self.assertEqual(s["centidays"],1800)
        self.finish("second",41,30,"renew")
        self.assertEqual(snapshot(self.path,3)["centidays"],2400)

    def test_fractional_days_preserved(self):
        self.finish(duration=365)
        self.assertEqual(snapshot(self.path,3)["centidays"],3650)
        self.assertEqual(days(3650),"36,5")
        self.assertEqual(days(1),"0,01")
        self.assertEqual(days(0),"0")

    def test_registration_and_history_do_not_earn(self):
        self.sql("INSERT INTO payments VALUES('historical',4,1,120,'paid')")
        self.sql("INSERT INTO payments VALUES('trial',4,1,0,'paid'),('deposit',4,0,120,'paid')")
        self.assertEqual(snapshot(self.path,3)["paid"],0)
        self.assertEqual(snapshot(self.path,3)["invited"],1)
        self.finish()
        self.assertEqual(self.awards(),[(3,1,10,300)])

    def test_no_referrer_no_award(self):
        self.sql("UPDATE users SET referrer_id=0 WHERE telegram_id=4")
        self.finish()
        self.assertEqual(self.awards(),[])

    def test_gate_requires_both_switches(self):
        with patch.dict(os.environ,{"VPN4US_SAFE_REFERRAL_ACCRUAL":"0"}):
            self.finish()
        self.sql("UPDATE settings SET value='false'")
        self.finish("second",41)
        self.assertEqual(snapshot(self.path,3)["centidays"],0)
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE name='referral_purchase_awards'"),[])

    def test_refund_before_completion_held(self):
        self.assertEqual(self.ledger.claim("first"),"claimed")
        self.sql("UPDATE payment_processing SET refund_state='partial_review'")
        self.ledger.complete("first",40,duration_days=30,action="new_key")
        self.assertEqual(self.review(),[("first","refund_present")])
        self.assertEqual(self.awards(),[])

    def test_refund_after_award_excludes_only_affected_purchase(self):
        self.finish()
        self.finish("second",41,90,"renew")
        self.sql("UPDATE payment_processing SET refund_state='full_review' WHERE payment_id='first'")
        self.assertEqual(snapshot(self.path,3)["centidays"],900)
        self.assertEqual(snapshot(self.path,3)["paid"],1)
        self.sql("UPDATE payment_processing SET refund_state='full_review' WHERE payment_id='second'")
        self.assertEqual(snapshot(self.path,3)["paid"],0)
        self.assertEqual(len(self.awards()),2)

    def test_parallel_distinct_purchases_each_awarded(self):
        self.assertEqual(self.ledger.claim("first"),"claimed")
        self.assertEqual(self.ledger.claim("second"),"claimed")
        def complete(pair):
            self.ledger.complete(*pair,duration_days=30,action="new_key")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(complete,[("first",40),("second",41)]))
        self.assertEqual(self.awards(),[(3,1,10,300),(3,1,10,300)])

    def test_corrupt_chain_held(self):
        self.sql("UPDATE users SET referrer_id=4 WHERE telegram_id=1")
        self.finish()
        self.assertEqual(self.review(),[("first","invalid_referral_chain")])

    def test_blocked_recipient_held(self):
        self.sql("UPDATE users SET is_blocked=1 WHERE telegram_id=3")
        self.finish()
        self.assertEqual(self.review(),[("first","recipient_unavailable")])

    def test_wrong_subscription_owner_held(self):
        self.finish(sid=10)
        self.assertEqual(self.review(),[("first","subscription_ownership_unconfirmed")])

    def test_invalid_duration_held(self):
        self.finish(duration=0)
        self.assertEqual(self.review(),[("first","invalid_purchase_duration_or_action")])

    def test_invalid_policy_held(self):
        self.sql("INSERT INTO settings VALUES('referral_levels','[[2,\"Bad\",10]]')")
        self.finish()
        self.assertEqual(self.review(),[("first","invalid_level_policy")])

    def test_policy_change_does_not_reprice_awards(self):
        self.finish()
        self.sql("INSERT INTO settings VALUES('referral_levels','[[1,\"New\",25]]')")
        self.finish("second",41,30,"renew")
        self.assertEqual(self.awards(),[(3,1,10,300),(3,1,25,750)])

    def test_default_tier_boundaries(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows=levels(db)
        self.assertEqual([tier(rows,n)[2] for n in (0,1,4,5,9,10,24,25,49,50)],[0,10,10,20,20,30,30,40,40,50])

    def test_unverified_payment_is_ineligible(self):
        with self.ledger._tx() as db:
            self.assertEqual(record_completed_purchase(db,"first",30,"new_key"),"ineligible")

    def test_main_screen_progress_and_navigation(self):
        self.finish()
        text,kb=screen(self.path,3,"example_bot","main")
        self.assertIn("Новичок",text)
        self.assertIn("1/5",text)
        self.assertIn("Ещё 4",text)
        self.assertIn("ref_3",text)
        self.assertEqual([r[0].callback_data for r in kb.inline_keyboard],
                         ["referral_levels","referral_spend","referral_history","back_to_main"])

    def test_levels_history_spend_are_read_only(self):
        self.finish()
        for page,expected in (("levels","Легенда"),("history","+3 дн."),("spend","не активировано")):
            text,kb=screen(self.path,3,"example_bot",page)
            self.assertIn(expected,text)
            self.assertEqual(kb.inline_keyboard[0][0].callback_data,"referral_main")
        self.assertEqual(self.awards(),[(3,1,10,300)])
