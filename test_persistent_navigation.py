import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import screen_navigation as nav


class PersistentNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p=patch.object(nav,"NAV_DB_PATH",Path(tmp.name)/"nav.db")
        p.start()
        self.addCleanup(p.stop)
        self.bot=SimpleNamespace(id=100,edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock())
        self.chat=SimpleNamespace(id=42,type="private")
        self.key=(100,42)

    def message(self,mid=1,*,command=False,text="Menu",result_id=2,markup=None):
        result=SimpleNamespace(message_id=result_id)
        return SimpleNamespace(chat=self.chat,bot=self.bot,message_id=mid,
            from_user=SimpleNamespace(id=42 if command else 100),text=text,caption=None,
            reply_markup=markup,edit_text=AsyncMock(return_value=SimpleNamespace(message_id=mid)),
            answer=AsyncMock(return_value=result),delete=AsyncMock(),
            edit_reply_markup=AsyncMock(),edit_media=AsyncMock(return_value=SimpleNamespace(message_id=mid)),
            answer_photo=AsyncMock(return_value=result))

    async def test_command_creates_then_reuses_screen(self):
        user=self.message(command=True,result_id=9)
        await nav.render_command(user,"Profile")
        self.assertEqual(nav._read(self.key),(9,"text"))
        await nav.render_command(user,"Buy")
        self.bot.edit_message_text.assert_awaited_once_with(text="Buy",chat_id=42,message_id=9)
        user.answer.assert_awaited_once()
        user.delete.assert_not_awaited()

    async def test_tracking_callback_and_command_share_screen(self):
        m=self.message(mid=7)
        await nav.render_screen(m,"Profile")
        self.assertEqual(nav._read(self.key),(7,"text"))
        await nav.render_command(self.message(command=True),"Buy")
        self.bot.edit_message_text.assert_awaited_once_with(text="Buy",chat_id=42,message_id=7)

    async def test_qr_then_command_replaces_known_photo(self):
        m=self.message(mid=7)
        await nav.render_photo_screen(m,object())
        self.assertEqual(nav._read(self.key),(7,"photo"))
        user=self.message(command=True,result_id=10)
        await nav.render_command(user,"Buy")
        self.bot.delete_message.assert_awaited_once_with(chat_id=42,message_id=7)
        self.assertEqual(nav._read(self.key),(10,"text"))
        user.delete.assert_not_awaited()

    async def test_reply_keyboard_replaces_screen(self):
        nav._save(self.key,7,"text")
        kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Profile")]])
        await nav.render_command(self.message(command=True,result_id=10),"Menu",reply_markup=kb)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(chat_id=42,message_id=7)

    async def test_unavailable_edit_replaces_screen(self):
        nav._save(self.key,7,"text")
        self.bot.edit_message_text.side_effect=TelegramBadRequest(method=None,message="message to edit not found")
        await nav.render_command(self.message(command=True,result_id=10),"Buy")
        self.assertEqual(nav._read(self.key),(10,"text"))

    async def test_failed_edit_not_replayed(self):
        nav._save(self.key,7,"text")
        self.bot.edit_message_text.side_effect=TelegramNetworkError(method=None,message="timeout")
        user=self.message(command=True)
        with self.assertRaises(TelegramNetworkError):
            await nav.render_command(user,"Buy")
        user.answer.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual(nav._read(self.key),(7,"text"))

    async def test_notification_preserved_and_old_menu_retired(self):
        nav._save(self.key,7,"text")
        m=self.message(mid=8,text="✅ Подписка продлена!",result_id=10)
        await nav.render_screen(m,"Guide")
        m.edit_text.assert_not_awaited()
        m.delete.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(chat_id=42,message_id=7)
        self.assertEqual(nav._read(self.key),(10,"text"))

    async def test_callback_replacement_no_duplicate_delete(self):
        nav._save(self.key,7,"photo")
        m=self.message(mid=7,text=None,result_id=10)
        await nav.render_screen(m,"Menu")
        m.delete.assert_awaited_once()
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual(nav._read(self.key),(10,"text"))

    async def test_chat_and_bot_isolation_and_persistence(self):
        nav._save(self.key,7,"text")
        nav._save((101,42),8,"photo")
        nav._save((100,43),9,"text")
        self.assertEqual(nav._read(self.key),(7,"text"))
        self.assertEqual(nav._read((101,42)),(8,"photo"))
        self.assertEqual(nav._read((100,43)),(9,"text"))
        self.assertIsNone(nav._read((101,43)))

    async def test_non_private_command_is_not_tracked(self):
        user=self.message(command=True)
        user.chat.type="group"
        await nav.render_command(user,"Menu")
        self.assertIsNone(nav._read(self.key))
        user.delete.assert_not_awaited()
