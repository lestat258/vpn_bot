import sqlite3
import tempfile
import unittest
from pathlib import Path
from payment_admin_view import load_journal, safe_receipt_link

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"data.db"
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE payments(id INTEGER PRIMARY KEY,payment_id TEXT UNIQUE,telegram_id INTEGER,tariff_id INTEGER,amount_rub REAL,status TEXT,created_at TEXT);
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,first_name TEXT);
                CREATE TABLE tariffs(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,payment_state TEXT,vpn_state TEXT,tax_state TEXT,refund_state TEXT,subscription_id INTEGER,receipt_uuid TEXT,receipt_url TEXT);
                INSERT INTO users VALUES (1,'<script>alert(1)</script>');
                INSERT INTO tariffs VALUES (1,'Test');
                INSERT INTO payments VALUES
                  (1,'old',1,1,1,'paid','2026-11-25 12:00:00'),
                  (8,'pending',1,1,1,'pending','2026-11-26 12:31:00'),
                  (9,'refund',1,1,1,'paid','2026-11-26 12:33:00'),
                  (10,'canceled',2,1,1,'canceled','2026-11-26 12:34:00'),
                  (11,'review',1,1,120,'paid','2026-11-26 12:35:00'),
                  (12,'free',1,1,0,'paid','2026-11-26 12:36:00');
                INSERT INTO payment_processing VALUES
                  ('refund','verified','done','canceled','full_done',3,'receipt1','https://lknpd.nalog.ru/api/v1/receipt/123456789012/receipt1/print'),
                  ('review','verified','done','needs_review','none',4,NULL,NULL),
                  ('canceled','canceled','not_started','skipped','none',NULL,NULL,NULL);
            """)

    def test_all_rows_and_stats(self):
        data=load_journal(self.path)
        self.assertEqual([p["id"] for p in data["payments"]],[12,11,10,9,8,1])
        self.assertEqual(data["stats"],{"total":6,"gross":122.0,"completed_refunds":1.0,"pending":1,"canceled":1,"review":1})
        self.assertEqual(data["payments"][2]["customer"],None)

    def test_filters(self):
        for key,ids in {"new":[11,10,9,8],"historical":[12,1],"review":[11],"refund":[9],
                        "paid":[12,11,9,1],"canceled":[10],"pending":[8]}.items():
            self.assertEqual([p["id"] for p in load_journal(self.path,key)["payments"]],ids)
        self.assertEqual(load_journal(self.path,"' OR 1=1 --")["selected"],"all")

    def test_pagination(self):
        with sqlite3.connect(self.path) as db:
            db.executemany("INSERT INTO payments VALUES (?, ?,1,1,1,'pending','2026-11-26 12:40:00')",[(i,str(i)) for i in range(20,125)])
        d=load_journal(self.path,page=2)
        self.assertEqual(d["pages"],3)
        self.assertEqual(len(d["payments"]),50)
        self.assertEqual(load_journal(self.path,page=999)["page"],3)
        self.assertEqual(load_journal(self.path,page="wrong")["page"],1)

    def test_links_allowlist(self):
        good="https://lknpd.nalog.ru/api/v1/receipt/123456789012/receipt1/print"
        self.assertEqual(safe_receipt_link(good,"receipt1"),good)
        for bad in ("javascript:alert(1)",good+"?x=1",good.replace("nalog.ru","nalog.ru.evil.invalid"),good.replace("receipt1","other")):
            self.assertIsNone(safe_receipt_link(bad,"receipt1"))
        refund=load_journal(self.path,"refund")["payments"][0]
        self.assertEqual(refund["receipt_link"],good)
        self.assertNotIn("receipt_uuid",refund)

    def test_read_only(self):
        before=self.path.read_bytes()
        load_journal(self.path)
        self.assertEqual(before,self.path.read_bytes())
