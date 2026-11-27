"""Provider integration regressions, isolated SQLite and fake provider."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import asyncio
import sys
from datetime import datetime
from types import ModuleType
from decimal import Decimal
from promo_checkout import create_checkout, check_open_checkout, request_key
from promo_reservations import (
    ReservationBusy,ReservationConflict,open_reservation,settle_verified_success,validate_binding,
    save_manual_grant,transaction,has_unbound,recover_verified_binding
)
from refund_fulfillment import scope,record_canceled
from trigger_manager import TriggerManager
from test_renewal_checkout import load_functions


class PromoCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/"db.sqlite"
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE user_promocodes(id INTEGER PRIMARY KEY,user_id INTEGER,promocode_id INTEGER,
                    code TEXT,discount_percent INTEGER,discount_amount REAL,expires_at TEXT,
                    is_used INTEGER,used_at TEXT,created_at TEXT,trigger_type TEXT);
                CREATE TABLE promocode_uses(promocode_id INTEGER,user_id INTEGER,source TEXT);
                CREATE TABLE payments(id INTEGER PRIMARY KEY AUTOINCREMENT,payment_id TEXT UNIQUE,
                    telegram_id INTEGER,tariff_id INTEGER,amount_rub REAL,status TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,payment_state TEXT,vpn_state TEXT,tax_state TEXT);
                INSERT INTO settings VALUES('promocode_user_42','15|0');
                INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status,created_at)
                    VALUES('old',42,1,100,'pending','2020-01-01');
            """)
        self.provider=SimpleNamespace(create=Mock(side_effect=self.create),find_one=Mock())
        self.payment=None

    def execute(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute(sql,args)

    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql,args).fetchall()

    def create(self,payload,key):
        self.payment=SimpleNamespace(id="pay",status="pending",paid=False,captured_at=None,
            metadata=payload["metadata"],amount=SimpleNamespace(**payload["amount"]),
            confirmation=SimpleNamespace(confirmation_url="https://example.invalid/pay"))
        self.provider.find_one.return_value=self.payment
        return self.payment

    def checkout(self,key="first",owner=42,**kwargs):
        return create_checkout(self.path,self.provider,key,owner,1,"Test",100,"bot",**kwargs)

    def verified(self):
        return {"id":self.payment.id,"metadata":self.payment.metadata,"amount_rub":self.payment.amount.value}

    def test_create_checkout_is_bound_before_link_returned(self):
        self.assertEqual(self.checkout(),(8500,1500,"https://example.invalid/pay"))
        self.assertEqual(self.query("SELECT amount_rub,status FROM payments WHERE payment_id='pay'"),[(85.0,"pending")])
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
        self.assertEqual(self.query("SELECT value FROM settings"),[("15|0",)])
        self.assertEqual(self.provider.create.call_args.args[1],"first")

    def test_pending_reopened_without_creating(self):
        self.checkout()
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("pending",8500,"https://example.invalid/pay"))
        self.provider.create.assert_called_once()
        self.provider.find_one.assert_called_once_with("pay")
        self.assertEqual(check_open_checkout(self.path,self.provider,43),("none",None,None))

    def test_competing_checkout_cannot_create_provider_payment(self):
        self.checkout()
        for key in ("first","second"):
            with self.assertRaises(ReservationBusy):
                self.checkout(key)
        self.provider.create.assert_called_once()

    def test_ambiguous_create_remains_reserved(self):
        self.provider.create.side_effect=TimeoutError("unknown")
        with self.assertRaises(TimeoutError):
            self.checkout()
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("review",8500,None))
        with self.assertRaises(ReservationBusy):
            self.checkout("second")
        self.provider.create.assert_called_once()
        self.provider.find_one.assert_not_called()

    def test_provider_mismatch_never_attaches(self):
        def wrong(payload,key):
            payment=self.create(payload,key)
            payment.amount.value="84.99"
            return payment
        self.provider.create.side_effect=wrong
        with self.assertRaises(ReservationConflict):
            self.checkout()
        self.assertEqual(open_reservation(self.path,42)["state"],"reserved")
        self.assertEqual(self.query("SELECT payment_id FROM payments"),[("old",)])

    def test_confirmed_cancel_bypasses_historical_id_cutoff(self):
        self.checkout()
        self.assertEqual(scope(self.path,"pay"),"new")
        self.payment.status="canceled"
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("canceled",8500,None))
        self.assertEqual(self.query("SELECT status FROM payments WHERE payment_id='pay'"),[("canceled",)])
        self.assertEqual(self.query("SELECT state FROM promo_reservations"),[("released",)])
        self.assertEqual(self.query("SELECT value FROM settings"),[("15|0",)])
        self.assertEqual(record_canceled(self.path,"pay",metadata=self.payment.metadata,amount="85.00"),"done")

    def test_unverified_cancel_never_releases(self):
        self.checkout()
        self.payment.status="canceled"
        self.payment.paid=True
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("review",8500,None))
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
        with self.assertRaises((ReservationConflict,ValueError,TypeError)):
            record_canceled(self.path,"pay")

    def test_cancel_rolls_back_reservation_if_journal_insert_fails(self):
        self.checkout()
        self.execute("CREATE TRIGGER fail_cancel BEFORE INSERT ON payment_processing BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            record_canceled(self.path,"pay",metadata=self.payment.metadata,amount="85.00")
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")
        self.assertEqual(self.query("SELECT status FROM payments WHERE payment_id='pay'"),[("pending",)])

    def test_verified_success_consumes_discount_once(self):
        self.checkout()
        self.assertEqual(settle_verified_success(self.path,"pay",self.verified()),"consumed")
        self.assertEqual(settle_verified_success(self.path,"pay",self.verified()),"done")
        self.assertIsNone(open_reservation(self.path,42))
        self.assertEqual(self.query("SELECT value FROM settings"),[("",)])

    def test_paid_lookup_does_not_forge_webhook_or_consume(self):
        self.checkout()
        self.payment.status="succeeded"
        self.payment.paid=True
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("paid",8500,None))
        self.assertEqual(open_reservation(self.path,42)["state"],"attached")

    def test_wrong_renewal_target_refused(self):
        self.checkout(action="renew",subscription_id=17)
        obj=self.verified()
        obj["metadata"]={**obj["metadata"],"subscription_id":"18"}
        with self.assertRaises(ReservationConflict):
            settle_verified_success(self.path,"pay",obj)
        self.assertIsNotNone(open_reservation(self.path,42))

    def test_unknown_reservation_marker_rejected(self):
        with self.assertRaises(ReservationConflict):
            validate_binding(self.path,"old",{"promo_reservation_id":"unknown"},100)
        self.assertEqual(settle_verified_success(self.path,"old",{"metadata":{}}),"ignored")
        self.assertEqual(scope(self.path,"old"),"historical")

    def test_no_discount_preserves_standard_payment(self):
        self.execute("DELETE FROM settings")
        self.assertEqual(self.checkout(),(10000,0,"https://example.invalid/pay"))
        self.assertIsNone(open_reservation(self.path,42))
        self.assertEqual(self.query("SELECT amount_rub FROM payments WHERE payment_id='pay'"),[(100.0,)])
        self.assertNotIn("promo_reservation_id",self.payment.metadata)

    def test_reserved_reward_survives_expiry_cleanup(self):
        self.execute("INSERT INTO user_promocodes VALUES(7,42,99,'SAVE',20,0,'2099-01-01',0,NULL,'2026-01-01','referrals')")
        self.checkout()
        self.execute("UPDATE user_promocodes SET expires_at='2000-01-01'")
        manager=TriggerManager()
        manager.db_path=self.path
        self.assertEqual(manager.cleanup_expired_promocodes(42),0)
        self.assertEqual(settle_verified_success(self.path,"pay",self.verified()),"consumed")
        self.assertEqual(self.query("SELECT is_used FROM user_promocodes"),[(1,)])

    def test_reward_preview_never_consumes(self):
        self.execute("INSERT INTO user_promocodes VALUES(7,42,99,'SAVE',20,0,'2099-01-01',0,NULL,'2026-01-01','referrals')")
        manager=TriggerManager()
        manager.db_path=self.path
        self.assertTrue(manager.use_promocode(42,7))
        self.assertFalse(manager.use_promocode(43,7))
        self.assertEqual(self.query("SELECT is_used FROM user_promocodes"),[(0,)])

    def test_idempotency_key_is_stable_and_scoped(self):
        self.assertEqual(request_key(1,"a"),request_key(1,"a"))
        self.assertNotEqual(request_key(1,"a"),request_key(2,"a"))
        self.assertEqual(len(request_key(1,"a")),64)


    def lost_response(self):
        def lost(payload,key):
            self.create(payload,key)
            raise TimeoutError("response lost")
        self.provider.create.side_effect=lost
        with self.assertRaises(TimeoutError):
            self.checkout()
        self.provider.list=Mock(return_value=SimpleNamespace(items=[self.payment],next_cursor=None))

    def test_lost_response_recovers_existing_pending_payment_without_create(self):
        self.lost_response()
        self.assertTrue(has_unbound(self.path))
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("pending",8500,"https://example.invalid/pay"))
        self.assertFalse(has_unbound(self.path))
        self.provider.create.assert_called_once()
        self.provider.find_one.assert_called_once_with("pay")
        self.assertEqual(self.query("SELECT payment_id FROM payments ORDER BY id"),[("old",),("pay",)])

    def test_lost_response_recovers_canceled_payment(self):
        self.lost_response()
        self.payment.status="canceled"
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("canceled",8500,None))
        self.assertEqual(self.query("SELECT state FROM promo_reservations"),[("released",)])
        self.assertEqual(self.query("SELECT value FROM settings"),[("15|0",)])

    def test_empty_list_never_releases_reservation(self):
        self.lost_response()
        self.provider.list.return_value=SimpleNamespace(items=[],next_cursor=None)
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("review",8500,None))
        self.assertTrue(has_unbound(self.path))
        self.provider.find_one.assert_not_called()

    def test_incomplete_list_never_attaches(self):
        self.lost_response()
        self.provider.list.side_effect=[
            SimpleNamespace(items=[self.payment],next_cursor="a"),
            SimpleNamespace(items=[],next_cursor="b"),
            SimpleNamespace(items=[],next_cursor="c")]
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("review",8500,None))
        self.assertTrue(has_unbound(self.path))
        self.assertEqual(self.provider.list.call_count,3)
        self.provider.find_one.assert_not_called()

    def test_two_matching_ids_require_review(self):
        self.lost_response()
        other=SimpleNamespace(**vars(self.payment))
        other.id="other"
        self.provider.list.return_value=SimpleNamespace(items=[self.payment,other],next_cursor=None)
        self.assertEqual(check_open_checkout(self.path,self.provider,42),("review",8500,None))
        self.assertTrue(has_unbound(self.path))

    def test_recovered_binding_must_match_metadata(self):
        self.lost_response()
        wrong={**self.payment.metadata,"telegram_id":"43"}
        with self.assertRaises(ReservationConflict):
            recover_verified_binding(self.path,"pay",wrong,"85.00")
        self.assertTrue(has_unbound(self.path))
        self.assertFalse(recover_verified_binding(self.path,"old",{},"100.00"))
        self.assertEqual(self.query("SELECT payment_id FROM payments"),[("old",)])

    def test_expired_manual_grant_cannot_discount_new_checkout(self):
        with transaction(self.path) as db:
            save_manual_grant(db,42,1,"15|0","2000-01-01")
        self.assertEqual(self.checkout(),(10000,0,"https://example.invalid/pay"))
        self.assertEqual(self.query("SELECT value FROM settings"),[("",)])
        self.assertEqual(self.query("SELECT * FROM manual_promo_grants"),[])

    def test_expiry_during_pending_order_does_not_block_verified_payment(self):
        with transaction(self.path) as db:
            save_manual_grant(db,42,1,"15|0","2099-01-01")
        self.checkout()
        self.execute("UPDATE manual_promo_grants SET valid_until='2000-01-01'")
        self.assertEqual(settle_verified_success(self.path,"pay",self.verified()),"consumed")
        self.assertEqual(self.query("SELECT * FROM manual_promo_grants"),[])

    def test_cancel_after_expiry_does_not_restore_expired_discount_for_new_order(self):
        with transaction(self.path) as db:
            save_manual_grant(db,42,1,"15|0","2099-01-01")
        self.checkout()
        self.execute("UPDATE manual_promo_grants SET valid_until='2000-01-01'")
        self.payment.status="canceled"
        self.assertEqual(check_open_checkout(self.path,self.provider,42)[0],"canceled")
        self.provider.create.side_effect=lambda payload,key: SimpleNamespace(
            id="pay2",metadata=payload["metadata"],amount=SimpleNamespace(**payload["amount"]),
            confirmation=SimpleNamespace(confirmation_url="https://example.invalid/pay"))
        self.assertEqual(self.checkout("second")[0:2],(10000,0))

    def verifier(self):
        from aiohttp import web
        async def query(sql,args):
            with closing(sqlite3.connect(self.path)) as db:
                db.row_factory=sqlite3.Row
                return db.execute(sql,args).fetchall()
        return load_functions("webhook.py",{"verify_provider_payment"},
            dict(async_get_db_query=query,DB_PATH=self.path,asyncio=asyncio,sqlite3=sqlite3,
                web=web,get_setting=lambda key:"test",logger=Mock()))["verify_provider_payment"]

    def fake_sdk(self):
        fake=ModuleType("yookassa")
        fake.Configuration=SimpleNamespace()
        fake.Payment=self.provider
        enc=ModuleType("encryption")
        enc.decrypt=lambda value:value
        return patch.dict(sys.modules,{"yookassa":fake,"encryption":enc})

    async def test_real_verifier_recovers_missing_local_order_on_success(self):
        self.lost_response()
        self.payment.status="succeeded"
        self.payment.paid=True
        with self.fake_sdk():
            verified,error=await self.verifier()("pay",allow_recovery=True)
        self.assertIsNone(error)
        self.assertEqual(verified["object"]["amount_rub"],"85.00")
        self.assertEqual(settle_verified_success(self.path,"pay",verified["object"]),"consumed")
        self.provider.create.assert_called_once()

    async def test_read_only_verifier_does_not_adopt_unknown_order(self):
        self.lost_response()
        with self.fake_sdk():
            verified,error=await self.verifier()("pay")
        self.assertIsNone(verified)
        self.assertEqual(error.status,404)
        self.assertTrue(has_unbound(self.path))
        self.provider.find_one.assert_not_called()

    async def test_real_verifier_rejects_wrong_metadata_on_recovery(self):
        self.lost_response()
        self.payment.status="succeeded"
        self.payment.paid=True
        self.payment.metadata={**self.payment.metadata,"tariff_id":"99"}
        with self.fake_sdk():
            verified,error=await self.verifier()("pay",allow_recovery=True)
        self.assertIsNone(verified)
        self.assertEqual(error.status,409)
        self.assertTrue(has_unbound(self.path))

    async def test_real_verifier_rejects_unconfirmed_payment(self):
        self.lost_response()
        with self.fake_sdk():
            verified,error=await self.verifier()("pay",allow_recovery=True)
        self.assertIsNone(verified)
        self.assertEqual(error.status,403)
        self.assertTrue(has_unbound(self.path))
