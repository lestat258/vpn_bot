"""Admin form navigation: real middleware, isolated state and SQLite."""
import math
from urllib.parse import urlsplit
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from states import AddTariff, EditTariff, EditText, AddServer, SendMessageToUser
import admin_form_navigation as form
import screen_navigation as nav
from test_renewal_checkout import load_functions


class State:
    def __init__(self):
        self.current=None
        self.data={}

    async def get_state(self):
        return self.current

    async def get_data(self):
        return self.data.copy()

    async def clear(self):
        self.current=None
        self.data={}

    async def set_state(self,state):
        self.current=state.state

    async def set_data(self,data):
        self.data=data.copy()

    async def update_data(self,**data):
        self.data.update(data)


class AdminFormTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/"db.sqlite"
        for obj,key,value in (
            (nav,"NAV_DB_PATH",Path(tmp.name)/"navigation.db"),
            (form,"render_screen",AsyncMock(side_effect=self.render_callback)),
            (form,"render_command",AsyncMock(side_effect=self.render_message)),
        ):
            p=patch.object(obj,key,value)
            p.start()
            self.addCleanup(p.stop)
        with closing(sqlite3.connect(self.db)) as db,db:
            db.executescript("""
                CREATE TABLE tariffs(id INTEGER PRIMARY KEY,name TEXT,price_rub REAL,
                  duration_days INTEGER,traffic_gb REAL,ip_limit INTEGER,is_active INTEGER DEFAULT 1);
                INSERT INTO tariffs VALUES(1,'Existing',150,30,0,3,1);
                CREATE TABLE servers(id INTEGER PRIMARY KEY,name TEXT,url TEXT,api_token TEXT,is_active INTEGER,status TEXT);
            """)
        self.bot=SimpleNamespace(id=100,send_message=AsyncMock())
        self.admin=SimpleNamespace(id=42)
        self.chat=SimpleNamespace(type="private",id=42)
        self.screen=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=SimpleNamespace(id=100),
            message_id=7,reply_markup=None)
        self.message=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=self.admin,text="hello",answer=AsyncMock(),delete=AsyncMock())
        self.callback=SimpleNamespace(message=self.screen,from_user=self.admin,data="admin_add_tariff",answer=AsyncMock())
        self.state=State()
        self.next=AsyncMock()
        self.settings=Mock()
        self.encrypt=Mock(side_effect=lambda token: 'encrypted:'+token)
        self.user=Mock(side_effect=lambda uid: (1,uid,'Username','Name') if uid==43 else None)
        self.legacy={42:1}
        self.middleware=form.AdminFormNavigation(42)
        nav._save((100,42),7,"text")
        self.set_buttons("admin_add_tariff","admin_edit_tariff","edittext_text_faq")
        self.env=load_functions("handlers/admin.py",{
            "admin_add_tariff_start","tariff_name","tariff_price","tariff_days","tariff_traffic","tariff_ip",
            "tariff_number","admin_edit_tariff_select","admin_edit_tariff_start","edit_tariff_field",
            "edit_tariff_name","edit_tariff_price","edit_tariff_days","edit_tariff_traffic","edit_tariff_ip_limit",
            "edit_text_start","edit_text_save","admin_add_server_start","server_name","panel_url","server_url","server_token",
            "send_message_from_user_card","send_message_process"},
            dict(types=SimpleNamespace(CallbackQuery=object,Message=object,
                 InlineKeyboardMarkup=form.types.InlineKeyboardMarkup,InlineKeyboardButton=form.types.InlineKeyboardButton),
                 FSMContext=object,ADMIN_ID=42,DB_PATH=self.db,AddTariff=AddTariff,EditTariff=EditTariff,
                 EditText=EditText,sqlite3=sqlite3,closing=closing,logging=logging,math=math,
                 form_begin=form.begin,form_step=form.step,form_retry=form.retry,form_finish=form.finish,
                 get_setting=lambda _: "<b>Old</b>",set_setting=self.settings,
                 ADMIN_TEXT_FIELDS={"text_faq":"FAQ"},extend_temp=self.legacy,
                 AddServer=AddServer,SendMessageToUser=SendMessageToUser,urlsplit=urlsplit,
                 TelegramAPIError=TelegramAPIError,encrypt=self.encrypt,get_user_by_telegram_id=self.user))

    def set_buttons(self,*values):
        self.screen.reply_markup=SimpleNamespace(inline_keyboard=[
            [SimpleNamespace(callback_data=v)] for v in values])

    async def render_callback(self,message,text,**kwargs):
        message.reply_markup=kwargs.get("reply_markup")
        return message

    async def render_message(self,message,text,**kwargs):
        self.screen.reply_markup=kwargs.get("reply_markup")
        return None

    async def call(self,event,handler=None):
        return await self.middleware(handler or self.next,event,{"state":self.state,"raw_state":self.state.current})

    async def start(self):
        await self.call(self.callback,lambda event,data:self.env["admin_add_tariff_start"](event,self.state))

    async def enter(self,text,name):
        self.message.text=text
        await self.call(self.message,lambda event,data:self.env[name](event,self.state))

    async def edit(self):
        self.callback.data="admin_edit_tariff"
        await self.call(self.callback,lambda event,data:self.env["admin_edit_tariff_select"](event,self.state))
        self.callback.data="admin_edit_tariff_select_1"
        await self.call(self.callback,lambda event,data:self.env["admin_edit_tariff_start"](event,self.state))

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT * FROM tariffs ORDER BY id").fetchall()

    async def test_start_binds_screen_and_abandons_legacy_extension(self):
        await self.start()
        self.assertEqual(self.state.current,AddTariff.name.state)
        self.assertEqual(self.state.data[form.FORM_SCREEN],7)
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"admin_tariffs")
        self.assertEqual(self.legacy,{})

    async def test_stale_start_rejected(self):
        nav._save((100,42),8,"text")
        await self.call(self.callback)
        self.next.assert_not_awaited()

    async def test_unshown_button_rejected(self):
        self.callback.data="edittext_secret"
        await self.call(self.callback)
        self.next.assert_not_awaited()

    async def test_unauthorized_start_rejected(self):
        self.callback.from_user=SimpleNamespace(id=43)
        await self.call(self.callback)
        self.next.assert_not_awaited()

    async def test_group_start_rejected(self):
        self.chat.type="group"
        await self.call(self.callback)
        self.next.assert_not_awaited()

    async def test_cancel_back_clears_form_before_parent(self):
        await self.start()
        self.callback.data="admin_tariffs"
        await self.call(self.callback)
        self.next.assert_awaited_once()
        self.assertIsNone(self.state.current)
        self.assertEqual(len(self.rows()),1)

    async def test_unrelated_callback_blocked(self):
        await self.start()
        self.callback.data="admin_delete_tariff"
        await self.call(self.callback)
        self.next.assert_not_awaited()
        self.assertEqual(self.state.current,AddTariff.name.state)

    async def test_stale_back_does_not_cancel_form(self):
        await self.start()
        self.callback.data="admin_tariffs"
        nav._save((100,42),8,"text")
        await self.call(self.callback)
        self.next.assert_not_awaited()
        self.assertEqual(self.state.current,AddTariff.name.state)

    async def test_cancel_command_no_mutation(self):
        await self.start()
        self.message.text="/cancel"
        await self.call(self.message)
        self.next.assert_not_awaited()
        self.assertIsNone(self.state.current)
        self.assertTrue(form.render_command.await_args.kwargs["clean_cancel"])
        self.assertEqual(len(self.rows()),1)

    async def test_menu_clears_state_before_dispatch(self):
        await self.start()
        self.message.text="👤 Профиль"
        data={"state":self.state,"raw_state":self.state.current}
        await self.middleware(self.next,self.message,data)
        self.next.assert_awaited_once()
        self.assertIsNone(data["raw_state"])
        self.assertIsNone(self.state.current)

    async def test_old_input_cannot_mutate(self):
        await self.start()
        nav._save((100,42),8,"text")
        await self.enter("Name","tariff_name")
        self.assertIsNone(self.state.current)
        self.assertEqual(len(self.rows()),1)

    async def test_nontext_input_keeps_prompt(self):
        await self.start()
        self.message.text=None
        await self.call(self.message)
        self.next.assert_not_awaited()
        self.assertIn("Название тарифа",form.render_command.await_args.args[1])
        self.assertEqual(self.state.current,AddTariff.name.state)

    async def test_invalid_numeric_input_keeps_step(self):
        await self.start()
        await self.enter("New","tariff_name")
        for value in ("nan","inf","-1","oops"):
            await self.enter(value,"tariff_price")
            self.assertEqual(self.state.current,AddTariff.price.state)
        self.assertEqual(len(self.rows()),1)

    async def test_create_tariff_full_flow(self):
        await self.start()
        for value,name in zip(("New","1.50","3","0","2"),
            ("tariff_name","tariff_price","tariff_days","tariff_traffic","tariff_ip")):
            await self.enter(value,name)
        self.assertIsNone(self.state.current)
        self.assertEqual(self.rows()[1],(2,"New",1.5,3,0.0,2,1))
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"admin_tariffs")

    async def test_edit_selection_requires_selection_state(self):
        self.callback.data="admin_edit_tariff_select_1"
        self.set_buttons(self.callback.data)
        await self.call(self.callback)
        self.next.assert_not_awaited()

    async def test_edit_skip_preserves_values_and_saves_last_ip(self):
        await self.edit()
        for name in ("edit_tariff_name","edit_tariff_price","edit_tariff_days","edit_tariff_traffic"):
            await self.enter("/skip",name)
        await self.enter("7","edit_tariff_ip_limit")
        self.assertEqual(self.rows(),[(1,"Existing",150.0,30,0.0,7,1)])
        self.assertIsNone(self.state.current)

    async def test_edit_concurrent_change_not_overwritten(self):
        await self.edit()
        for name in ("edit_tariff_name","edit_tariff_price","edit_tariff_days","edit_tariff_traffic"):
            await self.enter("/skip",name)
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("UPDATE tariffs SET price_rub=200 WHERE id=1")
        await self.enter("7","edit_tariff_ip_limit")
        self.assertEqual(self.rows()[0][2],200.0)
        self.assertEqual(self.rows()[0][5],3)
        self.assertIsNone(self.state.current)

    async def test_text_save_preserves_html_and_finishes(self):
        self.callback.data="edittext_text_faq"
        await self.call(self.callback,lambda event,data:self.env["edit_text_start"](event,self.state))
        self.assertIn("<b>Old</b>",form.render_screen.await_args.args[1])
        await self.enter("<b>New</b>","edit_text_save")
        self.settings.assert_called_once_with("text_faq","<b>New</b>")
        self.assertIsNone(self.state.current)

    async def test_text_cancel_never_saves(self):
        self.callback.data="edittext_text_faq"
        await self.call(self.callback,lambda event,data:self.env["edit_text_start"](event,self.state))
        self.message.text="/cancel"
        await self.call(self.message)
        self.settings.assert_not_called()

    async def test_text_field_allowlist(self):
        self.callback.data="edittext_bot_token"
        self.set_buttons(self.callback.data)
        await self.call(self.callback,lambda event,data:self.env["edit_text_start"](event,self.state))
        self.assertIsNone(self.state.current)
        self.settings.assert_not_called()


    def servers(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT name,url,api_token,is_active,status FROM servers").fetchall()

    async def start_server(self):
        self.callback.data="admin_add_server"
        self.set_buttons(self.callback.data)
        await self.call(self.callback,lambda event,data:self.env["admin_add_server_start"](event,self.state))

    async def start_send(self):
        self.callback.data="user_send_message_43"
        self.set_buttons(self.callback.data)
        await self.call(self.callback,lambda event,data:self.env["send_message_from_user_card"](event,self.state))

    async def server_to_token(self):
        await self.start_server()
        await self.enter("Test","server_name")
        await self.enter("https://example.invalid/panel/","server_url")

    async def test_server_start_and_back(self):
        await self.start_server()
        self.assertEqual(self.state.current,AddServer.name.state)
        await self.enter("Test","server_name")
        self.callback.data="admin_servers_menu"
        await self.call(self.callback)
        self.assertIsNone(self.state.current)
        self.assertEqual(self.servers(),[])

    async def test_server_url_rejects_invalid_and_credential_urls(self):
        await self.start_server()
        await self.enter("Test","server_name")
        for value in ("example.invalid","ftp://example.invalid","https://user:pass@example.invalid",
                      "https://example.invalid/?token=x","https://example.invalid/#x","https://example.invalid:99999"):
            await self.enter(value,"server_url")
            self.assertEqual(self.state.current,AddServer.url.state)
        self.assertEqual(self.servers(),[])

    async def test_server_token_encrypted_and_removed(self):
        await self.server_to_token()
        await self.enter("fake-token","server_token")
        self.encrypt.assert_called_once_with("fake-token")
        self.message.delete.assert_awaited_once()
        self.assertEqual(self.servers(),[("Test","https://example.invalid/panel","encrypted:fake-token",1,"pending")])
        self.assertIsNone(self.state.current)
        self.assertNotIn("fake-token",form.render_command.await_args.args[1])

    async def test_server_cancel_before_token_never_writes(self):
        await self.server_to_token()
        await self.enter("/cancel","server_token")
        self.encrypt.assert_not_called()
        self.assertEqual(self.servers(),[])

    async def test_server_invalid_token_not_written(self):
        await self.server_to_token()
        await self.enter("bad token","server_token")
        self.encrypt.assert_not_called()
        self.assertEqual(self.state.current,AddServer.token.state)
        self.message.delete.assert_awaited_once()
        self.assertEqual(self.servers(),[])

    async def test_server_failed_encryption_no_plaintext_fallback(self):
        await self.server_to_token()
        self.encrypt.side_effect=ValueError("secret")
        await self.enter("fake-token","server_token")
        self.assertEqual(self.servers(),[])
        self.assertIsNone(self.state.current)
        self.assertNotIn("secret",form.render_command.await_args.args[1])

    async def test_server_duplicate_url_does_not_overwrite(self):
        await self.server_to_token()
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("INSERT INTO servers VALUES(1,'Old','https://example.invalid/panel/','old',1,'online')")
        await self.enter("fake-token","server_token")
        self.assertEqual(self.servers(),[("Old","https://example.invalid/panel/","old",1,"online")])

    async def test_token_delete_failure_warns_without_echoing(self):
        await self.server_to_token()
        self.message.delete.side_effect=TelegramBadRequest(method=None,message="cannot delete")
        await self.enter("fake-token","server_token")
        self.assertEqual(len(self.servers()),1)
        self.assertIn("вручную",form.render_command.await_args.args[1])
        self.assertNotIn("fake-token",form.render_command.await_args.args[1])

    async def test_send_back_returns_exact_recipient(self):
        await self.start_send()
        self.assertEqual(self.state.current,SendMessageToUser.message_text.state)
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"user_back_to_43")
        self.callback.data="user_back_to_44"
        await self.call(self.callback)
        self.assertEqual(self.state.current,SendMessageToUser.message_text.state)
        self.callback.data="user_back_to_43"
        await self.call(self.callback)
        self.assertIsNone(self.state.current)
        self.bot.send_message.assert_not_awaited()

    async def test_send_cancel_and_menu_never_send(self):
        await self.start_send()
        await self.enter("/cancel","send_message_process")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"user_back_to_43")
        await self.start_send()
        self.message.text="👤 Профиль"
        await self.call(self.message)
        self.assertIsNone(self.state.current)
        self.bot.send_message.assert_not_awaited()

    async def test_send_success_only_selected_user(self):
        await self.start_send()
        await self.enter("<b>Test</b>","send_message_process")
        self.bot.send_message.assert_awaited_once_with(chat_id=43,
            text="📨 <b>Сообщение от администратора</b>\n\n<b>Test</b>",parse_mode="HTML")
        self.assertIsNone(self.state.current)
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"user_back_to_43")

    async def test_send_missing_recipient_no_delivery(self):
        await self.start_send()
        self.user.return_value=None
        self.user.side_effect=None
        await self.enter("Test","send_message_process")
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.state.current)

    async def test_send_uncertain_no_retry(self):
        await self.start_send()
        self.bot.send_message.side_effect=TimeoutError("unknown")
        await self.enter("Test","send_message_process")
        self.assertIsNone(self.state.current)
        self.bot.send_message.assert_awaited_once()
        self.message.text="Test again"
        await self.call(self.message)
        self.bot.send_message.assert_awaited_once()

    async def test_send_nontext_keeps_target(self):
        await self.start_send()
        self.message.text=None
        await self.call(self.message)
        self.assertEqual(self.state.current,SendMessageToUser.message_text.state)
        self.assertEqual(self.screen.reply_markup.inline_keyboard[0][0].callback_data,"user_back_to_43")
        self.bot.send_message.assert_not_awaited()
