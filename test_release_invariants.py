"""Offline VPN issuance safety regressions. No real server/DB/network calls."""
import ast
import json
import sqlite3
import tempfile
import unittest
# Included in the pinned regression workflow without changing its reviewed YAML.
from test_ui_wiring import TelegramUiWiring
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
        expected = hashlib.sha1(b'blob ' + str(len(data)).encode() + bytes([0]) + data).hexdigest()
        self.assertEqual(git_blob_sha(data), expected)



class MultiFileDeploymentSandboxTests(unittest.TestCase):
    """Exercise deployment on throwaway temporary folders only."""

    def setUp(self):
        import os
        self.owner = os.getuid()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prod = self.base / 'production'
        self.folder = self.base / 'backup'
        (self.prod / 'services').mkdir(parents=True)
        self.folder.mkdir()

    def target(self, filename='services/vpn_service.py'):
        return self.prod / filename

    def prepare(self, current=b'OLD SOURCE', updated=b'NEW SOURCE'):
        from deploy.release_apply import make_plan, write_backups
        self.target().write_bytes(current)
        files = {'services/vpn_service.py': updated}
        plan, fingerprint, same = make_plan('a' * 40, files, self.prod, owner=self.owner)
        self.assertEqual(same, 0)
        self.assertEqual(len(plan['changes']), 1)
        write_backups(self.folder, plan['changes'], self.prod, owner=self.owner)
        return plan, files, fingerprint

    def test_multi_replace_backup_and_rollback(self):
        from deploy.release_apply import apply_to_root, rollback_root
        plan, files, fp = self.prepare()
        self.assertEqual((self.folder / 'services/vpn_service.py').read_bytes(), b'OLD SOURCE')
        apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'NEW SOURCE')
        rollback_root(self.prod, plan['changes'], self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'OLD SOURCE')

    def test_two_existing_files_are_updated_and_restored(self):
        from deploy.release_apply import make_plan, write_backups, apply_to_root, rollback_root
        self.target().write_bytes(b'OLD A')
        other_name = 'services/second.py'
        self.target(other_name).write_bytes(b'OLD B')
        files = {'services/vpn_service.py': b'NEW A', other_name: b'NEW B'}
        plan, _, _ = make_plan('c' * 40, files, self.prod, owner=self.owner)
        self.assertEqual(len(plan['changes']), 2)
        write_backups(self.folder, plan['changes'], self.prod, owner=self.owner)
        apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'NEW A')
        self.assertEqual(self.target(other_name).read_bytes(), b'NEW B')
        rollback_root(self.prod, plan['changes'], self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'OLD A')
        self.assertEqual(self.target(other_name).read_bytes(), b'OLD B')

    def test_new_file_is_removed_on_rollback(self):
        from deploy.release_apply import make_plan, write_backups, apply_to_root, rollback_root
        name = 'services/new_module.py'
        files = {name: b'new module'}
        plan, _, _ = make_plan('b' * 40, files, self.prod, owner=self.owner)
        self.assertEqual(plan['changes'][0]['action'], 'new')
        write_backups(self.folder, plan['changes'], self.prod, owner=self.owner)
        apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.assertEqual(self.target(name).read_bytes(), b'new module')
        rollback_root(self.prod, plan['changes'], self.folder, owner=self.owner)
        self.assertFalse(self.target(name).exists())

    def test_reject_external_modification_before_apply(self):
        from deploy.release_apply import apply_to_root, Blocked
        plan, files, _ = self.prepare()
        self.target().write_bytes(b'OTHER CHANGE')
        with self.assertRaises(Blocked):
            apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'OTHER CHANGE')

    def test_reject_external_modification_before_rollback(self):
        from deploy.release_apply import apply_to_root, rollback_root, Blocked
        plan, files, _ = self.prepare()
        apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.target().write_bytes(b'CHANGED AFTER DEPLOYMENT')
        with self.assertRaises(Blocked):
            rollback_root(self.prod, plan['changes'], self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'CHANGED AFTER DEPLOYMENT')

    def test_no_partial_update_on_preflight_mismatch(self):
        from deploy.release_apply import make_plan, apply_to_root, Blocked
        self.target().write_bytes(b'A')
        other = self.target('services/second.py')
        other.write_bytes(b'B')
        files = {'services/vpn_service.py': b'AA', 'services/second.py': b'BB'}
        plan, _, _ = make_plan('a' * 40, files, self.prod, owner=self.owner)
        other.write_bytes(b'C')
        with self.assertRaises(Blocked):
            apply_to_root(self.prod, plan['changes'], files, self.folder, owner=self.owner)
        self.assertEqual(self.target().read_bytes(), b'A')
        self.assertEqual(other.read_bytes(), b'C')

    def test_existing_symlink_rejected(self):
        from deploy.release_apply import make_plan, Blocked
        self.target().symlink_to(self.base / 'arbitrary')
        with self.assertRaises(Blocked):
            make_plan('a' * 40, {'services/vpn_service.py': b'value'},
                      self.prod, owner=self.owner)

    def test_prohibited_paths(self):
        from deploy.release_apply import allow_path
        from deploy.release_plan import allowed_source
        for name in ('.env', 'data.db', 'secrets/key.pem', 'static/pwd.key',
                     'deploy/release_apply.py', 'test_release_invariants.py',
                     '/etc/passwd', 'services/../data.db', 'services\\\\x.py',
                     '.github/workflows/source-checks.yml'):
            with self.subTest(name=name):
                self.assertFalse(allow_path(name))
                self.assertFalse(allowed_source(name))
        for name in ('bot.py', 'admin_modules/servers.py', 'handlers/start.py',
                     'templates/edit_server_safe.html', 'static/app.js'):
            with self.subTest(name=name):
                self.assertTrue(allow_path(name))
                self.assertTrue(allowed_source(name))

    def test_plan_fingerprint_matches_read_only_planner(self):
        from deploy.release_apply import make_plan
        from deploy.release_plan import digest
        import json
        self.target().write_bytes(b'OLD SOURCE')
        files = {'services/vpn_service.py': b'NEW SOURCE'}
        plan, fp, _ = make_plan('a' * 40, files, self.prod, owner=self.owner)
        self.assertEqual(fp, digest(json.dumps(plan, sort_keys=True,
                             separators=(',', ':'), ensure_ascii=True).encode()))

    def test_manifest_is_root_private_in_real_deployment(self):
        from deploy.release_apply import write_manifest
        manifest = {'schema': 1, 'status': 'prepared', 'changes': []}
        write_manifest(self.folder, manifest)
        self.assertEqual(json.loads((self.folder / 'manifest.json').read_text()), manifest)
        self.assertEqual((self.folder / 'manifest.json').stat().st_mode & 0o777, 0o600)

    def test_git_blob_verifier(self):
        from deploy.release_apply import git_sha
        from deploy.release_plan import git_blob_sha
        sample = b'unchanged source'
        self.assertEqual(git_sha(sample), git_blob_sha(sample))


if __name__ == '__main__':
    unittest.main()
