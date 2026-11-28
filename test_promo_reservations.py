"""Isolated ledger tests; no provider, Telegram or production DB calls."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from promo_reservations import (reserve,attach,settle,transaction,metadata_for,payable,
    ReservationBusy,ReservationConflict,ZeroPayable,registered)


class PromoReservationTests(unittest.TestCase):
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
                CREATE TABLE payments(payment_id TEXT PRIMARY KEY,telegram_id INTEGER,tariff_id INTEGER,
                    amount_rub REAL,status TEXT);
                INSERT INTO payments VALUES('old',42,1,100,'pending');
                INSERT INTO settings VALUES('promocode_user_42','15|0');
            """)

    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql,args).fetchall()

    def execute(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute(sql,args)

    def reserve(self,key="one",**kwargs):
        return reserve(self.path,key,42,1,100,**kwargs)

    def reward(self,percent=20,expires="2099-01-01"):
        self.execute("INSERT INTO user_promocodes VALUES(7,42,99,?,0,?,0,NULL)",(percent,expires))

    def bound(self,key="one",payment="pay",**kwargs):
        row,new=self.reserve(key,**kwargs)
        self.assertTrue(new)
        meta=metadata_for(row)
        amount=str(payable(row)/100)
        self.assertTrue(attach(self.path,key,payment,meta,amount))
        return meta,amount

    def settle(self,meta,amount,status="succeeded",payment="pay"):
        with transaction(self.path) as db:
            result=settle(db,payment,meta,amount,status)
            if status=="canceled" and result in ("released","done"):
                db.execute("UPDATE payments SET status='canceled' WHERE payment_id=?",(payment,))
            return result

    def test_manual_reserved_not_consumed(self):
        row,new=self.reserve()
        self.assertTrue(new)
        self.assertEqual(payable(row),8500)
        self.assertEqual(self.query("SELECT value FROM settings"),[("15|0",)])
        self.assertEqual(self.query("SELECT payment_id FROM payments"),[("old",)])

    def test_same_request_returns_original_without_create(self):
        row,_=self.reserve()
        again,new=self.reserve()
        self.assertEqual(again,row)
        self.assertFalse(new)

    def test_different_request_blocked_even_before_provider_id(self):
        self.reserve()
        with self.assertRaises(ReservationBusy):
            self.reserve("other")

    def test_concurrent_reservations_only_one_wins(self):
        def attempt(i):
            try:
                return self.reserve(str(i))[1]
            except ReservationBusy:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt,range(16))),1)

    def test_identity_cannot_change_on_replay(self):
        self.reserve()
        with self.assertRaises(ReservationConflict):
            reserve(self.path,"one",43,1,100)

    def test_best_discount_reward_wins(self):
        self.reward()
        row,_=self.reserve()
        self.assertEqual((row["kind"],payable(row)),("reward",8000))

    def test_expired_reward_not_selected(self):
        self.reward(expires="2000-01-01")
        row,_=self.reserve()
        self.assertEqual(row["kind"],"manual")

    def test_no_discount_no_reservation(self):
        self.execute("DELETE FROM settings")
        self.assertEqual(self.reserve(),(None,False))
        self.assertEqual(self.query("SELECT count(*) FROM promo_reservations"),[(0,)])

    def test_zero_payable_never_reserves(self):
        self.execute("UPDATE settings SET value='100|0'")
        with self.assertRaises(ZeroPayable):
            self.reserve()
        self.assertEqual(self.query("SELECT payment_id FROM payments"),[("old",)])

    def test_rounding_to_kopecks(self):
        row,_=reserve(self.path,"fraction",42,1,"10.03")
        self.assertEqual((row["discount_kopecks"],payable(row)),(150,853))

    def test_attach_is_atomic_and_idempotent(self):
        meta,amount=self.bound()
        self.assertFalse(attach(self.path,"one","pay",meta,amount))
        self.assertEqual(self.query("SELECT amount_rub,status FROM payments WHERE payment_id='pay'"),[(85.0,"pending")])
        with self.assertRaises(ReservationConflict):
            attach(self.path,"one","second",meta,amount)

    def test_attach_wrong_metadata_does_not_create_local_payment(self):
        row,_=self.reserve()
        meta=metadata_for(row)
        meta["telegram_id"]=43
        with self.assertRaises(ReservationConflict):
            attach(self.path,"one","pay",meta,85)
        self.assertEqual(self.query("SELECT state,payment_id FROM promo_reservations"),[("reserved",None)])

    def test_existing_payment_id_never_adopted(self):
        row,_=self.reserve()
        with self.assertRaises(sqlite3.IntegrityError):
            attach(self.path,"one","old",metadata_for(row),85)
        self.assertEqual(self.query("SELECT state,payment_id FROM promo_reservations"),[("reserved",None)])

    def test_verified_success_clears_only_reserved_manual_once(self):
        meta,amount=self.bound()
        self.assertEqual(self.settle(meta,amount),"consumed")
        self.assertEqual(self.settle(meta,amount),"done")
        self.assertEqual(self.query("SELECT value FROM settings"),[("",)])
        self.assertEqual(self.reserve("second"),(None,False))

    def test_verified_cancellation_preserves_discount_for_next_order(self):
        meta,amount=self.bound()
        self.assertEqual(self.settle(meta,amount,"canceled"),"released")
        self.assertEqual(self.settle(meta,amount,"canceled"),"done")
        row,new=self.reserve("two")
        self.assertTrue(new)
        self.assertEqual(payable(row),8500)

    def test_reward_success_records_parent_id_once(self):
        self.reward()
        meta,amount=self.bound()
        self.assertEqual(self.settle(meta,amount),"consumed")
        self.assertEqual(self.settle(meta,amount),"done")
        self.assertEqual(self.query("SELECT is_used FROM user_promocodes"),[(1,)])
        self.assertEqual(self.query("SELECT promocode_id,user_id FROM promocode_uses"),[(99,42)])
        self.assertEqual(self.query("SELECT value FROM settings"),[("15|0",)])

    def test_reward_cancel_does_not_consume(self):
        self.reward()
        meta,amount=self.bound()
        self.assertEqual(self.settle(meta,amount,"canceled"),"released")
        self.assertEqual(self.query("SELECT is_used FROM user_promocodes"),[(0,)])
        self.assertEqual(self.query("SELECT * FROM promocode_uses"),[])

    def test_wrong_amount_and_renewal_target_rejected(self):
        meta,amount=self.bound(action="renew",subscription_id=8)
        with self.assertRaises(ReservationConflict):
            self.settle(meta,"84.99")
        meta["subscription_id"]=9
        with self.assertRaises(ReservationConflict):
            self.settle(meta,amount)
        self.assertEqual(self.query("SELECT state FROM promo_reservations"),[("attached",)])

    def test_conflicting_terminal_events_rejected(self):
        meta,amount=self.bound()
        self.settle(meta,amount,"canceled")
        with self.assertRaises(ReservationConflict):
            self.settle(meta,amount)

    def test_changed_manual_value_is_not_destroyed(self):
        meta,amount=self.bound()
        self.execute("UPDATE settings SET value='25|0'")
        with self.assertRaises(ReservationConflict):
            self.settle(meta,amount)
        self.assertEqual(self.query("SELECT value FROM settings"),[("25|0",)])
        self.assertEqual(self.query("SELECT state FROM promo_reservations"),[("attached",)])

    def test_transaction_failure_rolls_back_reward_consumption(self):
        self.reward()
        meta,amount=self.bound()
        self.execute("CREATE TRIGGER fail_use BEFORE INSERT ON promocode_uses BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.settle(meta,amount)
        self.assertEqual(self.query("SELECT is_used FROM user_promocodes"),[(0,)])
        self.assertEqual(self.query("SELECT state FROM promo_reservations"),[("attached",)])

    def test_legacy_payment_untouched_even_without_schema(self):
        with transaction(self.path) as db:
            self.assertFalse(registered(db,"old"))
            self.assertEqual(settle(db,"old",{},100,"succeeded"),"ignored")
        self.assertEqual(self.query("SELECT status FROM payments WHERE payment_id='old'"),[("pending",)])
