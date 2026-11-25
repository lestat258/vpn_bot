import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from apply_stage6a import patch_source, PatchMismatch, migrate
from inbound_policy import save_policy
from subscription_sync_plan import (
    SyncPlanningError, select_existing, index_inbounds, plan_subscription, build_plan
)

UID = str(uuid.uuid4())
SID = 'sid_abc_def_123'


def fixture_source():
    return '''import sqlite3
# vpn4us stage4c: verify actual client on every selected inbound
# vpn4us stage5: explicit subscription base (or legacy auto)
class VPNService:
    @staticmethod
    async def create_subscription():
        best_server={'id':16}
        inbound_ids=[92,100]
        subid='abc'
        conn=sqlite3.connect(DB_PATH)
        c=conn.cursor()
        c.execute('INSERT INTO subscriptions (xui_email) VALUES (?)', ('test',))
        subscription_id=c.lastrowid
        conn.commit()
        conn.close()
        return subscription_id
'''


def fake_inbound(iid, tag, has_client=True, *, email='ours@unit.invalid', uid=UID,
                 subid=SID, protocol='vless', enabled=True):
    records = []
    if has_client:
        records = [{'email': email, 'id': uid, 'subId': subid, 'enable': True,
                    'expiryTime': 1900000000000}]
    return {'id': iid, 'enable': enabled, 'tag': tag, 'protocol': protocol,
            'settings': json.dumps({'clients': records})}


class MigrationTests(unittest.TestCase):
    def test_source_patch_and_compile(self):
        result = patch_source(fixture_source())
        self.assertLess(result.index('INSERT INTO subscriptions'), result.index('mark new subscription'))
        self.assertLess(result.index('mark new subscription'), result.index('conn.commit()'))
        self.assertIn('managed_by_vpn4us=1', result)
        compile(result, 'patched_fixture.py', 'exec')

    def test_reapply_denied(self):
        with self.assertRaises(PatchMismatch):
            patch_source(patch_source(fixture_source()))

    def test_missing_commit_denied(self):
        with self.assertRaises(PatchMismatch):
            patch_source(fixture_source().replace('        conn.commit()\n', ''))

    def test_migration_idempotent_existing_not_adopted(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, xui_email TEXT, '
                       'xui_client_uid TEXT, is_active INTEGER)')
            db.execute("INSERT INTO subscriptions (xui_email,is_active) VALUES ('legacy',1)")
            migrate(db)
            migrate(db)
            row = db.execute('SELECT server_id, managed_by_vpn4us, xui_subid '
                             'FROM subscriptions WHERE id=1').fetchone()
            self.assertEqual(row, (None, 0, None))
            self.assertIn('subscription_inbound_memberships',
                          [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")])


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 'data.db'
        with sqlite3.connect(self.dbpath) as db:
            db.execute('CREATE TABLE servers (id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO servers VALUES (16)')
            db.execute('''CREATE TABLE subscriptions (id INTEGER PRIMARY KEY,
              xui_email TEXT, xui_client_uid TEXT, xui_subid TEXT, is_active INTEGER,
              server_id INTEGER, managed_by_vpn4us INTEGER)''')
            migrate(db)
            db.execute('''INSERT INTO subscriptions VALUES (1,?,?,?,?,?,?)''',
                       ('ours@unit.invalid', UID, SID, 1, 16, 1))
            db.execute('''INSERT INTO subscriptions VALUES (2,?,?,?,?,?,?)''',
                       ('foreign@unit.invalid', UID, SID, 1, 16, 0))
            db.execute('INSERT INTO subscription_inbound_memberships '
                       '(subscription_id, server_id, inbound_id) VALUES(1,16,92)')
            db.execute('INSERT INTO subscription_inbound_memberships '
                       '(subscription_id, server_id, inbound_id) VALUES(1,16,100)')
        save_policy(self.dbpath,16, {'mode':'group','group_number':1,'minimum_count':2})
        self.inbounds = [fake_inbound(92,'server-a--1'), fake_inbound(100,'server-b--1'),
                         fake_inbound(101,'server-c--1',False), fake_inbound(95,'cascade',False)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_new_matching_inbound(self):
        plan = build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['desired_inbounds'], [92,100,101])
        self.assertEqual(plan['managed_subscriptions'], 1)
        self.assertEqual(plan['would_add'], 1)
        self.assertEqual(plan['would_remove'], 0)
        self.assertEqual(plan['details'][0]['add'], [101])

    def test_removed_group_marker_detaches_only_managed(self):
        self.inbounds[0]['tag']='server-a'
        plan=build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['would_remove'], 1)
        self.assertEqual(plan['details'][0]['remove'],[92])
        self.assertEqual(plan['details'][0]['add'],[101])

    def test_physically_deleted_inbound_needs_only_metadata_cleanup(self):
        self.inbounds=[x for x in self.inbounds if x['id'] != 92]
        plan=build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['would_remove'], 0)
        self.assertEqual(plan['would_prune_deleted'],1)

    def test_existing_clients_ignore_sales_minimum(self):
        self.inbounds=self.inbounds[:2]
        self.inbounds[0]['enable']=False
        plan=build_plan(self.dbpath,16,self.inbounds)
        self.assertTrue(plan['below_sales_minimum'])
        self.assertEqual(plan['desired_inbounds'],[100])
        self.assertEqual(plan['would_remove'], 0)
        self.assertEqual(plan['retained_disabled'], 1)

    def test_identity_conflict_blocked(self):
        self.inbounds[1]['settings']=json.dumps({'clients':[{
            'email':'ours@unit.invalid','id':str(uuid.uuid4()),'subId':SID,'enable':True}]})
        plan=build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['needs_manual_review'],1)
        self.assertEqual(plan['would_add'],0)
        self.assertEqual(plan['would_remove'],0)

    def test_unmanaged_membership_blocks(self):
        self.inbounds[3] = fake_inbound(95,'cascade',True)
        plan = build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['needs_manual_review'],1)
        self.assertEqual(plan['would_add'],0)

    def test_no_desired_blocks(self):
        for inbound in self.inbounds:
            inbound['tag']='service'
        plan = build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['desired_inbounds'],[])
        self.assertEqual(plan['needs_manual_review'],1)
        self.assertEqual(plan['would_remove'],0)

    def test_manual_partial_removal_allowed(self):
        p={'mode':'manual','inbound_ids':[92,100],'minimum_count':2}
        selected=select_existing(p,[fake_inbound(100,'x')])
        self.assertEqual(selected,[100])

    def test_non_vless_inbound_without_client_list(self):
        self.inbounds.append({'id':93,'protocol':'hysteria','enable':True,
                             'tag':'service','settings':json.dumps({'auth':'x'})})
        plan=build_plan(self.dbpath,16,self.inbounds)
        self.assertEqual(plan['would_add'],1)

    def test_non_vless_selected_denied(self):
        self.inbounds[2]['protocol'] = 'trojan'
        with self.assertRaises(SyncPlanningError):
            build_plan(self.dbpath,16,self.inbounds)

    def test_malformed_duplicate_clients_fail_closed(self):
        old=json.loads(self.inbounds[0]['settings'])
        old['clients'].append(dict(old['clients'][0]))
        self.inbounds[0]['settings']=json.dumps(old)
        with self.assertRaises(SyncPlanningError):
            build_plan(self.dbpath,16,self.inbounds)


if __name__ == '__main__':
    unittest.main()
