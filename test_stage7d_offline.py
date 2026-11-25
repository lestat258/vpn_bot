"""Offline tests: no 3x-ui network writes, no real server/database access."""
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from vpn_issue_journal import IssueJournalError,ensure_schema, start_issue,finalize_issue,pending_summary
from apply_stage7d import patch_source,PatchMismatch

FIXTURE='''# vpn4us stage7c: single create attempt, fail closed
# vpn4us stage6a: mark new subscription as product-owned
# vpn4us stage4c: verify actual client on every selected inbound
import asyncio
async def create_subscription(telegram_id, tariff_id):
    telegram_id = telegram_id
    tariff_id = tariff_id
    best_server = {'id': 16}
    xui = None
    email = 'testclient'
    DB_PATH = 'db'
    _stage7c_snapshot = []
    assert_absent_on_all_inbounds(_stage7c_snapshot, email)
    result = await asyncio.to_thread(xui._request, 'POST', '/panel/api/clients/add', client_data)
    real_uuid = 'uuid'
    subid = 'subid'
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO subscriptions (xui_email) VALUES (?)', (email,))
    _vpn4us_sub_pk = c.lastrowid
    c.execute('UPDATE subscriptions SET managed_by_vpn4us=1 WHERE id=?',(_vpn4us_sub_pk,))
    c.executemany('INSERT INTO subscription_inbound_memberships (subscription_id,server_id,inbound_id) VALUES (?,?,?)', [(_vpn4us_sub_pk,16,92)])
    conn.commit()
    conn.close()
'''


@contextmanager
def dbconn(path):
    db=sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def setup_db(path):
    with dbconn(path) as db:
        db.executescript('''
        CREATE TABLE users(telegram_id INTEGER PRIMARY KEY);
        CREATE TABLE servers(id INTEGER PRIMARY KEY,is_active INTEGER);
        CREATE TABLE subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,telegram_id INTEGER,server_id INTEGER,xui_email TEXT,
            xui_client_uid TEXT,xui_subid TEXT,managed_by_vpn4us INTEGER,is_active INTEGER);
        CREATE TABLE subscription_inbound_memberships(subscription_id INTEGER,server_id INTEGER,inbound_id INTEGER);
        CREATE TABLE vpn4us_sync_journal(id INTEGER PRIMARY KEY);
        INSERT INTO users VALUES(8000000000000123);
        INSERT INTO servers VALUES(16,1);
        ''')
        ensure_schema(db)


class PatchTests(unittest.TestCase):
    def test_check_patch_compiles_and_positions(self):
        s=patch_source(FIXTURE)
        compile(s,'fixture','exec')
        self.assertIn('vpn4us stage7d: persist issue',s)
        self.assertLess(s.index('start_issue(DB_PATH'),s.index("'POST', '/panel/api/clients/add'"))
        self.assertLess(s.index('finalize_issue(c'),s.index('conn.commit()'))
        self.assertGreater(s.index('finalize_issue(c'),s.index("'INSERT INTO subscriptions"))
    def test_repeat_rejected(self):
        with self.assertRaises(PatchMismatch):
            patch_source(patch_source(FIXTURE))
    def test_missing_precheck_rejected(self):
        with self.assertRaises(PatchMismatch):
            patch_source(FIXTURE.replace('assert_absent_on_all_inbounds','anything_else'))
    def test_second_post_rejected(self):
        with self.assertRaises(PatchMismatch):
            patch_source(FIXTURE.replace('    real_uuid = ', "    result = await asyncio.to_thread(xui._request,'POST','/panel/api/clients/add', client_data)\n    real_uuid = "))
    def test_missing_membership_rejected(self):
        with self.assertRaises(PatchMismatch):
            patch_source(FIXTURE.replace('subscription_inbound_memberships','other_table'))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'data.db'
        setup_db(self.path)
    def tearDown(self):
        self.tmp.cleanup()
    def start(self, email='test_email', tid=8000000000000123):
        return start_issue(self.path,tid,16,email,15)
    def test_start_commits_before_post(self):
        jid=self.start()
        with dbconn(self.path) as db:
            self.assertEqual(db.execute('SELECT state FROM vpn4us_issue_journal WHERE id=?',(jid,)).fetchone(),('prepared',))
    def test_blocks_retry_even_with_new_email(self):
        self.start()
        with self.assertRaisesRegex(IssueJournalError,'unresolved'):
            self.start('another_email')
    def test_blocks_same_email_cross_user(self):
        self.start()
        with dbconn(self.path) as db: db.execute('INSERT INTO users VALUES(8000000000000199)')
        with self.assertRaisesRegex(IssueJournalError,'already used'):
            self.start('test_email',8000000000000199)
    def test_atomic_finalize_success(self):
        jid=self.start()
        with dbconn(self.path) as db:
            c=db.cursor()
            c.execute('''INSERT INTO subscriptions(telegram_id,server_id,xui_email,xui_client_uid,xui_subid,managed_by_vpn4us,is_active)
              VALUES(?,?,?,?,?,?,?)''',(8000000000000123,16,'test_email','a-uuid','a-subid',1,1))
            pk=c.lastrowid
            c.execute('INSERT INTO subscription_inbound_memberships VALUES(?,?,?)',(pk,16,92))
            finalize_issue(c,jid,16,8000000000000123,'test_email',pk,'a-uuid','a-subid')
        with dbconn(self.path) as db:
            self.assertEqual(db.execute('SELECT state,subscription_id FROM vpn4us_issue_journal WHERE id=?',(jid,)).fetchone(),('verified',pk))
    def test_failure_rollback_keeps_prepared(self):
        jid=self.start()
        try:
            with dbconn(self.path) as db:
                c=db.cursor()
                c.execute('''INSERT INTO subscriptions(telegram_id,server_id,xui_email,xui_client_uid,xui_subid,managed_by_vpn4us,is_active)
                  VALUES(?,?,?,?,?,?,?)''',(8000000000000123,16,'test_email','uid','sid',1,1))
                pk=c.lastrowid
                c.execute('INSERT INTO subscription_inbound_memberships VALUES(?,?,?)',(pk,16,92))
                finalize_issue(c,jid,16,8000000000000123,'test_email',pk,'uid','WRONG')
        except IssueJournalError:
            pass
        with dbconn(self.path) as db:
            self.assertEqual(db.execute('SELECT state FROM vpn4us_issue_journal WHERE id=?',(jid,)).fetchone(),('prepared',))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0],0)
    def test_missing_membership_refuses(self):
        jid=self.start()
        with dbconn(self.path) as db:
            c=db.cursor()
            c.execute('''INSERT INTO subscriptions(telegram_id,server_id,xui_email,xui_client_uid,xui_subid,managed_by_vpn4us,is_active)
                VALUES(?,?,?,?,?,?,?)''',(8000000000000123,16,'test_email','u','s',1,1))
            with self.assertRaisesRegex(IssueJournalError,'empty'):
                finalize_issue(c,jid,16,8000000000000123,'test_email',c.lastrowid,'u','s')
    def test_blocks_existing_active_owned(self):
        with dbconn(self.path) as db:
            db.execute('''INSERT INTO subscriptions(telegram_id,server_id,xui_email,xui_client_uid,xui_subid,managed_by_vpn4us,is_active)
                VALUES(?,?,?,?,?,?,?)''',(8000000000000123,16,'earlier','u','s',1,1))
        with self.assertRaisesRegex(IssueJournalError,'already exists'):
            self.start()
    def test_no_client_data_output(self):
        self.start()
        self.assertEqual(pending_summary(self.path),{'prepared':1})
    def test_missing_user_fails(self):
        with self.assertRaisesRegex(IssueJournalError,'User missing'):
            self.start(tid=8000000000000177)
    def test_invalid_ids_fail(self):
        with self.assertRaises(IssueJournalError):
            start_issue(self.path,True,16,'abc',15)
        with self.assertRaises(IssueJournalError):
            start_issue(self.path,8000000000000123,-1,'abc',15)

if __name__=='__main__':
    unittest.main()
