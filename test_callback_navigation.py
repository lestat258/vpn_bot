"""Versioned callback guard, using isolated navigation SQLite and fake Telegram."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import screen_navigation as nav
import callback_navigation as guard


class Object(SimpleNamespace):
    def model_copy(self, *, update):
        return type(self)(**{**vars(self), **update})


class CallbackNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(nav, "NAV_DB_PATH", Path(tmp.name) / "navigation.db")
        p.start()
        self.addCleanup(p.stop)
        self.key = (100, 42)
        self.bot = Object(id=100, edit_message_text=AsyncMock(), delete_message=AsyncMock())
        self.message = Object(bot=self.bot, chat=Object(id=42, type="private"),
                              from_user=Object(id=100), message_id=7, text="Menu",
                              reply_markup=None, edit_text=AsyncMock(), answer=AsyncMock(),
                              delete=AsyncMock(), edit_reply_markup=AsyncMock())
        self.handler = AsyncMock()
        self.middleware = guard.CurrentCallbackNavigation()

    def keyboard(self, *values):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=v, callback_data=v)] for v in values
        ] + [[InlineKeyboardButton(text="Pay", url="https://example.invalid/pay")]])

    def publish(self, *values, mid=7):
        kwargs, mapping = guard.prepare_buttons({"reply_markup": self.keyboard(*values)})
        guard.invalidate(self.key)
        nav._save(self.key, mid, "text")
        guard.remember(self.key, mid, mapping)
        self.message.message_id = mid
        self.message.reply_markup = kwargs["reply_markup"]
        return list(mapping)

    def event(self, value, **updates):
        return Object(**{"data": value, "message": self.message,
                         "from_user": Object(id=42), "answer": AsyncMock(), **updates})

    async def dispatch(self, event):
        return await self.middleware(self.handler, event, {})

    async def rejected(self, event):
        before = self.handler.await_count
        await self.dispatch(event)
        self.assertEqual(self.handler.await_count, before)
        event.answer.assert_awaited_once_with(guard.STALE, show_alert=True)

    async def test_current_decoded_for_existing_handlers(self):
        token, other = self.publish("admin_balance_apply_test", "user_balance_42")
        await self.dispatch(self.event(token))
        decoded = self.handler.await_args.args[0]
        self.assertEqual(decoded.data, "admin_balance_apply_test")
        self.assertTrue(guard.displayed(decoded.message, decoded.data))
        self.assertTrue(guard.displayed(decoded.message, "user_balance_42"))
        self.assertEqual(decoded.message.reply_markup.inline_keyboard[-1][0].url,
                         "https://example.invalid/pay")

    async def test_duplicate_consumed(self):
        token, = self.publish("toggle_auto_renew_1")
        await self.dispatch(self.event(token))
        await self.rejected(self.event(token))

    async def test_other_current_button_remains_available(self):
        a,b = self.publish("read_one", "read_two")
        await self.dispatch(self.event(a))
        await self.dispatch(self.event(b))
        self.assertEqual(self.handler.await_count, 2)

    async def test_old_generation_same_message_and_action(self):
        old, = self.publish("toggle_auto_renew_1")
        previous = self.message.model_copy(update={})
        new, = self.publish("toggle_auto_renew_1")
        self.assertNotEqual(old, new)
        await self.rejected(self.event(old, message=previous))
        await self.dispatch(self.event(new))
        self.handler.assert_awaited_once()

    async def test_previous_message_rejected(self):
        old, = self.publish("admin_delete_tariff_execute_1")
        previous = self.message.model_copy(update={})
        self.publish("admin_tariffs", mid=8)
        await self.rejected(self.event(old, message=previous))

    async def test_home_invalidates_tokens(self):
        token, = self.publish("user_block_43")
        guard.invalidate(self.key)
        await self.rejected(self.event(token))

    async def test_restart_reads_persisted_tokens(self):
        token, = self.publish("my_keys")
        self.middleware = guard.CurrentCallbackNavigation()
        await self.dispatch(self.event(token))
        self.handler.assert_awaited_once()
        self.middleware = guard.CurrentCallbackNavigation()
        await self.rejected(self.event(token))

    async def test_wrong_actor_rejected_without_consuming(self):
        token, = self.publish("admin_backup_menu")
        await self.rejected(self.event(token, from_user=Object(id=43)))
        await self.dispatch(self.event(token))
        self.handler.assert_awaited_once()

    async def test_wrong_bot_rejected(self):
        token, = self.publish("my_keys")
        msg = self.message.model_copy(update={"from_user": Object(id=999)})
        await self.rejected(self.event(token, message=msg))

    async def test_group_and_missing_message_rejected(self):
        token, = self.publish("my_keys")
        msg = self.message.model_copy(update={"chat": Object(id=42,type="group")})
        await self.rejected(self.event(token,message=msg))
        await self.rejected(self.event(token,message=None))

    async def test_forged_or_absent_button_rejected(self):
        token, = self.publish("my_keys")
        await self.rejected(self.event("nav:forged"))
        msg = self.message.model_copy(update={"reply_markup": self.keyboard("other")})
        await self.rejected(self.event(token,message=msg))

    async def test_legacy_mutation_never_bypasses_guard(self):
        self.message.reply_markup = self.keyboard("admin_balance_apply_old")
        await self.rejected(self.event("admin_balance_apply_old"))

    async def test_paid_guide_notification_allowed(self):
        self.message.reply_markup = self.keyboard("paid_key_guide")
        await self.dispatch(self.event("paid_key_guide"))
        self.handler.assert_awaited_once()

    async def test_trial_notification_allowed(self):
        self.message.text = "✅ Пробная подписка создана!\nКлюч"
        self.message.reply_markup = self.keyboard("my_keys", "back_to_main")
        await self.dispatch(self.event("my_keys"))
        await self.dispatch(self.event("back_to_main"))
        self.assertEqual(self.handler.await_count, 2)

    async def test_reminder_notification_allowed_but_old_menu_not(self):
        self.message.reply_markup = self.keyboard("back_to_tariffs")
        await self.rejected(self.event("back_to_tariffs"))
        self.message.text = "🚨 Ваша подписка истекает завтра!"
        await self.dispatch(self.event("back_to_tariffs"))
        self.handler.assert_awaited_once()

    async def test_navigation_db_error_fails_closed(self):
        token, = self.publish("admin_backup_menu")
        with patch.object(guard,"consume",side_effect=__import__("sqlite3").OperationalError("locked")):
            await self.rejected(self.event(token))

    async def test_renderer_rotates_tokens_on_in_place_edit(self):
        old, = self.publish("my_keys")
        old_message = self.message.model_copy(update={})
        self.message.edit_text.return_value = self.message
        await nav.render_screen(self.message,"Next",reply_markup=self.keyboard("my_keys"))
        markup = self.message.edit_text.await_args.kwargs["reply_markup"]
        new = markup.inline_keyboard[0][0].callback_data
        self.message.reply_markup = markup
        await self.rejected(self.event(old,message=old_message))
        await self.dispatch(self.event(new))
        self.handler.assert_awaited_once()

    async def test_command_edit_rotates_tokens(self):
        old, = self.publish("my_keys")
        previous = self.message.model_copy(update={})
        command = Object(bot=self.bot,chat=self.message.chat,from_user=Object(id=42),
                         text="/profile",answer=AsyncMock(),delete=AsyncMock())
        self.bot.edit_message_text.return_value = self.message
        await nav.render_command(command,"Profile",reply_markup=self.keyboard("my_keys"))
        markup = self.bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.message.reply_markup = markup
        await self.rejected(self.event(old,message=previous))
        await self.dispatch(self.event(markup.inline_keyboard[0][0].callback_data))
        self.handler.assert_awaited_once()

    async def test_ambiguous_render_disables_previous_token(self):
        old, = self.publish("my_keys")
        from aiogram.exceptions import TelegramNetworkError
        self.message.edit_text.side_effect = TelegramNetworkError(method=None,message="timeout")
        with self.assertRaises(TelegramNetworkError):
            await nav.render_screen(self.message,"Next",reply_markup=self.keyboard("my_keys"))
        await self.rejected(self.event(old))

    def test_tokens_fit_telegram_limit_and_preserve_url(self):
        original = self.keyboard("a"*64)
        kwargs, mapping = guard.prepare_buttons({"reply_markup":original})
        self.assertEqual(list(mapping.values()),["a"*64])
        self.assertLessEqual(len(next(iter(mapping)).encode()),64)
        self.assertEqual(original.inline_keyboard[0][0].callback_data,"a"*64)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[-1][0].url,"https://example.invalid/pay")
