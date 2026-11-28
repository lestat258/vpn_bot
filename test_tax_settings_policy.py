import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from tax_settings_policy import save, status
from tax_fulfillment import credentials


class FiscalSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=str(Path(tmp.name)/"test.db")
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO settings VALUES('tax_enabled','on'),
                    ('tax_description_template','Synthetic service');
                CREATE TABLE payment_processing(payment_id TEXT,tax_state TEXT);
                INSERT INTO payment_processing VALUES('old','skipped');
            """)
            db.execute("INSERT INTO settings VALUES('encryption_key',?)",
                       (base64.urlsafe_b64encode(Fernet.generate_key()).decode(),))

    def values(self):
        with sqlite3.connect(self.path) as db:
            return dict(db.execute("SELECT key,value FROM settings"))

    def form(self, **kw):
        return dict({"tax_enabled":"true","tax_inn":"123456789012",
                     "tax_password":"synthetic-password"},**kw)

    def test_legacy_flag_is_read_only_and_disabled(self):
        old=self.values()
        self.assertEqual(status(self.path),{"enabled":False,"legacy_flag":True,"credentials_ready":False})
        self.assertEqual(self.values(),old)

    def test_unchecked_saves_false(self):
        self.assertFalse(save(self.path,{}))
        self.assertEqual(self.values()["tax_enabled"],"false")

    def test_enable_without_credentials_rejected_atomically(self):
        old=self.values()
        with self.assertRaises(ValueError):
            save(self.path,{"tax_enabled":"on"})
        self.assertEqual(self.values(),old)

    def test_encrypt_and_roundtrip_with_fiscal_worker(self):
        self.assertTrue(save(self.path,self.form()))
        self.assertNotEqual(self.values()["tax_password"],"synthetic-password")
        self.assertEqual(credentials(self.path),("123456789012","synthetic-password"))
        self.assertTrue(status(self.path)["credentials_ready"])

    def test_blank_password_preserves_ciphertext(self):
        save(self.path,self.form())
        previous=self.values()["tax_password"]
        self.assertFalse(save(self.path,{"tax_password":""}))
        self.assertEqual(self.values()["tax_password"],previous)
        self.assertTrue(save(self.path,{"tax_enabled":"true","tax_password":""}))

    def test_toggle_guards_activation_and_disables(self):
        with self.assertRaises(ValueError):
            save(self.path,{},toggle=True)
        save(self.path,self.form())
        self.assertFalse(save(self.path,{},toggle=True))
        self.assertTrue(save(self.path,{},toggle=True))

    def test_bad_key_and_invalid_fields_leave_configuration_unchanged(self):
        for form in (self.form(tax_inn="invalid"),self.form(tax_description_template=""),self.form(tax_enabled="bad")):
            old=self.values()
            with self.assertRaises(ValueError):
                save(self.path,form)
            self.assertEqual(self.values(),old)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE settings SET value='broken' WHERE key='encryption_key'")
        old=self.values()
        with self.assertRaises(ValueError):
            save(self.path,self.form())
        self.assertEqual(self.values(),old)

    def test_history_not_requeued_on_enable(self):
        save(self.path,self.form())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM payment_processing").fetchall(),[("old","skipped")])
