"""Offline safety tests for stage 6D: never connect to a real 3x-ui server."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import uuid

from sync_guarded import (ensure_journal, select_one, SyncSafetyError, validate_effect,
    memberships, begin_action, confirm_action, mark_review, journal_unresolved, api_change,
    run_one, JOURNAL_DDL)
from apply_stage6a import migrate
from inbound_policy import save_policy

UID='00000000-0000-4000-8000-000000000123'
EMAIL='owned@example.invalid'
SUBID='my-test-subid'


def inbound(iid,tag='edge--1',has_client=True,*,uid=UID,subid=SUBID,enabled=True):
    clients = [{'email':EMAIL,'id':uid,'subId':subid,'enable':True}] if has_client else []
    return {'id':iid,'tag':tag,'remark':'node','enable':enabled,
            'protocol':'vless','settings':json.dumps({'clients':clients})}

class FakeHTTP:
    def __init__(self, panel): self.panel=panel;self.posts=0
    def post(self,url,**kw):
        self.posts += 1
        self.panel.calls.append((url,kw))
        id=kw['json']['inboundIds'][0]
        action=url.rsplit('/',1)[-1]
        if self.panel.mode=='http_fail':
            return SimpleNamespace(status_code=503,json=lambda: {})
        if action=='attach':
            for ib in self.panel.data:
                if ib['id']==id:
                    settings=json.loads(ib['settings'])
                    settings['clients'].append({'email':EMAIL,'id':UID,'subId':SUBID,'enable':True})
                    ib['settings']=json.dumps(settings)
        if action=='detach':
            for ib in self.panel.data:
                if ib['id']==id:
                    settings=json.loads(ib['settings'])
                    settings['clients']=[x for x in settings['clients'] if x['email']!=EMAIL]
                    ib['settings']=json.dumps(settings)
        return SimpleNamespace(status_code=200,json=lambda: {'success':True})

class FakePanel:
    def __init__(self,data,mode='normal'):
        import copy
        self.data=copy.deepcopy(data)
        self.mode=mode
        self.calls=[]
        self.session=FakeHTTP(self)
    def get_inbounds(self):
        import copy
        return copy.deepcopy(self.data)

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'data.db'
        with sqlite3.connect(self.db) as conn:
            conn.executescript('''CREATE TABLE servers(id INTEGER PRIMARY KEY,url TEXT,
            api_token TEXT,is_active INTEGER);
            INSERT INTO servers VALUES(16,'https://example.invalid','fake',1);
            CREATE TABLE subscriptions (id INTEGER PRIMARY KEY,xui_email TEXT,
                xui_client_uid TEXT,is_active INTEGER);''')
            migrate(conn)
            conn.execute('''INSERT INTO subscriptions
            (id,xui_email,xui_client_uid,is_active,server_id,managed_by_vpn4us,xui_subid)
            VALUES (1,?,?,?,?,?,?)''',(EMAIL,UID,1,16,1,SUBID))
            conn.execute('''INSERT INTO subscriptions
            (id,xui_email,xui_client_uid,is_active,server_id,managed_by_vpn4us,xui_subid)
            VALUES (2,?,?,?,?,?,?)''',('foreign@example.invalid',UID,1,16,0,SUBID))
            for iid in (92,100):
                conn.execute('''INSERT INTO subscription_inbound_memberships
                  (subscription_id,server_id,inbound_id) VALUES(1,16,?)''',(iid,))
        save_policy(self.db,16,{'mode':'group','group_number':1,'minimum_count':2})
        ensure_journal(self.db)
        self.snap=[inbound(92),inbound(100),inbound(101,has_client=False),inbound(95,'cascade',False)]
    def owned(self): return {'id':1,'xui_email':EMAIL,'xui_client_uid':UID,'xui_subid':SUBID}

class PickTests(Fixture):
    def test_add_one(self):
        from subscription_sync_plan import build_plan
        plan=build_plan(self.db,16,self.snap)
        self.assertEqual(select_one(plan,1),('attach',101))
    def test_not_ours(self):
        from subscription_sync_plan import build_plan
        with self.assertRaises(SyncSafetyError):
            select_one(build_plan(self.db,16,self.snap),2)
    def test_mismatch_refuses(self):
        from subscription_sync_plan import build_plan
        self.snap[0]=inbound(92,uid='00000000-0000-4000-8000-000000000124')
        with self.assertRaises(SyncSafetyError):
            select_one(build_plan(self.db,16,self.snap),1)
    def test_bad_panel_url(self):
        from sync_guarded import validate_panel_url
        for url in ('http://x.invalid','https://u:p@x.invalid','https://x.invalid?a=1'):
            with self.assertRaises(SyncSafetyError):validate_panel_url(url)
    def test_exact_memberships(self):
        self.assertEqual(memberships(self.snap,self.owned()),{92,100})
    def test_validate_effect(self):
        self.assertTrue(validate_effect('attach',101,{92,100},{92,100,101}))
        self.assertFalse(validate_effect('attach',101,{92,100},{92,100,101,95}))
        self.assertTrue(validate_effect('detach',92,{92,100},{100}))
        self.assertFalse(validate_effect('detach',92,{92,100},{92,100}))
    def test_metadata_drift_refuses(self):
        from subscription_sync_plan import build_plan
        self.snap[2]=inbound(101,has_client=True)
        with self.assertRaises(SyncSafetyError):select_one(build_plan(self.db,16,self.snap),1)

class JournalTests(Fixture):
    def test_journal_review_blocks(self):
        jid=begin_action(self.db,16,1,'attach',101,(EMAIL,UID,SUBID))
        self.assertTrue(journal_unresolved(self.db,16,1))
        with self.assertRaises(SyncSafetyError):
            begin_action(self.db,16,1,'attach',101,(EMAIL,UID,SUBID))
        mark_review(self.db,jid,'api_failed')
        self.assertTrue(journal_unresolved(self.db,16,1))
    def test_confirmation_updates_membership(self):
        jid=begin_action(self.db,16,1,'attach',101,(EMAIL,UID,SUBID))
        confirm_action(self.db,jid,16,1,'attach',101,self.owned())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT state FROM vpn4us_sync_journal WHERE id=?',(jid,)).fetchone()[0], 'verified')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_inbound_memberships WHERE inbound_id=101').fetchone()[0],1)
    def test_detach_updates_membership(self):
        jid=begin_action(self.db,16,1,'detach',92,(EMAIL,UID,SUBID))
        confirm_action(self.db,jid,16,1,'detach',92,self.owned())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_inbound_memberships WHERE inbound_id=92').fetchone()[0],0)
    def test_no_unowned_mutation(self):
        with self.assertRaises(SyncSafetyError):
            begin_action(self.db,16,2,'attach',101,(EMAIL,UID,SUBID))

class ApiTests(Fixture):
    def test_api_payload(self):
        panel=FakePanel(self.snap)
        self.assertTrue(api_change(panel,'https://example.invalid',EMAIL,'attach',101))
        endpoint,payload=panel.calls[0]
        self.assertTrue(endpoint.endswith('/attach'))
        self.assertEqual(payload['json'],{'inboundIds':[101]})
        self.assertTrue(payload['verify'])
        self.assertFalse(payload['allow_redirects'])
    def test_run_dryrun_never_posts(self):
        panel=FakePanel(self.snap)
        self.assertEqual(run_one(self.db,16,1,apply=False,xui_factory=lambda **kw:panel),'DRY_RUN_ONLY')
        self.assertEqual(panel.session.posts,0)
    def test_run_reject_unowned(self):
        panel=FakePanel(self.snap)
        with self.assertRaises(SyncSafetyError):
            run_one(self.db,16,2,apply=False,xui_factory=lambda **kw:panel)
        self.assertEqual(panel.session.posts,0)
    def test_apply_rejects_active_service(self):
        panel=FakePanel(self.snap)
        with patch('sync_guarded.require_services_stopped',side_effect=SyncSafetyError('running')):
            with self.assertRaises(SyncSafetyError):
                run_one(self.db,16,1,apply=True,xui_factory=lambda **kw:panel)
        self.assertEqual(panel.session.posts,0)
    def test_apply_mock_success(self):
        panel=FakePanel(self.snap)
        with patch('sync_guarded.require_services_stopped',return_value=None):
            self.assertEqual(run_one(self.db,16,1,apply=True,xui_factory=lambda **kw:panel),'VERIFIED_ONE_ACTION')
        self.assertEqual(panel.session.posts,1)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT state FROM vpn4us_sync_journal').fetchone()[0],'verified')
    def test_apply_mock_api_failure(self):
        panel=FakePanel(self.snap,mode='http_fail')
        with patch('sync_guarded.require_services_stopped',return_value=None):
            self.assertEqual(run_one(self.db,16,1,apply=True,xui_factory=lambda **kw:panel),'REVIEW_API_FAILED')
        self.assertEqual(panel.session.posts,1)
        self.assertTrue(journal_unresolved(self.db,16,1))
    def test_apply_no_repeat_on_ambiguous(self):
        panel=FakePanel(self.snap,mode='http_fail')
        with patch('sync_guarded.require_services_stopped',return_value=None):
            self.assertEqual(run_one(self.db,16,1,apply=True,xui_factory=lambda **kw:panel),'REVIEW_API_FAILED')
            with self.assertRaises(SyncSafetyError):
                run_one(self.db,16,1,apply=True,xui_factory=lambda **kw:panel)
        self.assertEqual(panel.session.posts,1)

if __name__=='__main__':unittest.main()
