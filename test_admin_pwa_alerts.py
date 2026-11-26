import asyncio
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from admin_pwa import icon_png, manifest, SIZES
from tax_fulfillment import auto_receipt_enabled
from payment_admin_alerts import drain_alerts


class PwaTests(unittest.TestCase):
    def test_icons(self):
        for size in SIZES:
            data = icon_png(size)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II",data[16:24]),(size,size))
        with self.assertRaises(ValueError):
            icon_png(1000)

    def test_manifest(self):
        import json
        m=json.loads(manifest().body)
        self.assertEqual(m["start_url"],"/dashboard")
        self.assertEqual(m["display"],"standalone")
        self.assertEqual({i["sizes"] for i in m["icons"]},{"192x192","512x512"})


class AlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name)/"data.db")
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO settings VALUES ('admin_id','12345');
                CREATE TABLE payments(id INTEGER PRIMARY KEY,payment_id TEXT,created_at TEXT,amount_rub REAL);
                CREATE TABLE payment_processing(payment_id TEXT,is_legacy INTEGER,payment_state TEXT,
                  vpn_state TEXT,tax_state TEXT,refund_state TEXT,updated_at TEXT);
                INSERT INTO payments VALUES (7,'old','2026-11-26 12:20:00',1),
                    (8,'new','2026-11-26 12:20:00',1),(9,'ok','2026-11-26 12:20:00',1),
                    (10,'free','2026-11-26 12:20:00',0),(11,'past','2026-11-26 12:00:00',1);
                INSERT INTO payment_processing VALUES
                    ('old',0,'verified','needs_review','needs_review','full_review',CURRENT_TIMESTAMP),
                    ('new',0,'verified','needs_review','needs_review','full_review',CURRENT_TIMESTAMP),
                    ('ok',0,'verified','done','done','none',CURRENT_TIMESTAMP),
                    ('free',0,'verified','needs_review','needs_review','full_review',CURRENT_TIMESTAMP),
                    ('past',0,'verified','needs_review','needs_review','full_review',CURRENT_TIMESTAMP);
            """)

    def test_receipt_switch(self):
        self.assertTrue(auto_receipt_enabled(self.path))
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO settings VALUES ('auto_receipt_delivery','off')")
        self.assertFalse(auto_receipt_enabled(self.path))

    async def test_alerts_deduplicated(self):
        sender=AsyncMock()
        await drain_alerts(self.path,sender=sender)
        await drain_alerts(self.path,sender=sender)
        self.assertEqual(sender.await_count,3)
        for call in sender.await_args_list:
            self.assertEqual(call.args[0],12345)
            self.assertIn("№8",call.args[1])

    async def test_failed_alert_not_replayed(self):
        sender=AsyncMock(side_effect=TimeoutError())
        await drain_alerts(self.path,sender=sender)
        await drain_alerts(self.path,sender=sender)
        self.assertEqual(sender.await_count,3)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT DISTINCT state FROM payment_admin_alerts").fetchall(),[("unconfirmed",)])

    async def test_no_recipient_no_delivery(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM settings WHERE key='admin_id'")
        sender=AsyncMock()
        await drain_alerts(self.path,sender=sender)
        sender.assert_not_awaited()
