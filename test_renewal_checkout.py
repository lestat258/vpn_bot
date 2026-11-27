"""Offline checkout regression: actual handler bodies, isolated dependencies."""
import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from html import escape
from subscription_renewal import renewable_subscriptions,renewal_tariff,expiry_presentation

ROOT=Path(__file__).resolve().parent


def load_functions(path,names,env):
    tree=ast.parse((ROOT/path).read_text(encoding="utf-8"))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),"exec"),env)
    return env


class CheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=str(Path(tmp.name)/"test.db")
        with closing(sqlite3.connect(self.path)) as db,db:
            db.executescript("""
                CREATE TABLE users(telegram_id INTEGER,is_blocked INTEGER);
                CREATE TABLE servers(id INTEGER,is_active INTEGER);
                CREATE TABLE tariffs(id INTEGER,name TEXT,price_rub REAL,duration_days INTEGER,traffic_gb REAL,ip_limit INTEGER,is_active INTEGER);
                CREATE TABLE subscriptions(id INTEGER,telegram_id INTEGER,server_id INTEGER,tariff_id INTEGER,end_date TEXT,is_active INTEGER,managed_by_vpn4us INTEGER);
                INSERT INTO users VALUES(42,0),(43,0);
                INSERT INTO servers VALUES(1,1);
                INSERT INTO tariffs VALUES(1,'Test',1,3,0,3,1),(2,'Free',0,3,0,3,1),(3,'Hidden',1,3,0,3,0);
                INSERT INTO subscriptions VALUES(1,42,1,1,'2099-01-01',1,1),(2,42,1,1,'2000-01-01',1,1),(3,43,1,1,'2000-01-01',1,1);
            """)
        self.pay=AsyncMock()
        self.callback=SimpleNamespace(data="extend_tariff_1_2",from_user=SimpleNamespace(id=42),answer=AsyncMock(),
                                      message=SimpleNamespace(answer=AsyncMock()))
        self.env=load_functions("handlers/payments.py",{"checkout_renewal","handle_direct_renewal","handle_renew_choose"},
            dict(DB_PATH=self.path,renewable_subscriptions=renewable_subscriptions,renewal_tariff=renewal_tariff,
                 process_payment=self.pay,types=SimpleNamespace(CallbackQuery=object)))

    async def test_direct_expired_target(self):
        await self.env["handle_direct_renewal"](self.callback)
        self.pay.assert_awaited_once_with(self.callback,1,"Test",1,3,0,3,action="renew",subscription_id=2,back_to="renew_from_key_2")

    async def test_other_owner_and_missing_target(self):
        for value in ("extend_tariff_1_3","extend_tariff_1_999","extend_tariff_x_2"):
            self.callback.data=value
            await self.env["handle_direct_renewal"](self.callback)
        self.pay.assert_not_awaited()

    async def test_free_and_inactive_tariff(self):
        for value in ("extend_tariff_2_2","extend_tariff_3_2"):
            self.callback.data=value
            await self.env["handle_direct_renewal"](self.callback)
        self.pay.assert_not_awaited()

    async def test_buy_renewal_target(self):
        self.callback.data="renew_choose_1_2"
        await self.env["handle_renew_choose"](self.callback)
        self.pay.assert_awaited_once_with(self.callback,1,"Test",1,3,0,3,action="renew",subscription_id=2,back_to="renew_select_1")

    async def test_profile_passes_exact_id(self):
        kb=Mock(return_value="keyboard")
        env=load_functions("handlers/profile.py",{"renew_from_key"},
            dict(DB_PATH=self.path,renewable_subscriptions=renewable_subscriptions,
                 expiry_presentation=expiry_presentation,escape=escape,get_tariffs_keyboard=kb,
                 render_screen=AsyncMock(),
                 types=SimpleNamespace(CallbackQuery=object)))
        self.callback.data="renew_from_key_2"
        await env["renew_from_key"](self.callback)
        kb.assert_called_once_with(subscription_id=2)
        self.assertIn("№2",env["render_screen"].await_args.args[1])

    async def test_keyboards_keep_target_and_buy_separate(self):
        env=load_functions("keyboards/keyboards.py",{"get_tariffs_keyboard"},
            dict(DB_PATH=self.path,sqlite3=sqlite3,
                 InlineKeyboardButton=lambda **kw:kw,InlineKeyboardMarkup=lambda **kw:kw))
        direct=env["get_tariffs_keyboard"](subscription_id=2)["inline_keyboard"]
        buy=env["get_tariffs_keyboard"]()["inline_keyboard"]
        self.assertEqual([r[0]["callback_data"] for r in direct],["extend_tariff_1_2","key_2"])
        self.assertIn("tariff_1",[r[0]["callback_data"] for r in buy])
        self.assertIn("tariff_2",[r[0]["callback_data"] for r in buy])

    async def test_list_expired_and_active_but_not_foreign(self):
        self.assertEqual([r[0] for r in renewable_subscriptions(self.path,42)],[1,2])
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE users SET is_blocked=1 WHERE telegram_id=42")
        self.assertEqual(renewable_subscriptions(self.path,42),[])
