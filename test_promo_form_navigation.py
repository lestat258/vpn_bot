import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramNetworkError
from states import EnterPromocode
from test_renewal_checkout import load_functions
import form_navigation as form
import screen_navigation as nav


class PromoFormTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p=patch.object(nav,"NAV_DB_PATH",Path(tmp.name)/"nav.db")
        p.start()
        self.addCleanup(p.stop)
        self.db=Path(tmp.name)/"main.db"
        with closing(sqlite3.connect(self.db)) as db,db:
            db.executescript("""
                CREATE TABLE promocodes(id INTEGER PRIMARY KEY,code TEXT,discount_percent INTEGER,
                    discount_amount REAL,valid_until TEXT,max_uses INTEGER,used_count INTEGER,is_active INTEGER);
                CREATE TABLE promocode_uses(id INTEGER PRIMARY KEY,promocode_id INTEGER,user_id INTEGER);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO promocodes VALUES(1,'SAVE',15,0,NULL,10,0,1);
            """)
        self.bot=SimpleNamespace(id=100,edit_message_text=AsyncMock(),delete_message=AsyncMock())
        self.chat=SimpleNamespace(id=42,type="private")
        self.screen=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=SimpleNamespace(id=100),message_id=7)
        self.message=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=SimpleNamespace(id=42),
            message_id=8,text="SAVE",answer=AsyncMock(),delete=AsyncMock())
        self.callback=SimpleNamespace(message=self.screen,data="profile_promocode",answer=AsyncMock())
        self.state=SimpleNamespace(get_state=AsyncMock(return_value=form.PROMO_STATE),
            get_data=AsyncMock(return_value={form.FORM_SCREEN:7}),clear=AsyncMock(),
            set_state=AsyncMock(),set_data=AsyncMock(),update_data=AsyncMock())
        self.render=AsyncMock(return_value=self.screen)
        self.error=AsyncMock()
        self.next=AsyncMock()
        nav._save((100,42),7,"text")
        self.env=load_functions("handlers/users.py",{"process_promocode_input"},
            dict(types=SimpleNamespace(Message=object),FSMContext=object,
                 current_form_input=form.current_form_input,PROMO_STATE=form.PROMO_STATE,
                 sqlite3=sqlite3,closing=closing,datetime=datetime,DB_PATH=self.db,
                 promo_input_error=self.error,render_command=self.render,get_back_keyboard=lambda x:x))
        self.start=load_functions("handlers/profile.py",{"profile_promocode"},
            dict(types=SimpleNamespace(CallbackQuery=object),FSMContext=object,
                 EnterPromocode=EnterPromocode,FORM_SCREEN=form.FORM_SCREEN,is_current_screen=form.is_current_screen,
                 render_screen=self.render,get_back_keyboard=lambda x:x,PROMO_PROMPT="Prompt"))

    def snapshot(self):
        with closing(sqlite3.connect(self.db)) as db:
            return (db.execute("SELECT used_count FROM promocodes").fetchone()[0],
                db.execute("SELECT promocode_id,user_id FROM promocode_uses").fetchall(),
                db.execute("SELECT key,value FROM settings").fetchall())

    async def test_promo_start_binds_active_screen(self):
        self.state.get_state.return_value=None
        await self.start["profile_promocode"](self.callback,self.state)
        self.state.set_data.assert_awaited_once_with({form.FORM_SCREEN:7})
        self.state.set_state.assert_awaited_once_with(EnterPromocode.waiting)

    async def test_stale_promo_start_rejected(self):
        self.state.get_state.return_value=None
        nav._save((100,42),9,"text")
        await self.start["profile_promocode"](self.callback,self.state)
        self.render.assert_not_awaited()

    async def test_valid_promo_writes_atomic_legacy_discount(self):
        await self.env["process_promocode_input"](self.message,self.state)
        self.assertEqual(self.snapshot(),(1,[(1,42)],[("promocode_user_42","15|0")]))
        self.state.clear.assert_awaited_once()
        self.assertIn("15%",self.render.await_args.args[1])

    async def test_unknown_code_keeps_form_open(self):
        self.message.text="BAD"
        await self.env["process_promocode_input"](self.message,self.state)
        self.error.assert_awaited_once()
        self.state.clear.assert_not_awaited()
        self.assertEqual(self.snapshot(),(0,[],[]))

    async def test_duplicate_code_does_not_increment(self):
        await self.env["process_promocode_input"](self.message,self.state)
        await self.env["process_promocode_input"](self.message,self.state)
        self.assertEqual(self.snapshot(),(1,[(1,42)],[("promocode_user_42","15|0")]))
        self.error.assert_awaited_once()

    async def test_stale_input_does_not_activate(self):
        nav._save((100,42),9,"text")
        await self.env["process_promocode_input"](self.message,self.state)
        self.assertEqual(self.snapshot(),(0,[],[]))
        self.render.assert_not_awaited()
        self.state.clear.assert_awaited_once()

    async def test_stale_callback_blocked_during_promo(self):
        self.callback.data="deposit_100"
        await form.UserFormNavigation()(self.next,self.callback,{"state":self.state})
        self.next.assert_not_awaited()
        self.callback.answer.assert_awaited_once()

    async def test_current_back_allowed_during_promo(self):
        self.callback.data="promo_achievements"
        await form.UserFormNavigation()(self.next,self.callback,{"state":self.state})
        self.next.assert_awaited_once()

    async def test_cancel_deletes_only_explicit_command_after_render(self):
        self.message.text="/cancel"
        await nav.render_command(self.message,"Menu",clean_cancel=True)
        self.message.delete.assert_awaited_once()
        self.message.text="SAVE"
        self.message.delete.reset_mock()
        await nav.render_command(self.message,"Menu",clean_cancel=True)
        self.message.delete.assert_not_awaited()

    async def test_failed_cancel_render_preserves_command(self):
        self.message.text="/cancel"
        self.bot.edit_message_text.side_effect=TelegramNetworkError(method=None,message="timeout")
        with self.assertRaises(TelegramNetworkError):
            await nav.render_command(self.message,"Menu",clean_cancel=True)
        self.message.delete.assert_not_awaited()

    async def test_activation_rolls_back_if_setting_write_fails(self):
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("CREATE TRIGGER fail_setting BEFORE INSERT ON settings BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.env["process_promocode_input"](self.message,self.state)
        self.assertEqual(self.snapshot(),(0,[],[]))
        self.render.assert_not_awaited()
