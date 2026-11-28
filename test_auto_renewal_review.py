"""No real panel, bot, database or provider."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_auto_renewal as prior
import auto_renewal as ar
import auto_renewal_review as review
import auto_renewal_worker as worker


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.f=prior.AutoRenewalTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.path=self.f.path
        self.f.sql("INSERT INTO settings VALUES('admin_id','99')")
        self.evidence="Independent panel audit confirmed final result"

    def hold(self, *, applied=True):
        if applied:
            self.f.panel.fail=True
            with self.assertRaises(ValueError):
                self.f.run_one()
        else:
            original=self.f.panel._request
            def fail(method,*args,**kwargs):
                if method=="POST": return None
                return original(method,*args,**kwargs)
            with patch.object(self.f.panel,"_request",side_effect=fail):
                with self.assertRaises(ValueError):
                    self.f.run_one()
        self.operation=review.pending_ids(self.path)[0]
        self.assertEqual(self.f.balance(),49.75)

    def decide(self,outcome,**kw):
        return review.resolve(self.path,self.operation,outcome,kw.get("actor",99),
                              kw.get("evidence",self.evidence),self.f.factory,
                              verified_finality=kw.get("verified_finality",True))

    def test_empty_journal_no_schema_creation(self):
        self.assertEqual(review.pending_ids(self.path),[])
        self.assertEqual(self.f.sql("SELECT name FROM sqlite_master WHERE name LIKE 'auto_renewal_%'"),[])

    def test_inspect_target_read_only_even_when_disabled(self):
        self.hold()
        with patch.dict(os.environ,{"VPN4US_SAFE_AUTORENEW":"0"}):
            result=review.inspect(self.path,self.operation,self.f.factory)
        self.assertEqual(result["status"],"target_observed")
        self.assertEqual(self.f.state(),[("needs_review",)])
        self.assertEqual(len(self.f.panel.writes),1)
        self.assertEqual(self.f.balance(),49.75)

    def test_confirm_no_second_debit_or_panel_write(self):
        self.hold()
        with patch.dict(os.environ,{"VPN4US_SAFE_AUTORENEW":"0"}):
            self.assertEqual(self.decide("confirmed"),"confirmed")
        self.assertEqual(self.f.balance(),49.75)
        self.assertEqual(self.f.state(),[("done",)])
        self.assertEqual(ar.review_count(self.path),0)
        self.assertEqual(self.f.sql("SELECT count(*) FROM balance_operations"),[(1,)])
        self.assertEqual(len(self.f.panel.writes),1)
        self.assertNotEqual(self.f.sql("SELECT end_date FROM subscriptions"),[(self.f.end,)])
        with self.assertRaises(ValueError): self.decide("confirmed")

    def test_original_observed_is_not_final_failure(self):
        self.hold(applied=False)
        self.assertEqual(review.inspect(self.path,self.operation,self.f.factory)["status"],"original_observed")
        with self.assertRaises(ValueError): self.decide("refunded",verified_finality=False)
        self.assertEqual(self.f.balance(),49.75)
        self.assertEqual(ar.review_count(self.path),1)

    def test_refund_exact_once_and_disable_same_cycle(self):
        self.hold(applied=False)
        self.assertEqual(self.decide("refunded"),"refunded")
        self.assertEqual(self.f.balance(),200)
        self.assertEqual(self.f.balance(43),900)
        self.assertEqual(self.f.sql("SELECT kind,delta_kopecks,actor_id FROM balance_operations ORDER BY rowid"),
                         [("auto_renew_reserve",-15025,None),("auto_renew_refund",15025,99)])
        self.assertEqual(self.f.sql("SELECT auto_renew,end_date FROM subscriptions"),[(0,self.f.end)])
        self.assertEqual(review.pending_ids(self.path),[])
        self.assertEqual(ar.review_count(self.path),0)
        self.f.sql("UPDATE subscriptions SET auto_renew=1")
        self.assertEqual(ar.candidates(self.path,prior.NOW),[])
        with self.assertRaises(ValueError): self.decide("refunded")
        self.assertEqual(self.f.balance(),200)

    def test_refund_for_target_observed_refused(self):
        self.hold()
        with self.assertRaises(ValueError): self.decide("refunded")
        self.assertEqual(self.f.balance(),49.75)

    def test_confirm_for_original_observed_refused(self):
        self.hold(applied=False)
        with self.assertRaises(ValueError): self.decide("confirmed")
        self.assertEqual(ar.review_count(self.path),1)

    def test_wrong_actor_and_empty_evidence_refused(self):
        self.hold()
        with self.assertRaises(ValueError): self.decide("confirmed",actor=98)
        with self.assertRaises(ValueError): self.decide("confirmed",evidence="short")
        self.assertEqual(ar.review_count(self.path),1)

    def test_quota_and_membership_changes_refused(self):
        self.hold()
        self.f.panel.client["totalGB"]=123
        with self.assertRaises(ValueError): self.decide("confirmed")
        self.f.panel.client["totalGB"]=0
        self.f.panel.ids=[5,9]
        with self.assertRaises(ValueError): self.decide("confirmed")
        self.assertEqual(ar.review_count(self.path),1)

    def test_missing_original_evidence_refused(self):
        self.hold()
        self.f.sql("DELETE FROM auto_renewal_evidence")
        with self.assertRaises(ValueError): self.decide("confirmed")
        self.assertEqual(ar.review_count(self.path),1)

    def test_local_change_during_get_prevents_refund(self):
        self.hold(applied=False)
        request=self.f.panel._request
        def change(*a,**kw):
            result=request(*a,**kw)
            self.f.sql("UPDATE users SET is_blocked=1 WHERE telegram_id=42")
            return result
        with patch.object(self.f.panel,"_request",side_effect=change):
            with self.assertRaises(ValueError): self.decide("refunded")
        self.assertEqual(self.f.balance(),49.75)

    def test_refund_transaction_rolls_back_if_resolution_insert_fails(self):
        self.hold(applied=False)
        self.f.sql("""CREATE TRIGGER reject_resolution BEFORE INSERT ON auto_renewal_resolutions
            BEGIN SELECT RAISE(ABORT,'synthetic'); END""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.decide("refunded")
        self.assertEqual(self.f.balance(),49.75)
        self.assertEqual(self.f.sql("SELECT auto_renew FROM subscriptions"),[(1,)])
        self.assertEqual(ar.review_count(self.path),1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_cycle_counts_review_without_charges(self):
        manager=Mock()
        manager.process_renewals=AsyncMock()
        with patch.object(worker,"review_count",return_value=1) as count, \
             patch.dict(os.environ,{"VPN4US_SAFE_AUTORENEW":"0"}):
            await worker.cycle("synthetic.db",manager)
        count.assert_called_once_with("synthetic.db")
        manager.process_renewals.assert_not_awaited()

    async def test_enabled_cycle_delegates_to_guarded_manager(self):
        manager=Mock()
        manager.process_renewals=AsyncMock()
        with patch.object(worker,"review_count",return_value=0), \
             patch.dict(os.environ,{"VPN4US_SAFE_AUTORENEW":"1"}):
            await worker.cycle("synthetic.db",manager)
        manager.process_renewals.assert_awaited_once()

    async def test_cancel_closes_notification_session(self):
        manager=Mock()
        manager.bot.session.close=AsyncMock()
        with patch.object(worker,"cycle",new_callable=AsyncMock) as cycle:
            task=asyncio.create_task(worker.renewal_worker("synthetic.db",interval=300,manager=manager))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        cycle.assert_awaited_once()
        manager.bot.session.close.assert_awaited_once()
