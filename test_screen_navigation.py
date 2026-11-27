import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from screen_navigation import render_screen


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    def message(self, *, text="Menu", caption=None, markup=None):
        return SimpleNamespace(text=text,caption=caption,reply_markup=markup,
            edit_text=AsyncMock(return_value="edited"),answer=AsyncMock(return_value="new"),
            delete=AsyncMock(),edit_reply_markup=AsyncMock())

    async def test_text_edit_no_new_message(self):
        m=self.message()
        self.assertEqual(await render_screen(m,"Next"),"edited")
        m.edit_text.assert_awaited_once_with("Next")
        m.answer.assert_not_awaited()
        m.delete.assert_not_awaited()

    async def test_no_change_no_new_message(self):
        m=self.message()
        m.edit_text.side_effect=TelegramBadRequest(method=None,message="message is not modified")
        self.assertIs(await render_screen(m,"Menu"),m)
        m.answer.assert_not_awaited()

    async def test_media_replacement_send_before_delete(self):
        m=self.message(text=None,caption="Key")
        order=[]
        async def send(*a,**kw):
            order.append("send")
            return "new"
        async def delete():
            order.append("delete")
        m.answer.side_effect=send
        m.delete.side_effect=delete
        self.assertEqual(await render_screen(m,"Next"),"new")
        self.assertEqual(order,["send","delete"])
        m.edit_text.assert_not_awaited()

    async def test_reply_keyboard_requires_new_message(self):
        m=self.message()
        kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Profile")]])
        await render_screen(m,"Main",reply_markup=kb)
        m.answer.assert_awaited_once_with("Main",reply_markup=kb)
        m.delete.assert_awaited_once()
        m.edit_text.assert_not_awaited()

    async def test_failed_send_preserves_old_message(self):
        m=self.message(text=None)
        m.answer.side_effect=TelegramNetworkError(method=None,message="timeout")
        with self.assertRaises(TelegramNetworkError):
            await render_screen(m,"Next")
        m.delete.assert_not_awaited()
        m.answer.assert_awaited_once()

    async def test_unexpected_edit_error_not_replayed(self):
        m=self.message()
        m.edit_text.side_effect=TelegramBadRequest(method=None,message="can't parse entities")
        with self.assertRaises(TelegramBadRequest):
            await render_screen(m,"Next")
        m.answer.assert_not_awaited()
        m.delete.assert_not_awaited()

    async def test_edit_unavailable_replaced(self):
        m=self.message()
        m.edit_text.side_effect=TelegramBadRequest(method=None,message="message can't be edited")
        await render_screen(m,"Next")
        m.answer.assert_awaited_once()
        m.delete.assert_awaited_once()

    async def test_failed_retirement_disables_old_buttons(self):
        m=self.message(text=None)
        m.delete.side_effect=TelegramBadRequest(method=None,message="message can't be deleted")
        self.assertEqual(await render_screen(m,"Next"),"new")
        m.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_issued_key_notifications_are_preserved(self):
        for m in (self.message(text="✅ Пробная подписка создана!"),
                  self.message(text=None,caption="✅ Подписка продлена!"),
                  self.message(markup=SimpleNamespace(inline_keyboard=[[SimpleNamespace(callback_data="paid_key_guide")]]))):
            await render_screen(m,"Next")
            m.answer.assert_awaited_once()
            m.edit_text.assert_not_awaited()
            m.delete.assert_not_awaited()
