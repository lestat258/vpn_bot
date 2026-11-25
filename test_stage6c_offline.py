"""Stage 6C offline tests: no real panel or database access."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from sync_api_preflight import (PreflightError, inspect_openapi,
    validate_panel_url, normalize_path, owned_scope_status, load_servers)


class RouteTests(unittest.TestCase):
    def test_supported_api_routes(self):
        spec = {'paths': {
            '/panel/api/clients/get/{email}': {'get': {}},
            '/panel/api/clients/{email}/attach': {'post': {}},
            '/panel/api/clients/{email}/detach': {'post': {}},
        }}
        self.assertTrue(all(inspect_openapi(spec).values()))

    def test_colon_style_openapi(self):
        spec = {'paths': {
            '/panel/api/clients/get/:email': {'get': {}},
            '/panel/api/clients/:email/attach': {'post': {}},
            '/panel/api/clients/:email/detach': {'post': {}},
        }}
        self.assertTrue(all(inspect_openapi(spec).values()))

    def test_missing_detach_denied(self):
        paths = {'/panel/api/clients/get/{email}': {'get': {}},
                 '/panel/api/clients/{email}/attach': {'post': {}}}
        supported = inspect_openapi({'paths': paths})
        self.assertFalse(supported['detach inbounds'])

    def test_post_does_not_count_for_get(self):
        paths = {'/panel/api/clients/get/{email}': {'post': {}}}
        supported = inspect_openapi({'paths': paths})
        self.assertFalse(supported['fetch client'])

    def test_malformed_openapi_denied(self):
        for spec in ({}, {'paths': []}, None, {'paths': {'oops': []}}):
            if spec == {'paths': {'oops': []}}:
                self.assertFalse(all(inspect_openapi(spec).values()))
            else:
                with self.assertRaises(PreflightError):
                    inspect_openapi(spec)

    def test_https_only(self):
        for url in ('http://example.invalid', 'https://user:pass@example.invalid',
                    'https://example.invalid/?token=abc', 'example.invalid'):
            with self.assertRaises(PreflightError):
                validate_panel_url(url)

    def test_https_panel_subpath(self):
        self.assertEqual(validate_panel_url('https://example.invalid/panel-root/'),
                         'https://example.invalid/panel-root/panel/api/openapi.json')

    def test_invalid_openapi_path(self):
        with self.assertRaises(PreflightError):
            normalize_path('panel/api/clients')


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE servers (id INTEGER PRIMARY KEY, url TEXT,
                api_token TEXT, is_active INTEGER);
                CREATE TABLE server_inbound_policies (server_id INTEGER);
                CREATE TABLE subscriptions (id INTEGER PRIMARY KEY,
                xui_email TEXT, xui_client_uid TEXT, xui_subid TEXT,
                managed_by_vpn4us INTEGER, server_id INTEGER, is_active INTEGER);
                CREATE TABLE subscription_inbound_memberships
                (subscription_id INTEGER, server_id INTEGER, inbound_id INTEGER);
                INSERT INTO servers VALUES(16,'https://example.invalid','placeholder',1);
                INSERT INTO server_inbound_policies VALUES(16);
            ''')

    def test_empty_owned_inventory(self):
        self.assertEqual(owned_scope_status(self.path, 16),
            {'owned':0, 'incomplete':0, 'duplicate':0, 'missing_memberships':0})

    def test_owned_and_foreign_separation(self):
        with sqlite3.connect(self.path) as db:
            db.execute('''INSERT INTO subscriptions VALUES
                (1,'other@invalid','uid','subid',0,16,1)''')
            db.execute('''INSERT INTO subscriptions VALUES
                (2,'mine@invalid','uid','subid',1,16,1)''')
            db.execute('''INSERT INTO subscription_inbound_memberships VALUES(2,16,92)''')
        self.assertEqual(owned_scope_status(self.path,16)['owned'],1)
        self.assertEqual(owned_scope_status(self.path,16)['missing_memberships'],0)

    def test_owned_duplicates_detected(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO subscriptions VALUES(1,'same@invalid','uid','subid',1,16,0)")
            db.execute("INSERT INTO subscriptions VALUES(2,'same@invalid','uid','subid',1,16,0)")
        self.assertEqual(owned_scope_status(self.path,16)['duplicate'],1)

    def test_owned_active_missing_memberships(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO subscriptions VALUES(1,'mine@invalid','uid','subid',1,16,1)")
        self.assertEqual(owned_scope_status(self.path,16)['missing_memberships'],1)

    def test_owned_incomplete_identity(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO subscriptions VALUES(1,'','uid','subid',1,16,0)")
        self.assertEqual(owned_scope_status(self.path,16)['incomplete'],1)

    def test_server_selection_readonly(self):
        self.assertEqual([r[0] for r in load_servers(self.path,None)], [16])
        self.assertEqual([r[0] for r in load_servers(self.path,16)], [16])
        self.assertEqual(load_servers(self.path,17), [])

if __name__ == '__main__':
    unittest.main()
