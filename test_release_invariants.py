"""Offline VPN issuance safety regressions. No real server/DB/network calls."""
import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from subscription_links import validate_subscription_base, build_subscription_link
from vpn_client_verify import inspect_vless_client, VerificationError
from vpn_creation_guard import gb_to_bytes, assert_absent_on_all_inbounds, CreationPreflightError
from vpn_issue_journal import start_issue, ensure_schema, IssueJournalError

ROOT = Path(__file__).resolve().parent
EMAIL = 'test@example.invalid'
UID = '12345678-1234-4234-8234-123456789abc'
SID = 'regression_subid'


def inbound(iid, *, present=True, uid=UID):
    clients = [{'email': EMAIL, 'id': uid, 'subId': SID, 'enable': True,
                'expiryTime': 1800000000000}] if present else []
    return {'id': iid, 'protocol': 'vless', 'enable': True,
            'settings': json.dumps({'clients': clients})}


class ReleaseSafety(unittest.TestCase):
    def test_single_add_and_journal_before_api(self):
        tree = ast.parse((ROOT / 'services/vpn_service.py').read_text(encoding='utf-8'))
        service = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VPNService')
        method = next(n for n in service.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == 'create_subscription')
        calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]
        posts = [n.lineno for n in calls if any(isinstance(a, ast.Constant)
                 and a.value == '/panel/api/clients/add' for a in n.args)]
        journal = [n.lineno for n in calls if isinstance(n.func, ast.Name)
                   and n.func.id == 'start_issue']
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(journal), 1)
        self.assertLess(journal[0], posts[0])
        forbidden = {'add_client', 'attach_client_to_inbounds', 'update_inbound_settings'}
        self.assertFalse([n for n in calls if isinstance(n.func, ast.Attribute)
                          and n.func.attr in forbidden])

    def test_subscription_id_uses_subscription_pk(self):
        tree = ast.parse((ROOT / 'services/vpn_service.py').read_text(encoding='utf-8'))
        service = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VPNService')
        method = next(n for n in service.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == 'create_subscription')
        assigns = [n for n in ast.walk(method) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'subscription_id' for t in n.targets)]
        self.assertEqual(len(assigns), 1)
        self.assertIsInstance(assigns[0].value, ast.Name)
        self.assertEqual(assigns[0].value.id, '_vpn4us_sub_pk')

    def test_no_duplicate_or_service_inbound(self):
        selected = [inbound(92), inbound(100), inbound(95, present=False)]
        self.assertEqual(inspect_vless_client(selected, [92, 100], EMAIL)['subid'], SID)
        with self.assertRaises(VerificationError):
            inspect_vless_client([inbound(92), inbound(100), inbound(95)], [92, 100], EMAIL)
        with self.assertRaises(VerificationError):
            inspect_vless_client([inbound(92), inbound(100, uid='12345678-1234-4234-8234-123456789999')],
                                 [92, 100], EMAIL)

    def test_limits_and_duplicate_email(self):
        self.assertEqual(gb_to_bytes(1), 1073741824)
        with self.assertRaises(CreationPreflightError):
            gb_to_bytes(-1)
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([inbound(92)], EMAIL)

    def test_https_subscription_link(self):
        self.assertEqual(build_subscription_link('https://example.invalid/sub/', SID),
                         'https://example.invalid/sub/' + SID)
        for bad in ('http://example.invalid/sub/', 'https://user:pass@example.invalid/sub/',
                    'https://example.invalid/sub?secret=x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_subscription_base(bad)

    def test_prepared_issue_survives_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'offline.db')
            with sqlite3.connect(db) as con:
                con.executescript('''
                    CREATE TABLE users (telegram_id INTEGER PRIMARY KEY);
                    CREATE TABLE servers (id INTEGER PRIMARY KEY, is_active INTEGER);
                    CREATE TABLE subscriptions (telegram_id INTEGER, managed_by_vpn4us INTEGER, is_active INTEGER);
                    INSERT INTO users VALUES (12345);
                    INSERT INTO servers VALUES (16, 1);
                ''')
                ensure_schema(con)
            issue = start_issue(db, 12345, 16, EMAIL, 15)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT state FROM vpn4us_issue_journal WHERE id=?',
                                             (issue,)).fetchone(), ('prepared',))
            with self.assertRaises(IssueJournalError):
                start_issue(db, 12345, 16, 'another@example.invalid', 15)

    def test_release_plan_source_allowlist(self):
        from deploy.release_plan import allowed_source
        for permitted in ('services/vpn_service.py', 'handlers/connect.py',
                          'admin_modules/servers.py', 'bot.py',
                          'templates/index.html', 'static/app.js'):
            with self.subTest(permitted=permitted):
                self.assertTrue(allowed_source(permitted))
        for forbidden in ('.env', 'data.db', 'secrets/key.pem',
                          'services/../data.db', 'services/secret.key',
                          'test_release_invariants.py', 'apply_patch.py',
                          'deploy/staging_pull.py', '/opt/vpn-bot/app.py',
                          'templates/../../.env', 'static/private.p12',
                          'services\\\\vpn_service.py'):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(allowed_source(forbidden))

    def test_release_plan_rejects_symlinks_and_accepts_missing(self):
        from deploy.release_plan import read_regular_file, PlanBlocked
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'services').mkdir()
            (root / 'services' / 'module.py').write_text('value = 1')
            self.assertEqual(read_regular_file(root, 'services/module.py'), b'value = 1')
            self.assertIsNone(read_regular_file(root, 'services/new.py'))
            (root / 'services' / 'redirect.py').symlink_to(root / 'services' / 'module.py')
            with self.assertRaises(PlanBlocked):
                read_regular_file(root, 'services/redirect.py')

    def test_release_plan_git_blob_hash(self):
        from deploy.release_plan import git_blob_sha
        import hashlib
        data = b'print(1)\\n'
        expected = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\\0' + data).hexdigest()
        self.assertEqual(git_blob_sha(data), expected)


if __name__ == '__main__':
    unittest.main()
