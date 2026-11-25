import unittest
import json
from vpn_creation_guard import CreationPreflightError, gb_to_bytes, assert_absent_on_all_inbounds
from apply_stage7c import patch_source, PatchMismatch


# Schematic fixture mimics the audited 7B AST: never reaches network.
FIXTURE = '''import asyncio, sqlite3
# vpn4us stage4b:
# vpn4us stage4c:
# vpn4us stage5:
# vpn4us stage6a:
async def create_subscription():
    best_server = {'id': 16}
    xui = XUIClient()
    client_data = {
        "client": {
            "email": email,
            "totalGB": int(traffic_gb or 0),
            "expiryTime": expiry_timestamp,
        },
        "inboundIds": inbound_ids,
    }
    result = await asyncio.to_thread(xui._request, 'POST', '/panel/api/clients/add', client_data)
    if not result or not result.get('success'):
        logger.warning('fallback')
        result = await asyncio.to_thread(xui.add_client, inbound_ids[0], email, traffic_gb, expiry_timestamp, 3)
        if result and len(inbound_ids) > 1:
            await asyncio.to_thread(xui.attach_client_to_inbounds, email, inbound_ids[1:])
        if not result:
            return {'success': False}
        subid = result.get('subId')
        real_uuid = result.get('id')
    else:
        client_info = await asyncio.to_thread(xui._request, 'GET', f'/panel/api/clients/get/{email}')
        if client_info and client_info.get('success'):
            obj = client_info.get('obj', {})
            client = obj.get('client', {})
            subid = client.get('subId')
            real_uuid = client.get('uuid')
        else:
            raise RuntimeError('not verified')
    if not subid:
        raise RuntimeError('missing')
    logger.info(f"subId: {subid}")
    sub_url = build_subscription_link('https://example.com', subid)
    confirmed = await asyncio.to_thread(confirm_vless_client, xui, DB_PATH, best_server['id'], inbound_ids, email)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO subscriptions VALUES (?)', (email,))
'''

class GuardTests(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([], 'abc')
    def test_collision(self):
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([{'id': 92,'settings': {'clients':[{'email':'abc'}]}}], 'abc')
    def test_other_inbound_collision(self):
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([{'id':92,'settings':{'clients':[]}},{'id':95,'settings':{'clients':[{'email':'abc'}]}}], 'abc')
    def test_valid(self):
        self.assertTrue(assert_absent_on_all_inbounds([{'id':92, 'settings':json.dumps({'clients':[]})}], 'abc'))
    def test_broken_settings(self):
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([{'id':92, 'settings':'not-json'}], 'abc')
    def test_duplicate_inbound(self):
        with self.assertRaises(CreationPreflightError):
            assert_absent_on_all_inbounds([{'id':92,'settings':{'clients':[]}},{'id':92,'settings':{'clients':[]}}], 'abc')
    def test_gb_unlimited(self):
        self.assertEqual(gb_to_bytes(0),0)
        self.assertEqual(gb_to_bytes(None),0)
    def test_gb_whole(self):
        self.assertEqual(gb_to_bytes(10),10*(1024**3))
    def test_gb_fraction(self):
        self.assertEqual(gb_to_bytes(1.5),int(1.5*(1024**3)))
    def test_gb_negative(self):
        with self.assertRaises(CreationPreflightError):
            gb_to_bytes(-1)
    def test_gb_bad(self):
        for x in (True, 'not-number', 'NaN', 'Infinity'):
            with self.subTest(x=x), self.assertRaises(CreationPreflightError):
                gb_to_bytes(x)
    def test_patch_compiles_and_no_fallback(self):
        updated=patch_source(FIXTURE)
        compile(updated,'fixture','exec')
        self.assertNotIn('xui.add_client,',updated)
        self.assertNotIn('xui.attach_client_to_inbounds,',updated)
        self.assertIn('assert_absent_on_all_inbounds',updated)
        self.assertIn('gb_to_bytes(traffic_gb)',updated)
        self.assertIn('confirm_vless_client, xui',updated)
        self.assertNotIn('logger.info(f"subId: {subid}")',updated)
    def test_repeat_fails(self):
        with self.assertRaises(PatchMismatch):
            patch_source(patch_source(FIXTURE))
    def test_fallback_changed_fails(self):
        with self.assertRaises(PatchMismatch):
            patch_source(FIXTURE.replace('xui.attach_client_to_inbounds', 'xui.another_thing'))
    def test_different_traffic_fails(self):
        with self.assertRaises(PatchMismatch):
            patch_source(FIXTURE.replace('int(traffic_gb or 0)', 'traffic_gb'))

if __name__=='__main__':
    unittest.main()
