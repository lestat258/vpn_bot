"""Referral navigation uses the same parent column as the deployed schema."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from referral import ReferralManager


class ReferralSchemaTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manager=ReferralManager()
        self.manager.db_path=str(Path(tmp.name)/"db.sqlite")
        with sqlite3.connect(self.manager.db_path) as db:
            db.executescript("""
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,referrer_id INTEGER DEFAULT 0);
                CREATE INDEX idx_users_referrer_id ON users(referrer_id);
                INSERT INTO users VALUES(1,0),(2,1),(3,2),(4,3),(5,4),(6,0),(7,NULL),(8,0);
            """)

    def parent(self,uid):
        with sqlite3.connect(self.manager.db_path) as db:
            return db.execute("SELECT referrer_id FROM users WHERE telegram_id=?",(uid,)).fetchone()[0]

    def test_three_levels_without_path_column(self):
        self.assertEqual(self.manager.get_referrals_by_level(1),{1:[2],2:[3],3:[4]})
        self.assertEqual(self.manager.get_referral_stats(1),
                         dict(total=3,level_1=1,level_2=1,level_3=1,bonus_days=50))

    def test_empty_statistics(self):
        self.assertEqual(self.manager.get_referral_stats(6),
                         dict(total=0,level_1=0,level_2=0,level_3=0,bonus_days=0))

    def test_chain_nearest_first(self):
        self.assertEqual(self.manager.get_referral_chain(4),[3,2,1])

    def test_register_and_preserve_existing_parent(self):
        self.assertTrue(self.manager.add_referral(6,2))
        self.assertFalse(self.manager.add_referral(6,1))
        self.assertEqual(self.parent(6),2)
        self.assertEqual(self.manager.get_referrals_by_level(1)[2],[3,6])

    def test_null_parent_can_register(self):
        self.assertTrue(self.manager.add_referral(7,1))
        self.assertEqual(self.parent(7),1)

    def test_nonexistent_and_self_links_are_rejected(self):
        for uid,ref in ((6,99),(99,1),(6,6),(6,0)):
            self.assertFalse(self.manager.add_referral(uid,ref))
        self.assertEqual(self.parent(6),0)

    def test_cycle_creation_rejected(self):
        self.assertFalse(self.manager.add_referral(1,5))
        self.assertEqual(self.parent(1),0)

    def test_corrupt_cycle_does_not_loop_or_count_self(self):
        with sqlite3.connect(self.manager.db_path) as db:
            db.execute("UPDATE users SET referrer_id=3 WHERE telegram_id=1")
        self.assertEqual(self.manager.get_referral_chain(1),[])
        self.assertEqual(self.manager.get_referrals_by_level(1),{1:[2],2:[3],3:[4]})
        self.assertFalse(self.manager.add_referral(8,1))

    def test_dangling_parent_is_not_valid_for_new_registration(self):
        with sqlite3.connect(self.manager.db_path) as db:
            db.execute("UPDATE users SET referrer_id=99 WHERE telegram_id=8")
        self.assertFalse(self.manager.add_referral(6,8))
        self.assertEqual(self.parent(6),0)
