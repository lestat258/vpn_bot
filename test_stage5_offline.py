import unittest
from subscription_links import validate_subscription_base, build_subscription_link
from apply_stage5 import patch_source, PatchMismatch

SAMPLE='''import asyncio
import sqlite3
# vpn4us stage4b: configured inbound policy (fail closed)
class VPNService:
    @staticmethod
    async def create_subscription():
        best_server={'id':16, 'url':'https://example.invalid'}
        xui = XUIClient(url=best_server['url'], api_token='test')
        subid = 'abcdefgh_123'
        domain_part = 'https://example.invalid/'
        sub_path = '/sub/'
        sub_url = f'{domain_part}{sub_path}{subid}'
        # vpn4us stage4c: verify actual client on every selected inbound
        conn = sqlite3.connect(DB_PATH)
        c=conn.cursor()
        c.execute('INSERT INTO subscriptions (telegram_id) VALUES (?)',(1,))
'''

class LinkTests(unittest.TestCase):
    def test_template(self):
        self.assertEqual(build_subscription_link('https://a.test/sub/{subid}', 'aBc-1_2'), 'https://a.test/sub/aBc-1_2')
    def test_prefix(self):
        self.assertEqual(build_subscription_link('https://a.test:2096/sub/', 'A_12345'), 'https://a.test:2096/sub/A_12345')
    def test_legacy_blank_not_valid_base(self):
        with self.assertRaises(ValueError):
            validate_subscription_base('')
    def test_reject_ambiguous_prefix(self):
        with self.assertRaises(ValueError):
            validate_subscription_base('https://a.test/sub')
    def test_reject_insecure(self):
        with self.assertRaises(ValueError):
            validate_subscription_base('http://a.test/sub/')
    def test_reject_query_credentials(self):
        for url in ['https://a.test/sub/?id=1','https://a.test/sub/#x','https://u:p@a.test/sub/']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                validate_subscription_base(url)
    def test_reject_subid_injection(self):
        for subid in ['', '../secret', 'a/b', 'x?y', ' a123']:
            with self.subTest(subid=subid),self.assertRaises(ValueError):
                build_subscription_link('https://a.test/sub/',subid)
    def test_reject_two_templates(self):
        with self.assertRaises(ValueError):
            validate_subscription_base('https://a.test/{subid}/{subid}')

class PatchTests(unittest.TestCase):
    def test_patch_compiles_and_is_ordered(self):
        result=patch_source(SAMPLE)
        self.assertLess(result.index('validate_subscription_base(_stage5_base)'),result.index('xui = XUIClient'))
        self.assertIn('sub_url = build_subscription_link(_stage5_base, subid)',result)
        self.assertLess(result.index('build_subscription_link(_stage5_base, subid)'),result.index('INSERT INTO subscriptions'))
        compile(result,'test_fixture','exec')
    def test_patch_not_repeatable(self):
        with self.assertRaises(PatchMismatch):
            patch_source(patch_source(SAMPLE))

if __name__=='__main__':
    unittest.main()
