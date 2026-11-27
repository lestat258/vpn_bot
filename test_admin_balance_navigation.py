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
                 "admin_deposit_process", "admin_withdraw_process"}
        nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        for n in nodes:
            n.decorator_list = []
        self.adjust = Mock(return_value=(1200, True))
        self.env = dict(types=SimpleNamespace(Message=object,
            InlineKeyboardButton=lambda **kw:kw, InlineKeyboardMarkup=lambda **kw:kw),
            FSMContext=object, ADMIN_ID=99, DB_PATH="unused", asyncio=asyncio,
            logging=logging, admin_adjust=self.adjust, kopecks=kopecks, rubles=rubles)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),"exec"),self.env)
        self.message = SimpleNamespace(text="3",from_user=SimpleNamespace(id=99),
            chat=SimpleNamespace(id=99),message_id=123,answer=AsyncMock())
        self.state = SimpleNamespace(get_data=AsyncMock(return_value={"user_id":42}),clear=AsyncMock())

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
