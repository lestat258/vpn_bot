import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from states import CustomDeposit, EnterPromocode
import screen_navigation as nav
import form_navigation as form
from balance_ledger import validate_deposit_amount, rubles
from test_renewal_checkout import load_functions


class DepositFormTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher=patch.object(nav,"NAV_DB_PATH",Path(tmp.name)/"nav.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot=SimpleNamespace(id=100)
        self.chat=SimpleNamespace(id=42,type="private")
        self.owner=SimpleNamespace(id=42)
        self.screen=SimpleNamespace(chat=self.chat,bot=self.bot,from_user=SimpleNamespace(id=100),message_id=7)
        self.message=SimpleNamespace(chat=self.chat,bot=self.bot,from_user=self.owner,message_id=8,text="12,50",answer=AsyncMock())
        self.callback=SimpleNamespace(message=self.screen,from_user=self.owner,data="deposit_custom",answer=AsyncMock())
        self.state=SimpleNamespace(get_state=AsyncMock(return_value=None),get_data=AsyncMock(return_value={}),
            set_data=AsyncMock(),set_state=AsyncMock(),clear=AsyncMock(),update_data=AsyncMock())
        nav._save((100,42),7,"text")
        self.render=AsyncMock(return_value=self.screen)
        self.command=AsyncMock(return_value=self.screen)
        self.pay=AsyncMock(return_value="payment keyboard")
        self.next=AsyncMock(return_value="handled")
        self.env=load_functions("handlers/profile.py",
            {"deposit_custom","cancel_custom_deposit","deposit_custom_amount"},
            dict(types=SimpleNamespace(CallbackQuery=object,Message=object),FSMContext=object,
                 CustomDeposit=CustomDeposit,FORM_SCREEN=form.FORM_SCREEN,is_current_screen=form.is_current_screen,
                 current_deposit_input=form.current_deposit_input,render_screen=self.render,
                 render_command=self.command,DEPOSIT_PROMPT="Prompt",get_back_keyboard=lambda x:x,
                 deposit_menu_content=lambda owner,notice="":(notice,"menu keyboard"),
                 validate_deposit_amount=validate_deposit_amount,rubles=rubles,create_deposit_payment=self.pay,
                 logging=SimpleNamespace(error=lambda *a:None)))

    def waiting(self):
        self.state.get_state.return_value=CustomDeposit.amount.state
        self.state.get_data.return_value={form.FORM_SCREEN:7}

    async def test_start_binds_current_screen(self):
        await self.env["deposit_custom"](self.callback,self.state)
        self.state.set_data.assert_awaited_once_with({form.FORM_SCREEN:7})
        self.state.set_state.assert_awaited_once_with(CustomDeposit.amount)
        self.render.assert_awaited_once()

    async def test_stale_start_rejected(self):
        self.screen.message_id=5
        await self.env["deposit_custom"](self.callback,self.state)
        self.render.assert_not_awaited()
        self.state.set_state.assert_not_awaited()

    async def test_invalid_amount_updates_screen_and_keeps_state(self):
        self.waiting()
        self.message.text="1"
        await self.env["deposit_custom_amount"](self.message,self.state)
        self.command.assert_awaited_once()
        self.assertIn("Введите сумму",self.command.await_args.args[1])
        self.state.clear.assert_not_awaited()
        self.pay.assert_not_awaited()

    async def test_valid_amount_creates_payment_once_and_updates_screen(self):
        self.waiting()
        await self.env["deposit_custom_amount"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.pay.assert_awaited_once_with(self.bot,42,1250,"message:42:8")
        self.assertIn("12.50",self.command.await_args.args[1])
        self.message.answer.assert_not_awaited()

    async def test_cancel_command_restores_menu(self):
        self.waiting()
        self.message.text="/cancel"
        await self.env["deposit_custom_amount"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.command.assert_awaited_once_with(self.message,"Пополнение отменено.",reply_markup="menu keyboard",parse_mode="HTML",clean_cancel=True)
        self.pay.assert_not_awaited()

    async def test_cancel_button_restores_menu(self):
        self.waiting()
        self.callback.data="cancel_custom_deposit"
        await self.env["cancel_custom_deposit"](self.callback,self.state)
        self.state.clear.assert_awaited_once()
        self.render.assert_awaited_once()
        self.pay.assert_not_awaited()

    async def test_old_amount_after_screen_change_never_creates_payment(self):
        self.waiting()
        nav._save((100,42),9,"text")
        await self.env["deposit_custom_amount"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.pay.assert_not_awaited()
        self.command.assert_not_awaited()

    async def test_navigation_abandons_input_before_handler(self):
        self.waiting()
        self.message.text="👤 Профиль"
        data={"state":self.state,"raw_state":CustomDeposit.amount.state}
        result=await form.UserFormNavigation()(self.next,self.message,data)
        self.assertEqual(result,"handled")
        self.state.clear.assert_awaited_once()
        self.assertIsNone(data["raw_state"])

    async def test_promo_navigation_clears_old_raw_state(self):
        self.state.get_state.return_value=EnterPromocode.waiting.state
        self.message.text="/profile"
        data={"state":self.state,"raw_state":EnterPromocode.waiting.state}
        await form.UserFormNavigation()(self.next,self.message,data)
        self.state.clear.assert_awaited_once()
        self.assertIsNone(data["raw_state"])

    async def test_stale_callback_cannot_create_payment_during_form(self):
        self.waiting()
        self.callback.data="deposit_100"
        await form.UserFormNavigation()(self.next,self.callback,{"state":self.state})
        self.next.assert_not_awaited()
        self.callback.answer.assert_awaited_once()

    async def test_valid_cancel_callback_reaches_handler(self):
        self.waiting()
        self.callback.data="cancel_custom_deposit"
        await form.UserFormNavigation()(self.next,self.callback,{"state":self.state})
        self.next.assert_awaited_once()

    async def test_provider_failure_returns_back_without_retry(self):
        self.waiting()
        self.pay.side_effect=RuntimeError("provider error")
        await self.env["deposit_custom_amount"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.pay.assert_awaited_once()
        self.assertEqual(self.command.await_args.kwargs["reply_markup"],"deposit")

    async def test_admin_state_untouched(self):
        self.state.get_state.return_value="AdminDeposit:amount"
        self.message.text="10"
        await form.UserFormNavigation()(self.next,self.message,{"state":self.state})
        self.state.clear.assert_not_awaited()
        self.next.assert_awaited_once()
