"""Offline tests. Never loads VPNService or calls live server APIs."""
import asyncio
import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from apply_stage4b import patch_source, PatchMismatch
from inbound_policy import ensure_schema, save_policy, InboundPolicyError

SAMPLE = '''\nclass VPNService:\n    @staticmethod\n    async def create_subscription(inbounds):\n        best_server = {"id": 16}\n        inbound_ids = []\n        for inbound in inbounds:\n            if inbound.get('enable', True):\n                inbound_id = inbound.get('id')\n                if inbound_id:\n                    inbound_ids.append(inbound_id)\n        if not inbound_ids:\n            return None\n        return inbound_ids\n'''

ROWS = [
    {'id': 92, 'tag': 'n12-in-443-tcp--1', 'enable': True},
    {'id': 93, 'tag': 'n12-in-8443-udp', 'enable': True},
    {'id': 95, 'tag': 'n12-in-10443-tcp', 'enable': True},
    {'id': 98, 'tag': 'n13-in-443-tcp', 'enable': True},
    {'id': 100, 'tag': 'n14-in-443-tcp--1', 'enable': True},
]

class PatchTests(unittest.TestCase):
    def test_patch_and_compilation(self):
        patched, bounds = patch_source(SAMPLE)
        self.assertIn('select_for_server(DB_PATH, best_server["id"], inbounds)', patched)
        self.assertNotIn('inbound_ids.append(', patched)
        ast.parse(patched)
        self.assertEqual(bounds[0] < bounds[1], True)

    def test_repeat_application_denied(self):
        with self.assertRaises(PatchMismatch):
            patch_source(patch_source(SAMPLE)[0])

    def test_unexpected_old_loop_denied(self):
        with self.assertRaises(PatchMismatch):
            patch_source(SAMPLE.replace('inbound_ids.append(inbound_id)', 'inbound_ids.extend([inbound_id])'))

    def test_policy_integrated_into_method(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'db.sqlite'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('CREATE TABLE servers(id INTEGER PRIMARY KEY)')
                con.execute('INSERT INTO servers VALUES(16)')
            ensure_schema(db)
            save_policy(db, 16, {'mode':'group', 'group_number':1, 'minimum_count':2})
            ns = {'DB_PATH': str(db)}
            exec(compile(patch_source(SAMPLE)[0], '<synthetic>', 'exec'), ns)
            got = asyncio.run(ns['VPNService'].create_subscription(ROWS))
            self.assertEqual(got, [92,100])
            with self.assertRaises(InboundPolicyError):
                asyncio.run(ns['VPNService'].create_subscription(ROWS[:-1]))

    def test_no_policy_denies(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'db.sqlite'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('CREATE TABLE servers(id INTEGER PRIMARY KEY)')
                con.execute('INSERT INTO servers VALUES(16)')
            ensure_schema(db)
            ns = {'DB_PATH': str(db)}
            exec(compile(patch_source(SAMPLE)[0], '<synthetic>', 'exec'), ns)
            with self.assertRaises(InboundPolicyError):
                asyncio.run(ns['VPNService'].create_subscription(ROWS))

if __name__ == '__main__':
    unittest.main(verbosity=2)
