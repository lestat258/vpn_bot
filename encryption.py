"""Fernet credentials, compatible with the existing double-base64 format.

Plain legacy values are accepted explicitly. Importing this module never
creates or rewrites credentials. A missing key is initialized only when
encryption is needed and no recognizable encrypted credentials exist.
"""
import base64
import binascii
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _as_bytes(value):
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes):
        return value
    raise TypeError("Credential must be str or bytes")


def _decode(value):
    return base64.b64decode(value, altchars=b"-_", validate=True)


def _encrypted_candidate(value):
    if not value or not isinstance(value, (str, bytes)):
        return False
    raw = _as_bytes(value)
    if raw.startswith((b"gAAAA", b"Z0FBQU")):
        return True
    try:
        return _decode(raw).startswith(b"gAAAA")
    except (ValueError, binascii.Error):
        return False


def unwrap_token(value):
    """Return Fernet token, None for legacy plain text, or fail on malformed marker."""
    if not _encrypted_candidate(value):
        return None
    raw = _as_bytes(value)
    if raw.startswith(b"gAAAA"):
        return raw
    try:
        token = _decode(raw)
    except (ValueError, binascii.Error):
        raise InvalidToken("Malformed encrypted credential") from None
    if not token.startswith(b"gAAAA"):
        raise InvalidToken("Malformed encrypted credential")
    return token


def cipher_from_stored_key(value):
    try:
        return Fernet(_decode(_as_bytes(value)))
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("Invalid stored encryption key; restore original key") from None


class EncryptionManager:
    def __init__(self, path=None):
        self.path = path
        self.cipher = None
        self.key = None
        self._lock = threading.Lock()

    def _db_path(self):
        if self.path is not None:
            return self.path
        from runtime_config import DB_PATH
        return DB_PATH

    def _connect(self):
        return sqlite3.connect(
            Path(self._db_path()).resolve().as_uri() + "?mode=rw",
            uri=True, timeout=10
        )

    @staticmethod
    def _existing_ciphertext(db):
        for (value,) in db.execute("SELECT value FROM settings WHERE key!='encryption_key'"):
            if _encrypted_candidate(value):
                return True
        for table, column in (("servers", "api_token"), ("users", "password")):
            columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column in columns:
                for (value,) in db.execute(f"SELECT {column} FROM {table}"):
                    if _encrypted_candidate(value):
                        return True
        return False

    def _load_key(self, *, allow_create=False):
        with self._lock:
            if self.cipher is not None:
                return self.cipher
            with closing(self._connect()) as db:
                row = db.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()
                if row is None:
                    if not allow_create:
                        raise ValueError("Encryption key missing; restore original key")
                    # Serialize first initialization; never overwrite an existing row.
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()
                        if row is None:
                            if self._existing_ciphertext(db):
                                raise ValueError("Encrypted credentials exist; restore original key")
                            key = Fernet.generate_key()
                            stored = base64.urlsafe_b64encode(key).decode("ascii")
                            db.execute("INSERT INTO settings(key,value) VALUES('encryption_key',?)", (stored,))
                            row = (stored,)
                cipher = cipher_from_stored_key(row[0])
                self.key = _decode(_as_bytes(row[0]))
                self.cipher = cipher
                return cipher

    def load_or_generate_key(self):
        self._load_key(allow_create=True)

    def generate_key(self):
        # Compatibility entry point: no implicit rotation.
        self._load_key(allow_create=True)
        return self.key

    def encrypt(self, data):
        if not data:
            return data
        raw = _as_bytes(data)
        cipher = self._load_key(allow_create=True)
        return base64.urlsafe_b64encode(cipher.encrypt(raw)).decode("ascii")

    def decrypt(self, encrypted_data):
        if not encrypted_data:
            return encrypted_data
        token = unwrap_token(encrypted_data)
        if token is None:
            return encrypted_data
        return self._load_key().decrypt(token).decode("utf-8")

    def is_encrypted(self, data):
        # A recognized marker is not proof of authenticity. Do not migrate
        # damaged ciphertext as though it were a plaintext credential.
        return _encrypted_candidate(data)

    def migrate_old_data(self):
        """Explicit legacy server-token migration; never invoked on import."""
        cipher = self._load_key(allow_create=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count = 0
            for server_id, token in db.execute(
                "SELECT id,api_token FROM servers WHERE api_token IS NOT NULL AND api_token!=''"
            ).fetchall():
                existing = unwrap_token(token)
                if existing is not None:
                    cipher.decrypt(existing)
                    continue
                encrypted = base64.urlsafe_b64encode(cipher.encrypt(_as_bytes(token))).decode("ascii")
                db.execute("UPDATE servers SET api_token=? WHERE id=?", (encrypted, server_id))
                count += 1
            return count


encryption_manager = EncryptionManager()


def encrypt(data):
    return encryption_manager.encrypt(data)


def decrypt(data):
    return encryption_manager.decrypt(data)
