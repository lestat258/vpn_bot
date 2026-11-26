import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from system_monitor import record_cycle, history, deliver, claim_event


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path=str(Path(folder.name)/"test.db")
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO settings VALUES('admin_id','12345')")

    def tick(self,t,**values):
        record_cycle(self.path,{key:(key,value) for key,value in values.items()},now=t)

    def events(self):
        return history(self.path,now=1000)["events"]

    def test_sustained_cpu_memory_and_recovery(self):
        for t in range(0,301,60):
            self.tick(t,cpu=95,memory=90)
        self.assertEqual(len(self.events()),2)
        self.tick(360,cpu=95,memory=90)
        self.assertEqual(len(self.events()),2)
        self.tick(420,cpu=70,memory=70)
        self.tick(480,cpu=None,memory=70)
        self.assertEqual(len(self.events()),3)
        self.tick(540,cpu=70,memory=70)
        self.tick(600,cpu=70,memory=70)
        self.assertEqual(len(self.events()),4)
        self.assertFalse(any(x["active"] for x in history(self.path)["states"]))

    def test_gap_breaks_evidence(self):
        self.tick(0,cpu=99)
        self.tick(60,cpu=99)
        self.tick(600,cpu=99)
        self.assertEqual(self.events(),[])

    def test_panel_three_failures_and_no_fake_recovery(self):
        for t in (0,60,120):
            self.tick(t,**{"panel:1":"Не подтверждён"})
        self.assertEqual(len(self.events()),1)
        self.tick(180,**{"panel:1":"Не проверен"})
        self.assertEqual(len(self.events()),1)
        self.tick(240,**{"panel:1":"API доступен"})
        self.tick(300,**{"panel:1":"API доступен"})
        self.assertEqual(self.events()[0]["kind"],"recovered")

    def test_disk_hysteresis_and_removed_panel(self):
        self.tick(0,disk=85)
        self.tick(60,disk=82)
        self.tick(120,disk=80)
        self.tick(180,disk=80)
        self.assertEqual([e["kind"] for e in self.events()],["recovered","opened"])
        for t in (240,300,360):
            self.tick(t,**{"panel:1":"Не подтверждён"})
        record_cycle(self.path,{},now=420,checked_panel_ids=set())
        self.assertEqual(self.events()[0]["kind"],"removed")
        self.assertEqual(self.events()[0]["delivery"],"not_required")

    async def test_delivery_dedup_and_recovery(self):
        self.tick(0,disk=90)
        sender=AsyncMock()
        await deliver(self.path,sender=sender)
        await deliver(self.path,sender=sender)
        self.assertEqual(sender.await_count,1)
        self.assertEqual(self.events()[0]["delivery"],"sent")
        self.tick(60,disk=70)
        self.tick(120,disk=70)
        await deliver(self.path,sender=sender)
        self.assertEqual(sender.await_count,2)

    async def test_ambiguous_and_interrupted_send(self):
        self.tick(0,disk=90)
        sender=AsyncMock(side_effect=TimeoutError())
        await deliver(self.path,sender=sender)
        await deliver(self.path,sender=sender)
        self.assertEqual(sender.await_count,1)
        self.assertEqual(self.events()[0]["delivery"],"unconfirmed")
        self.tick(60,disk=70)
        self.tick(120,disk=70)
        self.assertIsNotNone(claim_event(self.path))
        await deliver(self.path,sender=sender)
        self.assertEqual(sender.await_count,1)
        self.assertEqual(self.events()[0]["delivery"],"attempted")

    def test_no_tables_and_stale_heartbeat(self):
        self.assertFalse(history(self.path,now=0)["fresh"])
        self.tick(100,disk=10)
        self.assertTrue(history(self.path,now=200)["fresh"])
        self.assertFalse(history(self.path,now=300)["fresh"])
