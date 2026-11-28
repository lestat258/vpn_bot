import base64
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet, InvalidToken
from encryption import EncryptionManager


class CredentialFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "test.db")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("CREATE TABLE servers(id INTEGER PRIMARY KEY,api_token TEXT)")
        self.manager = EncryptionManager(self.path)

    def setting(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()

    def install_key(self, key):
        stored = base64.urlsafe_b64encode(key).decode()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO settings VALUES('encryption_key',?)", (stored,))
        return stored

    def test_plaintext_and_base64_are_not_ciphertext(self):
        for value in ("plain-credential!", "dGVzdA==", "synthetic:" + "x"*45, "", None):
            self.assertEqual(self.manager.decrypt(value), value)
            self.assertFalse(self.manager.is_encrypted(value))
        self.assertIsNone(self.setting())

    def test_legacy_wrapped_and_native_tokens(self):
        key = Fernet.generate_key()
        stored = self.install_key(key)
        native = Fernet(key).encrypt(b"synthetic")
        wrapped = base64.urlsafe_b64encode(native).decode()
        for value in (native.decode(), wrapped):
            self.assertTrue(self.manager.is_encrypted(value))
            self.assertEqual(self.manager.decrypt(value), "synthetic")
        new = self.manager.encrypt("another")
        self.assertEqual(Fernet(key).decrypt(base64.urlsafe_b64decode(new)), b"another")
        self.assertEqual(self.setting(), (stored,))

    def test_invalid_key_never_replaced(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO settings VALUES('encryption_key','broken')")
        with self.assertRaises(ValueError):
            self.manager.encrypt("synthetic")
        self.assertEqual(self.setting(), ("broken",))

    def test_missing_key_with_existing_ciphertext(self):
        token = base64.urlsafe_b64encode(Fernet(Fernet.generate_key()).encrypt(b"synthetic")).decode()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO servers VALUES(1,?)", (token,))
        with self.assertRaises(ValueError):
            self.manager.decrypt(token)
        with self.assertRaises(ValueError):
            self.manager.encrypt("new")
        self.assertIsNone(self.setting())

    def test_wrong_key_and_malformed_token(self):
        stored = self.install_key(Fernet.generate_key())
        token = base64.urlsafe_b64encode(Fernet(Fernet.generate_key()).encrypt(b"synthetic")).decode()
        for value in (token, "gAAAA-invalid", "Z0FBQU!"):
            self.assertTrue(self.manager.is_encrypted(value))
            with self.assertRaises(InvalidToken):
                self.manager.decrypt(value)
        self.assertEqual(self.setting(), (stored,))

    def test_concurrent_initialization(self):
        def create(i):
            return EncryptionManager(self.path).encrypt(str(i))
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(create, range(8)))
        self.assertEqual([self.manager.decrypt(x) for x in values], list(map(str, range(8))))

    def test_no_implicit_migration(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO servers VALUES(1,'plain-token')")
        self.manager.encrypt("new")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT api_token FROM servers").fetchone(), ("plain-token",))
        self.assertEqual(self.manager.migrate_old_data(), 1)
        self.assertEqual(self.manager.migrate_old_data(), 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(self.manager.decrypt(db.execute("SELECT api_token FROM servers").fetchone()[0]), "plain-token")
