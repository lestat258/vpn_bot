"""Administrative balance forms with real SQLite ledger and isolated Telegram."""
import ast
import asyncio
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from balance_ledger import admin_adjust, kopecks, rubles, MAX_KOPECKS, ensure_schema
from states import AdminDeposit, AdminWithdraw
from test_admin_form_navigation import State
from test_renewal_checkout import load_functions
import admin_form_navigation as form
import screen_navigation as nav


class AdminBalanceNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/"balance.db"
        with closing(sqlite3.connect(self.db)) as db,db:
            db.executescript("""
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,balance REAL);
                INSERT INTO users VALUES(42,10),(43,20);
                CREATE TABLE payments(payment_id TEXT PRIMARY KEY);
                CREATE TABLE payment_processing(payment_id TEXT PRIMARY KEY,refund_state TEXT);
            """)
            ensure_schema(db)
        self.bot=SimpleNamespace(id=100)
        self.admin=SimpleNamespace(id=99)
        self.chat=SimpleNamespace(id=99,type="private")
        self.screen=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=SimpleNamespace(id=100),
            message_id=7,reply_markup=None)
        self.message=SimpleNamespace(bot=self.bot,chat=self.chat,from_user=self.admin,
            text="",message_id=123,answer=AsyncMock())
        self.callback=SimpleNamespace(bot=self.bot,message=self.screen,from_user=self.admin,
            data="",answer=AsyncMock())
        self.state=State()
        self.next=AsyncMock()
        self.legacy={99:1}
        self.middleware=form.AdminFormNavigation(99)
        for obj,key,value in (
            (nav,"NAV_DB_PATH",Path(tmp.name)/"navigation.db"),
            (form,"render_screen",AsyncMock(side_effect=self.render_callback)),
            (form,"render_command",AsyncMock(side_effect=self.render_message)),
        ):
            p=patch.object(obj,key,value)
            p.start()
            self.addCleanup(p.stop)
        nav._save((100,99),7,"text")
        self.adjust=Mock(side_effect=admin_adjust)
        self.env=load_functions("handlers/admin.py",{
            "admin_balance_keyboard","render_admin_balance","admin_user_balance",
            "admin_balance_snapshot","admin_balance_start","admin_deposit_start","admin_withdraw_start",
            "admin_balance_operation","admin_deposit_process","admin_withdraw_process","admin_balance_apply"},
            dict(types=form.types,FSMContext=object,ADMIN_ID=99,DB_PATH=self.db,sqlite3=sqlite3,closing=closing,
                 asyncio=asyncio,logging=logging,admin_adjust=self.adjust,kopecks=kopecks,rubles=rubles,
                 MAX_KOPECKS=MAX_KOPECKS,AdminDeposit=AdminDeposit,AdminWithdraw=AdminWithdraw,
                 form_begin=form.begin,form_step=form.step,form_retry=form.retry,form_finish=form.finish,
                 private_admin=form.private_admin,callback_is_current=form.callback_is_current,
                 CONFIRM_TOKEN=form.CONFIRM_TOKEN,FORM_SCREEN=form.FORM_SCREEN,extend_temp=self.legacy,
                 render_screen=form.render_screen))

    async def render_callback(self,message,text,**kwargs):
        message.reply_markup=kwargs.get("reply_markup")
        return message

    async def render_message(self,message,text,**kwargs):
        self.screen.reply_markup=kwargs.get("reply_markup")

    def set_buttons(self,*values):
        self.screen.reply_markup=SimpleNamespace(inline_keyboard=[
            [SimpleNamespace(callback_data=v)] for v in values])

    def buttons(self):
        return [b.callback_data for row in self.screen.reply_markup.inline_keyboard for b in row]

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT telegram_id,balance FROM users ORDER BY telegram_id").fetchall()

    def journal(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT telegram_id,kind,delta_kopecks,before_kopecks,after_kopecks,actor_id,operation_id FROM balance_operations").fetchall()

    async def call(self,event,handler=None):
        return await self.middleware(handler or self.next,event,{"state":self.state,"raw_state":self.state.current})

    async def cb(self,value,name=None):
        self.callback.data=value
        handler=(lambda event,data:self.env[name](event,self.state)) if name else None
        return await self.call(self.callback,handler)

    async def start(self,withdraw=False,owner=42):
        kind="withdraw" if withdraw else "deposit"
        value=f"user_{kind}_{owner}"
        self.set_buttons(value)
        await self.cb(value,f"admin_{kind}_start")

    async def enter(self,value,withdraw=False):
        self.message.text=value
        name="admin_withdraw_process" if withdraw else "admin_deposit_process"
        await self.call(self.message,lambda event,data:self.env[name](event,self.state))

    async def prepare(self,value="3",withdraw=False,owner=42):
        await self.start(withdraw,owner)
        await self.enter(value,withdraw)
        return self.state.data[form.CONFIRM_TOKEN]

    async def apply(self,token=None):
        await self.cb(token or self.state.data[form.CONFIRM_TOKEN],"admin_balance_apply")

    async def test_start_scoped_to_owner(self):
        await self.start()
        self.assertEqual(self.state.current,AdminDeposit.amount.state)
        self.assertEqual(self.state.data["user_id"],42)
        self.assertEqual(self.buttons(),["user_balance_42"])
        self.assertEqual(self.legacy,{})
        self.adjust.assert_not_called()

    async def test_withdraw_prompt_scoped(self):
        await self.start(True,43)
        self.assertEqual(self.state.current,AdminWithdraw.amount.state)
        self.assertEqual(self.buttons(),["user_balance_43"])

    async def test_input_does_not_mutate_and_displays_amount(self):
        await self.prepare("1,25")
        self.assertEqual(self.rows(),[(42,10.0),(43,20.0)])
        self.assertEqual(self.journal(),[])
        self.assertEqual(self.state.data["amount"],"1.25")
        text=form.render_command.await_args.args[1]
        self.assertIn("+1.25",text)
        self.assertIn("42",text)
        self.adjust.assert_not_called()

    async def test_success_deposit_and_durable_identity(self):
        token=await self.prepare("1,25")
        await self.apply()
        self.assertEqual(self.rows(),[(42,11.25),(43,20.0)])
        self.assertEqual(self.journal(),[(42,"admin_credit",125,1000,1125,99,"admin-confirm:"+token)])
        self.assertIsNone(self.state.current)
        self.assertEqual(self.buttons(),["user_deposit_42","user_withdraw_42","user_back_to_42"])

    async def test_success_withdraw_exact_owner(self):
        token=await self.prepare("3",True,43)
        await self.apply()
        self.assertEqual(self.rows(),[(42,10.0),(43,17.0)])
        self.assertEqual(self.journal(),[(43,"admin_withdraw",-300,2000,1700,99,"admin-confirm:"+token)])

    async def test_repeated_confirmation_no_second_write(self):
        token=await self.prepare()
        await self.apply()
        await self.apply(token)
        self.adjust.assert_called_once()
        self.assertEqual(len(self.journal()),1)

    async def test_old_confirmation_rejected_after_back(self):
        token=await self.prepare()
        await self.cb(self.state.data[form.BACK_TOKEN])
        self.assertEqual(self.state.current,AdminDeposit.amount.state)
        self.assertIn("3.00",form.render_screen.await_args.args[1])
        await self.enter("4")
        await self.apply(token)
        self.adjust.assert_not_called()
        await self.apply()
        self.assertEqual(self.rows()[0],(42,14.0))

    async def test_invalid_amount_retries(self):
        await self.start()
        for value in ("abc","0","-1","NaN","Infinity","1.001","100000.01"):
            await self.enter(value)
            self.assertEqual(self.state.current,AdminDeposit.amount.state)
            self.assertEqual(self.buttons(),["user_balance_42"])
        self.adjust.assert_not_called()

    async def test_insufficient_amount_retries(self):
        await self.start(True)
        await self.enter("11",True)
        self.assertEqual(self.state.current,AdminWithdraw.amount.state)
        self.assertIn("Недостаточно",form.render_command.await_args.args[1])
        self.adjust.assert_not_called()

    async def test_concurrent_balance_change_refused_at_apply(self):
        await self.prepare("8",True)
        admin_adjust(self.db,"other",42,99,"5",withdraw=True)
        await self.apply()
        self.assertEqual(self.rows()[0],(42,5.0))
        self.assertEqual(len(self.journal()),1)
        self.assertIn("недоступна",form.render_screen.await_args.args[1])
        self.assertIsNone(self.state.current)

    async def test_refund_hold_still_blocks_withdraw(self):
        await self.prepare("1",True)
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("INSERT INTO balance_deposit_orders(payment_id,telegram_id,amount_kopecks,refunded_kopecks) VALUES('held',42,1000,100)")
            db.execute("INSERT INTO payment_processing VALUES('held','partial_review')")
        await self.apply()
        self.assertEqual(self.rows()[0],(42,10.0))
        self.assertEqual(self.journal(),[])
        self.assertIn("недоступна",form.render_screen.await_args.args[1])

    async def test_cancel_at_confirmation(self):
        await self.prepare()
        await self.cb("user_balance_42")
        self.assertIsNone(self.state.current)
        self.adjust.assert_not_called()

    async def test_wrong_owner_cancel_rejected(self):
        await self.prepare()
        await self.cb("user_balance_43")
        self.assertEqual(self.state.current,AdminDeposit.confirm.state)
        self.adjust.assert_not_called()

    async def test_cancel_command(self):
        await self.prepare()
        self.message.text="/cancel"
        await self.call(self.message)
        self.assertIsNone(self.state.current)
        self.assertEqual(self.buttons(),["user_balance_42"])
        self.assertTrue(form.render_command.await_args.kwargs["clean_cancel"])
        self.adjust.assert_not_called()

    async def test_menu_navigation_clears_form(self):
        await self.prepare()
        self.message.text="👤 Профиль"
        await self.call(self.message)
        self.assertIsNone(self.state.current)
        self.adjust.assert_not_called()

    async def test_unauthorized_start(self):
        self.callback.from_user=SimpleNamespace(id=43)
        await self.start()
        self.assertIsNone(self.state.current)
        self.adjust.assert_not_called()

    async def test_unauthorized_confirmation(self):
        await self.prepare()
        self.callback.from_user=SimpleNamespace(id=43)
        await self.apply()
        self.adjust.assert_not_called()

    async def test_stale_screen_confirmation(self):
        await self.prepare()
        nav._save((100,99),8,"text")
        await self.apply()
        self.adjust.assert_not_called()

    async def test_uncertain_result_only_review_navigation(self):
        token=await self.prepare()
        self.adjust.side_effect=RuntimeError()
        await self.apply()
        self.assertIsNone(self.state.current)
        self.assertEqual(self.buttons(),["user_balance_42","user_back_to_42"])
        await self.apply(token)
        self.adjust.assert_called_once()

    async def test_legacy_cancel_rejected(self):
        await self.prepare()
        await self.cb("admin_balance_cancel_deposit_42")
        self.assertEqual(self.state.current,AdminDeposit.confirm.state)
        self.adjust.assert_not_called()

    def test_profile_cancel_is_scoped_to_custom_deposit(self):
        tree=ast.parse((Path(__file__).parent/"handlers/profile.py").read_text(encoding="utf-8"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="deposit_custom_amount")
        self.assertIn("CustomDeposit.amount",ast.unparse(node.decorator_list[0]))
