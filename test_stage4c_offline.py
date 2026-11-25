import json
import unittest
from unittest.mock import patch
import uuid
from vpn_client_verify import inspect_vless_client, confirm_vless_client, VerificationError
from apply_stage4c import patch_source, PatchMismatch

U = str(uuid.uuid4())
SID = 'a1b2c3d4e5'
EMAIL = 'test_new_user_001'
EXPIRES = 1800000000000


def make_inbound(iid, *, has=True, uid=U, subid=SID, enabled=True,
                 expiry=EXPIRES, protocol='vless', json_settings=True):
    clients = [{'email': EMAIL, 'id': uid, 'subId': subid,
                'enable': enabled, 'expiryTime': expiry}] if has else []
    settings = {'clients': clients}
    return {'id': iid, 'enable': True, 'protocol': protocol,
            'settings': json.dumps(settings) if json_settings else settings}


def snapshot():
    return [make_inbound(92), make_inbound(100), make_inbound(95,has=False)]


class VerifyTests(unittest.TestCase):
    def test_success(self):
        result = inspect_vless_client(snapshot(), [92,100], EMAIL,
                                    expected_uuid=U,expected_subid=SID,
                                    minimum_expiry_ms=EXPIRES)
        self.assertEqual(result['subid'], SID)
        self.assertEqual(result['uuid'], U)

    def test_missing_selected(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92), make_inbound(100,has=False)], [92,100], EMAIL)

    def test_service_inbound(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100),make_inbound(95)], [92,100], EMAIL)

    def test_uuid_mismatch(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100,uid=str(uuid.uuid4()))], [92,100], EMAIL)

    def test_subid_mismatch(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100,subid='other')], [92,100], EMAIL)

    def test_disabled(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100,enabled=False)], [92,100], EMAIL)

    def test_expiry(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100,expiry=EXPIRES-120000)], [92,100], EMAIL,
                                minimum_expiry_ms=EXPIRES)

    def test_wrong_subid_from_creation(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client(snapshot(), [92,100], EMAIL, expected_subid='madeup')

    def test_non_vless_fails_closed(self):
        with self.assertRaises(VerificationError):
            inspect_vless_client([make_inbound(92),make_inbound(100,protocol='trojan')], [92,100], EMAIL)

    def test_duplicate_email(self):
        data = snapshot()
        recs = json.loads(data[0]['settings'])
        recs['clients'].append(recs['clients'][0].copy())
        data[0]['settings'] = json.dumps(recs)
        with self.assertRaises(VerificationError):
            inspect_vless_client(data, [92,100], EMAIL)

    def test_no_network_with_mock(self):
        class FakeXui:
            def get_inbounds(self):
                return snapshot()
        with patch('vpn_client_verify.select_for_server', return_value=[92,100]) as f:
            out = confirm_vless_client(FakeXui(), '/unused/test.db',16,[92,100],EMAIL, expected_uuid=U)
            self.assertEqual(out['uuid'],U)
            f.assert_called_once()

    def test_policy_changed_denies(self):
        class FakeXui:
            def get_inbounds(self):
                return snapshot()
        with patch('vpn_client_verify.select_for_server', return_value=[92]):
            with self.assertRaises(VerificationError):
                confirm_vless_client(FakeXui(),'/unused/test.db',16,[92,100],EMAIL)


class PatchTests(unittest.TestCase):
    def fixture(self):
        return '''import asyncio
import sqlite3
# vpn4us stage4b: configured inbound policy (fail closed)
class VPNService:
    @staticmethod
    async def create_subscription():
        try:
            xui = object()
            inbound_ids = [92,100]
            email = 'example'
            real_uuid = 'abc'
            subid = 'def'
            best_server = {'id':16}
            DB_PATH = ':memory:'
            expiry_timestamp = 1800000000000
            sub_url = 'https://example.invalid/'
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('INSERT INTO subscriptions (telegram_id) VALUES (?)', (1,))
            conn.commit()
            conn.close()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}
'''

    def test_patch_is_before_db_insert(self):
        result, lineno = patch_source(self.fixture())
        self.assertLess(result.find('confirm_vless_client'), result.find('sqlite3.connect(DB_PATH)'))
        self.assertIn('expected_subid=subid', result)

    def test_patch_repeat_denied(self):
        updated, _ = patch_source(self.fixture())
        with self.assertRaises(PatchMismatch):
            patch_source(updated)


if __name__ == '__main__':
    unittest.main()
