import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from balance_ledger import kopecks, rubles


class AdminBalanceNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).parent / "handlers/admin.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"admin_balance_keyboard", "admin_balance_operation",
                 "admin_deposit_process", "admin_withdraw_process",
                 "admin_balance_cancel_keyboard", "admin_balance_cancel",
                 "admin_deposit_start", "admin_withdraw_start"}
        nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        for n in nodes:
            n.decorator_list = []
        self.adjust = Mock(return_value=(1200, True))
        self.env = dict(types=SimpleNamespace(Message=object, CallbackQuery=object,
            InlineKeyboardButton=lambda **kw:kw, InlineKeyboardMarkup=lambda **kw:kw),
            FSMContext=object, ADMIN_ID=99, DB_PATH="unused", asyncio=asyncio,
            logging=logging, admin_adjust=self.adjust, kopecks=kopecks, rubles=rubles,
            AdminDeposit=SimpleNamespace(amount=SimpleNamespace(state="AdminDeposit:amount")),
            AdminWithdraw=SimpleNamespace(amount=SimpleNamespace(state="AdminWithdraw:amount")),
            render_admin_balance=AsyncMock())
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),"exec"),self.env)
        self.message = SimpleNamespace(text="3",from_user=SimpleNamespace(id=99),
            chat=SimpleNamespace(id=99),message_id=123,answer=AsyncMock())
        self.state = SimpleNamespace(get_data=AsyncMock(return_value={"user_id":42}),clear=AsyncMock(),
            get_state=AsyncMock(return_value="AdminDeposit:amount"),update_data=AsyncMock(),set_state=AsyncMock())
        self.callback=SimpleNamespace(data="admin_balance_cancel_deposit_42",
            from_user=SimpleNamespace(id=99),answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(),answer=AsyncMock()))

    def buttons(self):
        return [r[0]["callback_data"] for r in self.message.answer.await_args.kwargs["reply_markup"]["inline_keyboard"]]

    async def test_success_withdraw_returns_same_user_controls(self):
        await self.env["admin_withdraw_process"](self.message,self.state)
        self.adjust.assert_called_once_with("unused","admin:99:123",42,99,"3",withdraw=True)
        self.state.clear.assert_awaited_once()
        self.assertIn("12.00",self.message.answer.await_args.args[0])
        self.assertEqual(self.buttons(),["user_deposit_42","user_withdraw_42","user_back_to_42"])

    async def test_success_deposit_returns_controls(self):
        await self.env["admin_deposit_process"](self.message,self.state)
        self.adjust.assert_called_once_with("unused","admin:99:123",42,99,"3",withdraw=False)
        self.state.clear.assert_awaited_once()
        self.assertEqual(self.buttons(),["user_deposit_42","user_withdraw_42","user_back_to_42"])

    async def test_declined_withdraw_returns_controls(self):
        self.adjust.side_effect=ValueError("Insufficient")
        await self.env["admin_withdraw_process"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.assertIn("недоступна",self.message.answer.await_args.args[0])
        self.assertEqual(self.buttons(),["user_deposit_42","user_withdraw_42","user_back_to_42"])

    async def test_cancel_returns_controls_without_mutation(self):
        self.message.text="/cancel"
        await self.env["admin_deposit_process"](self.message,self.state)
        self.adjust.assert_not_called()
        self.state.clear.assert_awaited_once()
        self.assertEqual(self.buttons(),["user_deposit_42","user_withdraw_42","user_back_to_42"])

    async def test_uncertain_result_only_read_navigation(self):
        self.adjust.side_effect=RuntimeError()
        await self.env["admin_withdraw_process"](self.message,self.state)
        self.state.clear.assert_awaited_once()
        self.assertEqual(self.buttons(),["user_balance_42","user_back_to_42"])

    async def test_unauthorized_no_mutation_or_controls(self):
        self.message.from_user.id=100
        await self.env["admin_deposit_process"](self.message,self.state)
        self.adjust.assert_not_called()
        self.state.clear.assert_awaited_once()
        self.assertNotIn("reply_markup",self.message.answer.await_args.kwargs)


    async def test_cancel_deposit_button_clears_input_and_returns_balance(self):
        await self.env["admin_balance_cancel"](self.callback,self.state)
        self.state.clear.assert_awaited_once()
        self.env["render_admin_balance"].assert_awaited_once_with(self.callback,42,canceled=True)
        self.adjust.assert_not_called()

    async def test_cancel_withdraw_button_clears_input(self):
        self.callback.data="admin_balance_cancel_withdraw_42"
        self.state.get_state.return_value="AdminWithdraw:amount"
        await self.env["admin_balance_cancel"](self.callback,self.state)
        self.state.clear.assert_awaited_once()
        self.env["render_admin_balance"].assert_awaited_once_with(self.callback,42,canceled=True)
        self.adjust.assert_not_called()

    async def test_stale_or_wrong_owner_cancel_cannot_clear_other_state(self):
        for value in ("admin_balance_cancel_deposit_43","admin_balance_cancel_withdraw_42","admin_balance_cancel_bad_42"):
            self.callback.data=value
            await self.env["admin_balance_cancel"](self.callback,self.state)
        self.state.clear.assert_not_awaited()
        self.env["render_admin_balance"].assert_not_awaited()
        self.adjust.assert_not_called()

    async def test_unauthorized_cancel(self):
        self.callback.from_user.id=100
        await self.env["admin_balance_cancel"](self.callback,self.state)
        self.state.clear.assert_not_awaited()
        self.env["render_admin_balance"].assert_not_awaited()

    async def test_deposit_prompt_has_cancel_button(self):
        self.callback.data="user_deposit_42"
        await self.env["admin_deposit_start"](self.callback,self.state)
        markup=self.callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"],"admin_balance_cancel_deposit_42")

    async def test_withdraw_prompt_has_cancel_button(self):
        self.callback.data="user_withdraw_42"
        await self.env["admin_withdraw_start"](self.callback,self.state)
        markup=self.callback.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"],"admin_balance_cancel_withdraw_42")

    def test_profile_cancel_is_scoped_to_custom_deposit(self):
        tree=ast.parse((Path(__file__).parent/"handlers/profile.py").read_text(encoding="utf-8"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="cancel_deposit")
        self.assertIn("CustomDeposit.amount",ast.unparse(node.decorator_list[0]))
