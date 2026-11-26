import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from dashboard_metrics import dashboard_data, local_metrics, cpu_percent, probe_panel, token_clear


class DashboardTests(unittest.TestCase):
    def test_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"data.db"
            with sqlite3.connect(path) as db:
                db.executescript("""
                CREATE TABLE users(created_at TEXT);
                INSERT INTO users VALUES ('2026-11-25 22:00:00');
                CREATE TABLE subscriptions(tariff_id INT,is_active INT,end_date TEXT);
                INSERT INTO subscriptions VALUES(1,1,'2026-12-01'),(1,1,'2026-11-01');
                CREATE TABLE tariffs(id INT,name TEXT);
                INSERT INTO tariffs VALUES(1,'Test');
                CREATE TABLE servers(is_active INT);
                INSERT INTO servers VALUES(1),(0);
                CREATE TABLE payments(id INT,payment_id TEXT,status TEXT,amount_rub REAL,created_at TEXT);
                INSERT INTO payments VALUES(7,'old','paid',5,'2026-11-25 22:00:00'),
                    (8,'new','paid',2,'2026-11-26 13:00:00'),
                    (9,'free','paid',0,'2026-11-26 13:00:00'),
                    (10,'pending','pending',3,'2026-11-26 13:00:00');
                CREATE TABLE payment_processing(payment_id TEXT,is_legacy INT,refund_state TEXT);
                INSERT INTO payment_processing VALUES('old',0,'full_done'),('new',0,'full_done');
                """)
            now=datetime(2026,11,26,15,tzinfo=timezone.utc)
            out=dashboard_data(path,now)
            self.assertEqual(out["finance"][0],{"label":"Сегодня","gross":"7.00","refunded":"2.00"})
            self.assertEqual(out["payments_count"],2)
            self.assertEqual(out["active_subs"],1)
            self.assertEqual(out["enabled_servers"],1)
            self.assertEqual(out["servers_count"],2)
            self.assertEqual(out["new_users_today"],1)
            self.assertEqual(len(out["payments_daily"]),7)
            self.assertEqual(out["payments_daily"][-1]["count"],2)
            self.assertEqual(out["top_tariffs"],[{"name":"Test","count":1}])

    def test_cpu(self):
        self.assertEqual(cpu_percent((100,50),(200,75)),75)
        self.assertIsNone(cpu_percent((100,50),(100,50)))

    def test_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/"meminfo").write_text("MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n")
            (p/"uptime").write_text("90061.0 0")
            with patch("dashboard_metrics.cpu_ticks",side_effect=[(100,50),(200,75)]):
                data=local_metrics(p,tmp)
            self.assertIn("75.0%",data["cpu"])
            self.assertEqual(data["memory"],"0.50 / 1.00 ГиБ (50%)")
            self.assertEqual(data["uptime"],"1 д. 1 ч. 1 мин.")
            self.assertNotEqual(data["disk"],"Нет данных")

    def test_token(self):
        import base64
        from cryptography.fernet import Fernet
        k=Fernet.generate_key()
        stored=base64.urlsafe_b64encode(Fernet(k).encrypt(b"abc")).decode()
        self.assertEqual(token_clear(stored,base64.urlsafe_b64encode(k).decode()),"abc")
        with self.assertRaises(Exception):
            token_clear(stored,None)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe(self):
        import httpx
        async def handler(request):
            self.assertEqual(request.headers["authorization"],"Bearer abc")
            return httpx.Response(200,json={"success":True,"obj":[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            self.assertEqual(await probe_panel(client,{"url":"https://example.org/panel","api_token":"abc"},None),"API доступен")
            self.assertEqual(await probe_panel(client,{"url":"http://example.org","api_token":"abc"},None),"Не проверен")

    async def test_redirect_not_success(self):
        import httpx
        async def handler(request):
            return httpx.Response(302,headers={"location":"https://other.example"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            self.assertEqual(await probe_panel(client,{"url":"https://example.org","api_token":"abc"},None),"Не подтверждён")
