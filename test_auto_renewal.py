"""Isolated SQLite and fake 3x-ui; no real accounts or network."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import auto_renewal as ar
from subscription_renewal import DAY_MS, renew_subscription, renewable_subscriptions
from test_subscription_renewal import FakePanel, NOW
from promo_checkout import create_checkout
from promo_reservations import ReservationBusy


class AutoRenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/"offline.db")
        env = patch.dict(os.environ, {"VPN4US_SAFE_AUTORENEW":"1"})
        env.start()
        self.addCleanup(env.stop)
        self.end = datetime.fromtimestamp((NOW-1000)/1000,timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO settings VALUES('auto_renew_enabled','true');
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,is_blocked INTEGER,balance REAL);
                INSERT INTO users VALUES(42,0,200.00),(43,0,900.00);
                CREATE TABLE servers(id INTEGER PRIMARY KEY,url TEXT,api_token TEXT,is_active INTEGER);
                INSERT INTO servers VALUES(2,'https://correct.invalid','synthetic',1),(3,'https://other.invalid','other',1);
                CREATE TABLE tariffs(id INTEGER PRIMARY KEY,name TEXT,price_rub REAL,duration_days INTEGER,is_active INTEGER);
                INSERT INTO tariffs VALUES(3,'Test',150.25,3,1);
                CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,telegram_id INTEGER,server_id INTEGER,
                    tariff_id INTEGER,end_date TEXT,xui_email TEXT,xui_client_uid TEXT,xui_subid TEXT,
                    is_active INTEGER,managed_by_vpn4us INTEGER,auto_renew INTEGER);
                CREATE TABLE subscription_inbound_memberships(subscription_id INTEGER,server_id INTEGER,inbound_id INTEGER);
                INSERT INTO subscription_inbound_memberships VALUES(1,2,5),(1,2,8);
                CREATE TABLE payments(payment_id TEXT PRIMARY KEY,telegram_id INTEGER,tariff_id INTEGER,status TEXT);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,vpn_state TEXT,refund_state TEXT);
            """)
            db.execute("INSERT INTO subscriptions VALUES(1,42,2,3,?,'test@example.invalid','uid','sid',1,1,1)",(self.end,))
            from balance_ledger import ensure_schema
            ensure_schema(db)
        self.panel = FakePanel(NOW-1000,enabled=False)
        self.factory = Mock(return_value=self.panel)
        self.candidate = ar.candidates(self.path,NOW)[0]

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as db,db:
            return db.execute(sql,params).fetchall()

    def run_one(self, candidate=None):
        return ar.process_one(self.path,candidate or self.candidate,self.factory,clock=lambda:NOW)

    def balance(self,owner=42):
        return self.sql("SELECT balance FROM users WHERE telegram_id=?",(owner,))[0][0]

    def state(self):
        if not self.sql("SELECT 1 FROM sqlite_master WHERE name='auto_renewal_operations'"):
            return []
        return self.sql("SELECT state FROM auto_renewal_operations")

    def assert_untouched(self):
        self.assertEqual(self.balance(),200)
        self.assertEqual(self.balance(43),900)
        self.assertEqual(self.sql("SELECT end_date FROM subscriptions WHERE id=1"),[(self.end,)])
        self.assertEqual(self.sql("SELECT count(*) FROM balance_operations"),[(0,)])
        self.assertEqual(self.state(),[])
        self.assertEqual(self.panel.writes,[])

    def test_disabled_environment_no_network_or_debit(self):
        with patch.dict(os.environ,{"VPN4US_SAFE_AUTORENEW":"0"}):
            self.assertEqual(ar.candidates(self.path,NOW),[])
            with self.assertRaises(ValueError): self.run_one()
        self.factory.assert_not_called()
        self.assert_untouched()

    def test_disabled_admin_setting_no_network_or_debit(self):
        self.sql("UPDATE settings SET value='false'")
        self.assertEqual(ar.candidates(self.path,NOW),[])
        with self.assertRaises(ValueError): self.run_one()
        self.factory.assert_not_called()
        self.assert_untouched()

    def test_due_window_no_future_or_old_replay(self):
        for expiry in (NOW+1000,NOW-DAY_MS,NOW-5*DAY_MS):
            self.sql("UPDATE subscriptions SET end_date=?",(datetime.fromtimestamp(expiry/1000,timezone.utc).isoformat(),))
            self.assertEqual(ar.candidates(self.path,NOW),[])

    def test_success_exact_owner_tariff_cents_server_and_duration(self):
        end=self.run_one()
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(self.balance(43),900)
        self.assertEqual(self.state(),[("done",)])
        self.assertEqual(self.sql("SELECT kind,delta_kopecks,before_kopecks,after_kopecks FROM balance_operations"),
                         [("auto_renew_reserve",-15025,20000,4975)])
        self.assertEqual(self.sql("SELECT end_date FROM subscriptions"),[(end.isoformat(),)])
        self.assertEqual(self.panel.writes[0][1]["addDays"],4)
        self.assertEqual(self.panel.writes[0][2],1)
        self.factory.assert_called_once_with(url="https://correct.invalid",api_token="synthetic")
        self.assertEqual(ar.review_count(self.path),0)

    def test_stale_candidate_no_second_debit_or_post(self):
        self.run_one()
        with self.assertRaises(ValueError): self.run_one()
        self.assertEqual(len(self.panel.writes),1)
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(self.sql("SELECT count(*) FROM balance_operations"),[(1,)])

    def test_insufficient_balance_no_post(self):
        self.sql("UPDATE users SET balance=150.24 WHERE telegram_id=42")
        with self.assertRaises(ValueError): self.run_one()
        self.assertEqual(self.balance(),150.24)
        self.assertEqual(self.panel.writes,[])
        self.assertEqual(self.state(),[])

    def test_refund_hold_no_debit(self):
        self.sql("INSERT INTO payments VALUES('held',42,0,'paid')")
        self.sql("INSERT INTO payment_processing VALUES('held','done','partial_review')")
        self.sql("INSERT INTO balance_deposit_orders(payment_id,telegram_id,amount_kopecks,refunded_kopecks) VALUES('held',42,10000,100)")
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_blocked_owner_no_debit(self):
        self.sql("UPDATE users SET is_blocked=1 WHERE telegram_id=42")
        self.assertEqual(ar.candidates(self.path,NOW),[])
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_opt_out_after_selection_no_debit(self):
        self.sql("UPDATE subscriptions SET auto_renew=0")
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_tariff_price_or_duration_changed_no_debit(self):
        self.sql("UPDATE tariffs SET price_rub=151,duration_days=4")
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_wrong_owner_candidate_no_debit(self):
        with self.assertRaises(ValueError): self.run_one(replace(self.candidate,owner=43))
        self.assert_untouched()

    def test_wrong_client_identity_no_debit(self):
        self.panel.client["subId"]="other"
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_wrong_membership_no_debit(self):
        self.panel.ids=[5,9]
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_panel_get_timeout_no_debit(self):
        with patch.object(self.panel,"_request",side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): self.run_one()
        self.assert_untouched()

    def test_pending_invoice_blocks_auto(self):
        self.sql("INSERT INTO payments VALUES('invoice',42,3,'pending')")
        self.assertEqual(ar.candidates(self.path,NOW),[])
        with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_deposit_invoice_does_not_block_auto(self):
        self.sql("INSERT INTO payments VALUES('deposit',42,0,'pending')")
        self.assertEqual(len(ar.candidates(self.path,NOW)),1)
        self.run_one()
        self.assertEqual(self.state(),[("done",)])

    def test_uncertain_manual_checkout_blocks_auto(self):
        ar.begin_manual_attempt(self.path,"manual",42,1)
        self.assertEqual(ar.candidates(self.path,NOW),[])
        with self.assertRaises(ValueError): self.run_one()
        with self.assertRaises(ValueError):
            ar.begin_manual_attempt(self.path,"another",42,1)
        self.assert_untouched()

    def test_registered_manual_checkout_then_pending_invoice_blocks_auto(self):
        ar.begin_manual_attempt(self.path,"manual",42,1)
        self.sql("INSERT INTO payments VALUES('manual-invoice',42,3,'pending')")
        ar.finish_manual_attempt(self.path,"manual")
        self.assertEqual(ar.candidates(self.path,NOW),[])
        self.sql("UPDATE payments SET status='canceled'")
        self.assertEqual(len(ar.candidates(self.path,NOW)),1)

    def test_ambiguous_post_is_reserved_no_retry_or_manual_extension(self):
        self.panel.fail=True
        with self.assertRaises(ValueError): self.run_one()
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(self.state(),[("needs_review",)])
        self.assertEqual(ar.review_count(self.path),1)
        self.assertEqual(ar.candidates(self.path,NOW),[])
        self.assertEqual(renewable_subscriptions(self.path,42,1),[])
        with self.assertRaises(ValueError): self.run_one()
        with self.assertRaises(ValueError):
            renew_subscription(self.path,1,3,3,self.factory,clock=lambda:NOW)
        provider=Mock()
        with self.assertRaises(ReservationBusy):
            create_checkout(self.path,provider,"manual",42,3,"Test",150.25,"bot",action="renew",subscription_id=1)
        provider.create.assert_not_called()
        self.assertEqual(len(self.panel.writes),1)
        self.assertEqual(self.sql("SELECT end_date FROM subscriptions"),[(self.end,)])
        self.assertEqual(self.sql("SELECT count(*) FROM balance_operations"),[(1,)])

    def test_process_crash_after_reserve_blocks_replay(self):
        request=self.panel._request
        def crash(method,*args,**kwargs):
            if method=="POST": raise SystemExit("simulated process death")
            return request(method,*args,**kwargs)
        with patch.object(self.panel,"_request",side_effect=crash):
            with self.assertRaises(SystemExit): self.run_one()
        self.assertEqual(self.state(),[("reserved",)])
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(ar.review_count(self.path),1)
        with self.assertRaises(ValueError): self.run_one()
        self.assertEqual(self.panel.writes,[])

    def test_concurrent_same_subscription_only_one_mutation(self):
        def attempt():
            try:
                self.run_one()
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(len(self.panel.writes),1)
        self.assertEqual(self.sql("SELECT count(*) FROM balance_operations"),[(1,)])

    def test_local_change_after_post_requires_review(self):
        request=self.panel._request
        def changed(method,*args,**kwargs):
            result=request(method,*args,**kwargs)
            if method=="POST": self.sql("UPDATE users SET is_blocked=1 WHERE telegram_id=42")
            return result
        with patch.object(self.panel,"_request",side_effect=changed):
            with self.assertRaises(ValueError): self.run_one()
        self.assertEqual(self.state(),[("needs_review",)])
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(self.sql("SELECT end_date FROM subscriptions"),[(self.end,)])
        self.assertEqual(len(self.panel.writes),1)

    def test_global_disable_during_panel_read_prevents_charge(self):
        request=self.panel._request
        def disable(method,*args,**kwargs):
            result=request(method,*args,**kwargs)
            if method=="GET": self.sql("UPDATE settings SET value='false'")
            return result
        with patch.object(self.panel,"_request",side_effect=disable):
            with self.assertRaises(ValueError): self.run_one()
        self.assert_untouched()

    def test_notification_failure_never_repeats_completed_renewal(self):
        from auto_renew import AutoRenewManager
        bot=Mock()
        bot.send_message=AsyncMock(side_effect=TimeoutError)
        manager=AutoRenewManager(self.path,bot=bot)
        with patch("auto_renew.time.time_ns",return_value=NOW*1000000), \
             patch("auto_renew.XUIClient",self.factory), \
             patch("auto_renew.get_setting",return_value="42"):
            asyncio.run(manager.process_renewals())
            asyncio.run(manager.process_renewals())
        self.assertEqual(self.balance(),49.75)
        self.assertEqual(len(self.panel.writes),1)
        self.assertEqual(self.state(),[("done",)])
