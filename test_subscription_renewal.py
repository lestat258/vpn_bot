import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from subscription_renewal import DAY_MS,renewal_days,renew_subscription,expiry_presentation

NOW=1800000000000
EMAIL="test@example.invalid"


class FakePanel:
    def __init__(self,expiry,enabled=True,total=0,used=0):
        self.client={"email":EMAIL,"uuid":"uid","subId":"sid","enable":enabled,"expiryTime":expiry,"totalGB":total}
        self.ids=[5,8]
        self.used=used
        self.writes=[]
        self.fail=False
        self.reenable=True

    def _request(self,method,path,data=None,retries=None):
        if method=="GET":
            return {"success":True,"obj":{"client":dict(self.client),"inboundIds":list(self.ids),"usedTraffic":self.used}}
        self.writes.append((path,data,retries))
        self.client["expiryTime"]+=data["addDays"]*DAY_MS
        if self.reenable:
            self.client["enable"]=True
        return None if self.fail else {"success":True,"obj":{"adjusted":1}}


class RenewalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=str(Path(tmp.name)/"test.db")
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,is_blocked INTEGER);
                CREATE TABLE servers(id INTEGER PRIMARY KEY,url TEXT,api_token TEXT,is_active INTEGER);
                CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,telegram_id INTEGER,server_id INTEGER,
                    tariff_id INTEGER,end_date TEXT,xui_email TEXT,xui_client_uid TEXT,xui_subid TEXT,
                    is_active INTEGER,managed_by_vpn4us INTEGER);
                CREATE TABLE subscription_inbound_memberships(subscription_id INTEGER,server_id INTEGER,inbound_id INTEGER);
                INSERT INTO users VALUES(42,0);
                INSERT INTO servers VALUES(2,'https://example.invalid','synthetic',1);
                INSERT INTO subscription_inbound_memberships VALUES(1,2,5),(1,2,8);
            """)

    def setup_client(self,expiry,**kwargs):
        self.panel=FakePanel(expiry,**kwargs)
        self.original=datetime.fromtimestamp(expiry/1000,timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("INSERT INTO subscriptions VALUES(1,42,2,3,?,'test@example.invalid','uid','sid',1,1)",(self.original,))

    def renew(self):
        return renew_subscription(self.path,1,3,4,lambda **kw:self.panel,clock=lambda:NOW)

    def end(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT end_date,tariff_id FROM subscriptions WHERE id=1").fetchone()

    def test_active_exact_extension(self):
        self.setup_client(NOW+DAY_MS)
        self.assertEqual(int(self.renew().timestamp()*1000),NOW+4*DAY_MS)
        self.assertEqual(self.panel.writes,[("/panel/api/clients/bulkAdjust",{"emails":[EMAIL],"addDays":3},1)])
        self.assertEqual(self.end()[1],4)

    def test_expired_rounding_and_reactivation(self):
        self.setup_client(NOW-2*DAY_MS-1000,enabled=False)
        target=int(self.renew().timestamp()*1000)
        self.assertGreaterEqual(target,NOW+3*DAY_MS)
        self.assertLess(target,NOW+4*DAY_MS)
        self.assertTrue(self.panel.client["enable"])
        self.assertEqual(self.panel.writes[0][1]["addDays"],6)

    def test_exact_day_and_boundary(self):
        self.assertEqual(renewal_days(NOW,NOW,3),3)
        self.assertEqual(renewal_days(NOW-DAY_MS,NOW,3),4)
        self.assertEqual(renewal_days(NOW-1,NOW,3),4)

    def test_blocked_user_and_local_subscription(self):
        self.setup_client(NOW-1000,enabled=False)
        for sql in ("UPDATE users SET is_blocked=1","UPDATE users SET is_blocked=0; UPDATE subscriptions SET is_active=0"):
            with closing(sqlite3.connect(self.path)) as db,db:
                db.executescript(sql)
            with self.assertRaises(ValueError):
                self.renew()
            self.assertEqual(self.panel.writes,[])

    def test_disabled_unexpired(self):
        self.setup_client(NOW+1000,enabled=False)
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(self.panel.writes,[])

    def test_traffic_depletion_not_reactivated(self):
        self.setup_client(NOW-1000,enabled=False,total=100,used=100)
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(self.panel.writes,[])

    def test_wrong_identity_or_membership(self):
        self.setup_client(NOW-1000,enabled=False)
        self.panel.client["subId"]="wrong"
        with self.assertRaises(ValueError):
            self.renew()
        self.panel.client["subId"]="sid"
        self.panel.ids=[5,9]
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(self.panel.writes,[])

    def test_ambiguous_write_never_commits_or_replays(self):
        self.setup_client(NOW-1000,enabled=False)
        self.panel.fail=True
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(len(self.panel.writes),1)
        self.assertEqual(self.end(),(self.original,3))
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(len(self.panel.writes),1)

    def test_still_disabled_after_update(self):
        self.setup_client(NOW-1000,enabled=False)
        self.panel.reenable=False
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(self.end(),(self.original,3))
        self.assertEqual(len(self.panel.writes),1)

    def test_expiry_display(self):
        now=datetime(2026,11,27,12,0,tzinfo=timezone.utc)
        self.assertEqual(expiry_presentation((now-timedelta(minutes=1)).isoformat(),now=now),
                         (False,"27.11.2026","Срок действия истёк"))
        self.assertEqual(expiry_presentation((now+timedelta(minutes=1)).isoformat(),now=now),
                         (True,"27.11.2026","Менее суток"))
