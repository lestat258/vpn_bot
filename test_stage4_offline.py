#!/usr/bin/env python3
"""All tests offline; no calls to 3x-ui, YooKassa, Telegram, or FNS."""
import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from inbound_policy import (
    InboundPolicyError, ensure_schema, get_policy, save_policy, select_inbounds,
    select_for_server,
)

ROOT = Path(__file__).resolve().parent

INBOUNDS = [
    {'id': 83, 'tag': 'n11-in-443-tcp', 'enable': False},
    {'id': 84, 'tag': 'n11-in-8443-udp', 'enable': False},
    {'id': 87, 'tag': 'n11-in-18011-tcp', 'enable': False},
    {'id': 92, 'tag': 'n12-in-443-tcp--1', 'enable': True},
    {'id': 93, 'tag': 'n12-in-8443-udp', 'enable': True},
    {'id': 94, 'tag': 'in-14458-tcp', 'enable': True},
    {'id': 95, 'tag': 'n12-in-10443-tcp', 'enable': True},
    {'id': 98, 'tag': 'n13-in-443-tcp', 'enable': True},
    {'id': 100, 'tag': 'n14-in-443-tcp--1', 'enable': True},
]

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.db'
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT)')
            con.execute("INSERT INTO servers VALUES (16, 'Test')")
        ensure_schema(self.db)

    def test_unconfigured_fails_closed(self):
        self.assertIsNone(get_policy(self.db, 16))
        with self.assertRaises(InboundPolicyError):
            select_for_server(self.db, 16, INBOUNDS)

    def test_group_1_selects_exactly_expected_inbounds(self):
        p = {'mode': 'group', 'group_number': 1, 'minimum_count': 2}
        save_policy(self.db, 16, p)
        self.assertEqual(select_for_server(self.db, 16, INBOUNDS), [92, 100])

    def test_group_missing_member_fails(self):
        p = {'mode': 'group', 'group_number': 1, 'minimum_count': 2}
        with self.assertRaises(InboundPolicyError):
            select_inbounds(p, INBOUNDS[:-1])

    def test_manual_excludes_service_inbounds(self):
        p = {'mode': 'manual', 'inbound_ids': [92, 100], 'minimum_count': 2}
        save_policy(self.db, 16, p)
        self.assertEqual(select_for_server(self.db, 16, INBOUNDS), [92, 100])

    def test_manual_disabled_fails(self):
        p = {'mode': 'manual', 'inbound_ids': [83, 92], 'minimum_count': 2}
        with self.assertRaises(InboundPolicyError):
            select_inbounds(p, INBOUNDS)

    def test_all_requires_opt_in(self):
        with self.assertRaises(InboundPolicyError):
            select_inbounds({'mode':'all','minimum_count':1}, INBOUNDS)
        got = select_inbounds({'mode':'all','minimum_count':1,'allow_all':True}, INBOUNDS)
        self.assertEqual(got, [92,93,94,95,98,100])

    def test_group_multimarker_and_boundary(self):
        p = {'mode':'group','group_number':2,'minimum_count':1}
        got = select_inbounds(p, [
            {'id': 1, 'tag':'x--1--2','enable': True},
            {'id': 2, 'tag':'x--20','enable':True},
            {'id': 3, 'tag':'x--2bad','enable':True},
        ])
        self.assertEqual(got, [1])

    def test_all_empty_fails(self):
        with self.assertRaises(InboundPolicyError):
            select_inbounds({'mode':'all','minimum_count':1,'allow_all':True}, [])

    def test_sql_roundtrip_and_overwrite(self):
        save_policy(self.db, 16, {'mode':'manual', 'inbound_ids':[92], 'minimum_count':1})
        self.assertEqual(get_policy(self.db, 16)['inbound_ids'], [92])
        save_policy(self.db, 16, {'mode':'group', 'group_number':1, 'minimum_count':2})
        self.assertEqual(get_policy(self.db, 16)['mode'], 'group')

    def test_nonexistent_server_fails(self):
        with self.assertRaises(InboundPolicyError):
            save_policy(self.db, 999, {'mode':'group','group_number':1})

    def test_insecure_or_duplicate_ids_rejected(self):
        for xs in ([92, 92], ['not-id'], [True]):
            with self.subTest(xs=xs), self.assertRaises(InboundPolicyError):
                select_inbounds({'mode':'manual','inbound_ids':xs}, INBOUNDS)

    def test_routes_and_templates(self):
        tree = ast.parse((ROOT/'admin_modules/servers.py').read_text(encoding='utf-8'))
        funs = {n.name:n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,ast.AsyncFunctionDef))}
        for name in ('stage4_inbounds_page', 'stage4_save_inbounds'):
            code = ast.unparse(funs[name])
            self.assertIn('check_auth(', code)
            self.assertIn('inbound', code)
        post = ast.unparse(funs['stage4_save_inbounds'])
        self.assertIn('compare_digest', post)
        self.assertIn('save_policy', post)
        from jinja2 import Environment
        Environment(autoescape=True).parse((ROOT/'templates/server_inbounds.html').read_text(encoding='utf-8'))
        code = (ROOT/'services/xui_client.py').read_text(encoding='utf-8')
        self.assertIn('timeout=12', code)

if __name__ == '__main__':
    unittest.main(verbosity=2)
