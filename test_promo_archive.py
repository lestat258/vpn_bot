"""Offline archive and full-price consent tests. No live provider calls."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from promo_checkout import create_checkout
from promo_reservations import (
    archive_info,set_archived,prepare_full_price,full_price_details,
    open_reservation,settle_verified_success,ReservationBusy,ReservationConflict
)
from refund_fulfillment import record_canceled


class PromoArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/"db.sqlite"
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE user_promocodes(id INTEGER PRIMARY KEY,user_id INTEGER,promocode_id INTEGER,
                    discount_percent INTEGER,discount_amount REAL,expires_at TEXT,is_used INTEGER,used_at TEXT);
                CREATE TABLE promocode_uses(promocode_id INTEGER,user_id INTEGER,source TEXT);
                CREATE TABLE payments(id INTEGER PRIMARY KEY AUTOINCREMENT,payment_id TEXT UNIQUE,
                    telegram_id INTEGER,tariff_id INTEGER,amount_rub REAL,status TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,payment_state TEXT,vpn_state TEXT,tax_state TEXT);
                INSERT INTO settings VALUES('promocode_user_42','50|0');
            """)
        self.provider=SimpleNamespace(create=Mock(side_effect=self.create))
        self.discounted=None
        self.checkout("discount")
        self.discounted=self.last_payment

    def create(self,payload,key):
        self.last_payment=SimpleNamespace(id=key,status="pending",paid=False,
            metadata=payload["metadata"],amount=SimpleNamespace(**payload["amount"]),
            confirmation=SimpleNamespace(confirmation_url="https://example.invalid/"+key))
        return self.last_payment

    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql,args).fetchall()

    def execute(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute(sql,args)

    def checkout(self,key,**kwargs):
        return create_checkout(self.path,self.provider,key,42,1,"Test",150,"bot",**kwargs)

    def archive(self):
        self.assertTrue(set_archived(self.path,42,1,True))

    def prepare(self,**kwargs):
        return prepare_full_price(self.path,42,1,150,kwargs.get("action","new_key"),kwargs.get("subscription_id"))

    def verified(self):
        return {"id":"discount","metadata":self.discounted.metadata,"amount_rub":"75.00"}

    def test_archive_preserves_payment_reservation_and_grant(self):
        self.archive()
        self.assertTrue(archive_info(self.path,42)["archived"])
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
        self.assertEqual(self.query("SELECT payment_id,amount_rub,status FROM payments"),[("discount",75.0,"pending")])
        self.assertEqual(self.query("SELECT value FROM settings"),[("50|0",)])

    def test_archive_owner_and_order_scope(self):
        self.assertFalse(set_archived(self.path,43,1,True))
        self.assertFalse(set_archived(self.path,42,2,True))
        self.assertFalse(archive_info(self.path,42)["archived"])

    def test_restore_preserves_original_payment(self):
        self.archive()
        self.assertTrue(set_archived(self.path,42,1,False))
        self.assertFalse(archive_info(self.path,42)["archived"])
        self.provider.create.assert_called_once()

    def test_archive_alone_never_bypasses_discount_reservation(self):
        self.archive()
        with self.assertRaises(ReservationBusy):
            self.checkout("another")
        self.provider.create.assert_called_once()

    def test_full_price_requires_archive(self):
        self.assertIsNone(self.prepare())
        with self.assertRaises(ReservationConflict):
            self.checkout("another",full_price_token="fake")
        self.provider.create.assert_called_once()

    def test_confirmed_full_price_creates_distinct_undiscounted_order(self):
        self.archive()
        token=self.prepare()
        self.assertEqual(self.checkout("full",full_price_token=token)[:2],(15000,0))
        self.assertEqual(self.query("SELECT payment_id,amount_rub,status FROM payments ORDER BY id"),
            [("discount",75.0,"pending"),("full",150.0,"pending")])
        self.assertNotIn("promo_reservation_id",self.last_payment.metadata)
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
        self.assertEqual(self.query("SELECT value FROM settings"),[("50|0",)])

    def test_confirmation_cannot_be_reused(self):
        self.archive()
        token=self.prepare()
        self.checkout("full",full_price_token=token)
        with self.assertRaises(ReservationConflict):
            self.checkout("again",full_price_token=token)
        self.assertEqual(self.provider.create.call_count,2)

    def test_parallel_confirmation_only_one_provider_create(self):
        self.archive()
        token=self.prepare()
        def attempt(i):
            try:
                self.checkout("full"+str(i),full_price_token=token)
                return True
            except ReservationConflict:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt,range(12))),1)
        self.assertEqual(self.provider.create.call_count,2)

    def test_changed_target_or_price_rejected(self):
        self.archive()
        token=self.prepare(action="renew",subscription_id=2)
        with self.assertRaises(ReservationConflict):
            self.checkout("wrong",full_price_token=token,action="renew",subscription_id=3)
        with self.assertRaises(ReservationConflict):
            create_checkout(self.path,self.provider,"wrong-price",42,1,"Test",151,"bot",
                action="renew",subscription_id=2,full_price_token=token)
        self.assertIsNone(full_price_details(self.path,token,43))
        self.provider.create.assert_called_once()

    def test_restore_invalidates_confirmation(self):
        self.archive()
        token=self.prepare()
        set_archived(self.path,42,1,False)
        set_archived(self.path,42,1,True)
        with self.assertRaises(ReservationConflict):
            self.checkout("full",full_price_token=token)

    def test_new_confirmation_replaces_old(self):
        self.archive()
        first=self.prepare()
        second=self.prepare()
        self.assertIsNone(full_price_details(self.path,first,42))
        self.assertIsNotNone(full_price_details(self.path,second,42))

    def test_expired_confirmation_rejected(self):
        self.archive()
        token=self.prepare()
        self.execute("UPDATE promo_full_price_intents SET created_at='2000-01-01'")
        with self.assertRaises(ReservationConflict):
            self.checkout("full",full_price_token=token)

    def test_late_success_consumes_archived_discount_exactly_once(self):
        self.archive()
        token=self.prepare()
        self.assertEqual(settle_verified_success(self.path,"discount",self.verified()),"consumed")
        self.assertEqual(settle_verified_success(self.path,"discount",self.verified()),"done")
        self.assertEqual(self.query("SELECT value FROM settings"),[("",)])
        self.assertIsNone(archive_info(self.path,42))
        with self.assertRaises(ReservationConflict):
            self.checkout("full",full_price_token=token)

    def test_verified_cancel_releases_archived_discount(self):
        self.archive()
        token=self.prepare()
        self.assertEqual(record_canceled(self.path,"discount",metadata=self.discounted.metadata,amount="75.00"),"done")
        self.assertIsNone(archive_info(self.path,42))
        self.assertEqual(self.query("SELECT value FROM settings"),[("50|0",)])
        with self.assertRaises(ReservationConflict):
            self.checkout("full",full_price_token=token)
        self.assertEqual(self.checkout("new")[:2],(7500,7500))

    def test_uncertain_full_price_attempt_not_replayed(self):
        self.archive()
        token=self.prepare()
        self.provider.create.side_effect=TimeoutError("unknown")
        with self.assertRaises(TimeoutError):
            self.checkout("full",full_price_token=token)
        with self.assertRaises(ReservationConflict):
            self.checkout("again",full_price_token=token)
        self.assertEqual(self.provider.create.call_count,2)
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
