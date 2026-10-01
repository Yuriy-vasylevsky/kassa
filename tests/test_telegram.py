import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Update
from telegram.error import BadRequest, NetworkError

from kassa.bot import TelegramBot, build_application
from kassa.config import Settings
from kassa.service import CashService
from kassa.storage import State, Store

OWNER = 752963390


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cash.sqlite3"
        self.store = Store(self.path)
        self.service = CashService(self.store, frozenset({OWNER}))
        self.transport = TelegramBot(self.service)
        self.bot = AsyncMock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=100)
        self.context = SimpleNamespace(bot=self.bot)

    def message(self, text, update_id=1, user=OWNER, chat_type="private"):
        return Update.de_json({"update_id": update_id, "message": {
            "message_id": update_id, "date": 0, "text": text,
            "from": {"id": user, "is_bot": False, "first_name": "User"},
            "chat": {"id": user if chat_type == "private" else -42, "type": chat_type},
        }}, self.bot)

    def callback(self, action, update_id=2, user=OWNER, message_id=100, reply_markup=None):
        return Update.de_json({"update_id": update_id, "callback_query": {
            "id": str(update_id), "chat_instance": "instance", "data": action,
            "from": {"id": user, "is_bot": False, "first_name": "User"},
            "message": {"message_id": message_id, "date": 0,
                        "chat": {"id": user, "type": "private"}, "reply_markup": reply_markup},
        }}, self.bot)

    async def test_start_buttons_value_and_report(self):
        await self.transport.handle(self.message("/start"), self.context)
        self.assertIn("💰 КАСА", self.bot.send_message.call_args.kwargs["text"])
        with self.store.transaction() as tx:
            self.assertEqual(tx.state(OWNER).main_message_id, 100)
        await self.transport.handle(self.callback("edit:received"), self.context)
        self.bot.answer_callback_query.assert_awaited()
        await self.transport.handle(self.message("123,45", update_id=3), self.context)
        with self.store.transaction() as tx:
            self.assertEqual(tx.cash()["received"], 12345)
        self.bot.delete_message.assert_awaited()
        await self.transport.handle(self.callback("report", update_id=4), self.context)
        self.assertIn("Отримав  123.45", self.bot.send_message.call_args_list[-2].kwargs["text"])
        self.assertNotIn("parse_mode", self.bot.send_message.call_args_list[-2].kwargs)
        sent = self.bot.send_message.call_args_list[-2].kwargs
        buttons = sent["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].text, "📋 Скопіювати")
        self.assertEqual(buttons[0][0].copy_text.text, sent["text"])
        self.assertIsNone(buttons[0][0].callback_data)
        self.assertEqual(buttons[1][0].callback_data, "report_delete")

    async def test_report_delete_preserves_cash_and_dashboard(self):
        await self.transport.handle(self.message("/start"), self.context)
        with self.store.transaction() as tx:
            tx.update_fields({"received": 12345})
        await self.transport.handle(self.message("/report", update_id=2), self.context)
        markup = self.bot.send_message.call_args_list[-2].kwargs["reply_markup"].to_dict()
        self.bot.reset_mock()
        update = self.callback("report_delete", update_id=3, message_id=101, reply_markup=markup)
        await self.transport.handle(update, self.context)
        self.assertEqual(self.bot.delete_message.call_args.kwargs["message_id"], 101)
        self.assertEqual(self.bot.delete_message.call_args.kwargs["chat_id"], OWNER)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        with self.store.transaction() as tx:
            self.assertEqual(tx.cash()["received"], 12345)
            self.assertEqual(tx.state(OWNER).main_message_id, 100)

    async def test_persistent_history_menu_and_saved_report(self):
        await self.transport.handle(self.message("/start"), self.context)
        menu = self.bot.send_message.call_args_list[0].kwargs["reply_markup"]
        self.assertTrue(menu.is_persistent)
        self.assertEqual(menu.keyboard[0][1].text, "📚 Історія звітів")
        await self.transport.handle(self.message("/report", update_id=2), self.context)
        saved = self.bot.send_message.call_args_list[-2].kwargs["text"]
        await self.transport.handle(self.message("📚 Історія звітів", update_id=3), self.context)
        view = self.bot.send_message.call_args.kwargs
        self.assertIn("Чистий дохід за місяць", view["text"])
        action = view["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertTrue(action.startswith("saved:"))
        sent_count = self.bot.send_message.await_count
        await self.transport.handle(self.callback(action, update_id=4), self.context)
        self.assertEqual(self.bot.send_message.await_count, sent_count)
        opened = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual(opened["text"], saved)
        buttons = opened["reply_markup"].inline_keyboard
        self.assertEqual(len(buttons), 1)
        self.assertEqual(len(buttons[0]), 1)
        self.assertEqual(buttons[0][0].text, "⬅️ Назад")
        self.assertEqual(buttons[0][0].callback_data, "history:0")
        await self.transport.handle(self.callback("history:0", update_id=6), self.context)
        self.assertIn("Історія звітів", self.bot.edit_message_text.call_args.kwargs["text"])
        await self.transport.handle(self.message("💰 Каса", update_id=5), self.context)
        self.assertIn("💰 КАСА", self.bot.send_message.call_args.kwargs["text"])

    async def test_menu_inputs_and_old_panel_are_deleted(self):
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=value) for value in (90, 100, 110)]
        await self.transport.handle(self.message("/start"), self.context)
        deleted = [call.kwargs["message_id"] for call in self.bot.delete_message.call_args_list]
        self.assertIn(1, deleted)
        self.assertIn(90, deleted)
        self.bot.reset_mock()
        await self.transport.handle(self.message("📚 Історія звітів", update_id=2), self.context)
        deleted = [call.kwargs["message_id"] for call in self.bot.delete_message.call_args_list]
        self.assertEqual(deleted, [2, 100])
        with self.store.transaction() as tx:
            self.assertEqual(tx.state(OWNER).main_message_id, 110)
        self.bot.edit_message_text.assert_not_awaited()
        await self.transport.handle(self.callback("back", update_id=3, message_id=110), self.context)
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs["message_id"], 110)

    async def test_relocation_send_failure_keeps_old_panel(self):
        with self.store.transaction() as tx:
            tx.save(State(OWNER, main_message_id=90))
        self.bot.send_message.side_effect = NetworkError("Offline")
        with self.assertRaises(NetworkError):
            await self.transport.refresh(self.bot, OWNER, move_to_bottom=True)
        self.bot.delete_message.assert_not_awaited()
        with self.store.transaction() as tx:
            self.assertEqual(tx.state(OWNER).main_message_id, 90)

    async def test_cleanup_failure_does_not_lose_new_panel(self):
        with self.store.transaction() as tx:
            tx.save(State(OWNER, main_message_id=90))
        self.bot.delete_message.side_effect = BadRequest("Message can't be deleted")
        await self.transport.handle(self.message("💰 Каса"), self.context)
        with self.store.transaction() as tx:
            self.assertEqual(tx.state(OWNER).main_message_id, 100)

    async def test_report_delete_rejects_dashboard_and_unrelated_messages(self):
        await self.transport.handle(self.message("/start"), self.context)
        await self.transport.send_report(self.bot, OWNER, "Звіт")
        markup = self.bot.send_message.call_args.kwargs["reply_markup"].to_dict()
        self.bot.reset_mock()
        await self.transport.handle(self.callback("report_delete", reply_markup=markup), self.context)
        await self.transport.handle(self.callback("report_delete", message_id=101), self.context)
        await self.transport.handle(self.callback("report_delete", user=999, message_id=101,
                                                   reply_markup=markup), self.context)
        self.bot.delete_message.assert_not_awaited()

    async def test_long_report_is_complete_and_copy_limit_is_respected(self):
        await self.transport.send_report(self.bot, OWNER, "я" * 256)
        self.assertEqual(len(self.bot.send_message.call_args.kwargs["reply_markup"]
                             .inline_keyboard[0][0].copy_text.text), 256)
        text = "<&>\n" + "Звіт\n" * 70
        await self.transport.send_report(self.bot, OWNER, text)
        sent = self.bot.send_message.call_args.kwargs
        self.assertNotIn("parse_mode", sent)
        self.assertEqual(sent["text"], text)
        buttons = sent["reply_markup"].inline_keyboard
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0][0].callback_data, "report_copy_help")
        self.assertIsNone(buttons[0][0].copy_text)
        self.assertEqual(buttons[1][0].callback_data, "report_delete")

    async def test_copy_help_does_not_delete_or_change_report(self):
        await self.transport.handle(self.callback("report_copy_help", message_id=101), self.context)
        answer = self.bot.answer_callback_query.call_args.kwargs
        self.assertTrue(answer["show_alert"])
        self.assertIn("Копіювати текст", answer["text"])
        self.bot.delete_message.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_report_delete_failure_allows_retry(self):
        await self.transport.send_report(self.bot, OWNER, "Звіт")
        markup = self.bot.send_message.call_args.kwargs["reply_markup"].to_dict()
        self.bot.delete_message.side_effect = NetworkError("Test offline")
        await self.transport.handle(self.callback("report_delete", message_id=101,
                                                   reply_markup=markup), self.context)
        self.assertTrue(self.bot.answer_callback_query.call_args.kwargs["show_alert"])
        self.bot.delete_message.side_effect = None
        await self.transport.handle(self.callback("report_delete", update_id=3, message_id=101,
                                                   reply_markup=markup), self.context)
        self.assertEqual(self.bot.delete_message.await_count, 2)

    async def test_unauthorized_messages_callbacks_and_groups(self):
        await self.transport.handle(self.message("/start", user=999), self.context)
        self.assertEqual(self.bot.send_message.call_args.kwargs["text"], "⛔ Немає доступу.")
        await self.transport.handle(self.callback("reset_confirm", user=999), self.context)
        self.assertIn("Немає доступу", self.bot.answer_callback_query.call_args.kwargs["text"])
        self.bot.reset_mock()
        await self.transport.handle(self.message("/start", chat_type="group"), self.context)
        self.bot.send_message.assert_not_awaited()
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    async def test_deleted_dashboard_is_recreated_and_no_change_is_ignored(self):
        with self.store.transaction() as tx:
            tx.save(State(OWNER, main_message_id=90))
        self.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        await self.transport.refresh(self.bot, OWNER)
        self.bot.send_message.assert_awaited_once()
        with self.store.transaction() as tx:
            self.assertEqual(tx.state(OWNER).main_message_id, 100)
        self.bot.reset_mock()
        self.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        await self.transport.refresh(self.bot, OWNER)
        self.bot.send_message.assert_not_awaited()

    async def test_ui_network_failure_does_not_undo_money_or_reapply_duplicate(self):
        await self.transport.handle(self.message("/start"), self.context)
        await self.transport.handle(self.callback("edit:received"), self.context)
        self.bot.send_message.side_effect = NetworkError("Test offline")
        with self.assertRaises(NetworkError):
            await self.transport.handle(self.message("10", update_id=3), self.context)
        with self.store.transaction() as tx:
            self.assertEqual(tx.cash()["received"], 1000)
            self.assertIsNone(tx.state(OWNER).edit_field)
        self.service.process(OWNER, 4, action="edit:salary", message_id=100)
        await self.transport.handle(self.message("10", update_id=3), self.context)
        with self.store.transaction() as tx:
            self.assertEqual(tx.cash()["salary"], 0)
            self.assertEqual(tx.state(OWNER).edit_field, "salary")

    async def test_application_startup_uses_polling_and_keeps_pending_updates(self):
        settings = Settings("123456789:" + "a" * 35, frozenset({OWNER}), self.path)
        app = build_application(settings)
        self.assertEqual(app.update_processor.max_concurrent_updates, 1)
        await app.post_init(SimpleNamespace(bot=self.bot))
        self.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        commands = self.bot.set_my_commands.call_args.args[0]
        self.assertEqual([command.command for command in commands], ["start", "cancel", "report", "help"])
