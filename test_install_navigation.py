"""Regression tests for install navigation, isolated from live VPN and Telegram."""
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from html import escape
from subscription_renewal import expiry_presentation
from test_renewal_checkout import load_functions


class InstallNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path=str(Path(tmp.name)/"test.db")
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE tariffs(id INTEGER,name TEXT);
                CREATE TABLE subscriptions(id INTEGER,telegram_id INTEGER,xui_email TEXT,end_date TEXT,
                    tariff_id INTEGER,xui_client_uid TEXT,is_active INTEGER);
                INSERT INTO tariffs VALUES(1,'Test');
                INSERT INTO subscriptions VALUES(1,42,'a','2099-12-06',1,'uid1',1),
                    (2,42,'b','2099-12-01',1,'uid2',1),(3,43,'c','2099-12-01',1,'uid3',1);
            """)
        self.path=path
        self.screen=AsyncMock()
        self.vpn=SimpleNamespace(get_subscription_link=AsyncMock(return_value="https://example.org/sub"))
        self.callback=SimpleNamespace(data="device_ios",from_user=SimpleNamespace(id=42),answer=AsyncMock(),message=object())
        self.env=load_functions("handlers/users.py",
            {"device_instruction","install_select_back","render_install_choices","device_key_selected",
             "show_instruction_for_key","back_to_devices","back_to_keys_from_install"},
            dict(DB_PATH=path,sqlite3=sqlite3,logging=logging,expiry_presentation=expiry_presentation,
                 escape=escape,render_screen=self.screen,vpn_service=self.vpn,get_setting=lambda k:None,
                 get_back_keyboard=lambda x:{"inline_keyboard":[[{"callback_data":x}]]},
                 get_device_keyboard=lambda:"devices",get_tariffs_keyboard=lambda:"tariffs",
                 types=SimpleNamespace(CallbackQuery=object,InlineKeyboardButton=lambda **kw:kw,
                                       InlineKeyboardMarkup=lambda **kw:kw)))

    def buttons(self):
        return [b for row in self.screen.await_args.kwargs["reply_markup"]["inline_keyboard"] for b in row]

    async def test_multiple_subscriptions_select_screen_and_date(self):
        await self.env["device_instruction"](self.callback)
        self.assertEqual([b["callback_data"] for b in self.buttons()],
                         ["select_device_ios_1","select_device_ios_2","back_to_devices"])
        self.assertIn("06.12.2099",self.buttons()[0]["text"])
        self.vpn.get_subscription_link.assert_not_awaited()

    async def test_selected_subscription_returns_to_same_device_selection(self):
        for device in ("ios","android","windows"):
            self.callback.data=f"select_device_{device}_1"
            await self.env["device_key_selected"](self.callback)
            self.assertEqual([b["callback_data"] for b in self.buttons() if "callback_data" in b],
                             [f"install_select_{device}"])
        self.assertNotIn("Добавить в 1 клик",self.screen.await_args.args[1])

    async def test_single_subscription_returns_to_devices(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM subscriptions WHERE id=2")
        await self.env["device_instruction"](self.callback)
        self.assertEqual([b["callback_data"] for b in self.buttons() if "callback_data" in b],["back_to_devices"])

    async def test_back_to_selection_does_not_skip_even_if_only_one_remains(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM subscriptions WHERE id=2")
        self.callback.data="install_select_ios"
        await self.env["install_select_back"](self.callback)
        self.assertEqual([b["callback_data"] for b in self.buttons()],["select_device_ios_1","back_to_devices"])
        self.vpn.get_subscription_link.assert_not_awaited()

    async def test_legacy_back_returns_to_devices(self):
        await self.env["back_to_keys_from_install"](self.callback)
        self.assertEqual(self.screen.await_args.kwargs["reply_markup"],"devices")

    async def test_missing_link_preserves_return_path(self):
        self.vpn.get_subscription_link.return_value=None
        self.callback.data="select_device_ios_1"
        await self.env["device_key_selected"](self.callback)
        self.assertEqual(self.buttons()[0]["callback_data"],"install_select_ios")

    async def test_foreign_subscription_never_fetches_link(self):
        self.callback.data="select_device_ios_3"
        await self.env["device_key_selected"](self.callback)
        self.vpn.get_subscription_link.assert_not_awaited()
        self.assertEqual(self.buttons()[0]["callback_data"],"install_select_ios")

    async def test_legacy_instruction_no_longer_promises_missing_button(self):
        self.env["get_setting"]=lambda k:"2. Нажмите «Добавить в 1 клик» ниже"
        self.callback.data="select_device_ios_1"
        await self.env["device_key_selected"](self.callback)
        self.assertNotIn("Добавить в 1 клик",self.screen.await_args.args[1])
        self.assertIn("Скопируйте ссылку",self.screen.await_args.args[1])
