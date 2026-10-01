import tempfile
import unittest
from pathlib import Path

from kassa.cash import FIELDS, MAX_CENTS, MoneyError, calculate
from kassa.service import CashService
from kassa.storage import State, Store
from kassa.views import dashboard, keyboard, report
from test_cash import FIXTURE
from datetime import datetime
from zoneinfo import ZoneInfo
from kassa.history import report_day
from kassa.views import history_view

OWNER = 752963390
OTHER = 42


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cash.sqlite3"
        self.store = Store(self.path)
        self.service = CashService(self.store, frozenset({OWNER, OTHER}))
        self.seq = 0
        with self.store.transaction() as tx:
            tx.save(State(OWNER, main_message_id=100))
            tx.save(State(OTHER, main_message_id=200))

    def event(self, *, user=OWNER, **kwargs):
        self.seq += 1
        if "action" in kwargs:
            kwargs.setdefault("message_id", 100 if user == OWNER else 200)
        return self.service.process(user, self.seq, **kwargs)

    def cash(self):
        with self.store.transaction() as tx:
            return tx.cash()

    def state(self, user=OWNER):
        with self.store.transaction() as tx:
            return tx.state(user)

    def test_every_field_can_be_edited_and_replaced(self):
        for index, name in enumerate(FIELDS):
            self.event(action="edit:" + name)
            self.assertTrue(self.event(text=str(index)).delete_input)
            if name == "ink":
                self.event(action="ink_done")
            self.assertEqual(self.cash()[name], index * 100)
            self.event(action="edit:" + name)
            self.event(text="1.01")
            if name == "ink":
                self.event(action="ink_done")
            self.assertEqual(self.cash()[name], 101)
        self.assertIsNone(self.state().edit_field)

    def test_access_checks_and_no_storage_for_strangers(self):
        self.assertTrue(self.service.authorized(OWNER, OWNER, "private"))
        for user, chat, kind in ((1, 1, "private"), (OWNER, -1, "group"), (OWNER, OTHER, "private")):
            self.assertFalse(self.service.authorized(user, chat, kind))
        with self.assertRaises(PermissionError):
            self.service.process(1, 10, text="/start")
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute("SELECT count(*) FROM users").fetchone()[0], 2)

    def test_invalid_input_keeps_edit_and_original_values(self):
        self.event(action="edit:received")
        self.event(text="abc")
        self.assertEqual(self.cash()["received"], 0)
        self.assertEqual(self.state().edit_field, "received")
        self.assertIn("❌", self.state().notice)
        self.event(text="123,45")
        self.assertEqual(self.cash()["received"], 12345)

    def test_restart_preserves_cash_and_pending_input(self):
        self.event(action="edit:received")
        self.event(text="100")
        self.event(action="edit:ink")
        restarted = CashService(Store(self.path), frozenset({OWNER}))
        restarted.process(OWNER, 99, text="-1.50")
        restarted.process(OWNER, 100, action="ink_done", message_id=100)
        self.assertEqual(self.cash()["received"], 10000)
        self.assertEqual(self.cash()["ink"], -150)
        self.assertEqual(self.state().main_message_id, 100)

    def test_atomic_validation_and_transaction_rollback(self):
        with self.store.transaction() as tx:
            tx.update_fields({"received": MAX_CENTS})
        before = self.cash()
        with self.assertRaises(MoneyError), self.store.transaction() as tx:
            tx.update_fields({"salary": 1, "mono": 99})
        self.assertEqual(self.cash(), before)
        with self.assertRaises(RuntimeError), self.store.transaction() as tx:
            tx.update_fields({"received": 100})
            raise RuntimeError("Crash before commit")
        self.assertEqual(self.cash(), before)

    def test_aggregate_overflow_keeps_pending_edit(self):
        with self.store.transaction() as tx:
            tx.update_fields({"received": MAX_CENTS})
        self.event(action="edit:salary")
        self.event(text="0.01")
        self.assertEqual(self.cash()["salary"], 0)
        self.assertEqual(self.state().edit_field, "salary")

    def test_duplicate_cannot_write_into_later_edit_even_after_restart(self):
        self.event(action="edit:received")
        self.event(text="100")
        repeated_id = self.seq
        self.event(action="edit:salary")
        restarted = CashService(Store(self.path), frozenset({OWNER}))
        outcome = restarted.process(OWNER, repeated_id, text="100")
        self.assertFalse(outcome.refresh)
        self.assertEqual(self.cash()["salary"], 0)
        self.assertEqual(self.state().edit_field, "salary")

    def test_old_dashboard_and_unknown_field_do_not_change_cash(self):
        self.event(action="edit:received", message_id=99)
        self.assertIsNone(self.state().edit_field)
        self.event(action="edit:unknown")
        self.assertIsNone(self.state().edit_field)
        self.event(text="500")
        self.assertEqual(self.cash()["received"], 0)

    def test_reset_requires_recent_confirmation_and_cancels_all_users(self):
        with self.store.transaction() as tx:
            tx.update_fields(FIXTURE)
        before = self.cash()
        self.event(action="reset_confirm", now=100)
        self.assertEqual(self.cash(), before)
        self.event(action="reset", now=100)
        self.event(action="reset_confirm", now=221)
        self.assertEqual(self.cash(), before)
        self.assertIn("застаріло", self.state().notice)
        self.event(user=OTHER, action="edit:mono")
        self.event(action="reset", now=300)
        self.event(action="reset_confirm", now=301)
        self.assertEqual(self.cash()["received"], calculate(before)["on_card"])
        self.assertEqual(calculate(self.cash())["start_balance"], calculate(before)["on_card"])
        self.assertTrue(all(value == 0 for name, value in self.cash().items() if name != "received"))
        self.assertIsNone(self.state(OTHER).edit_field)

    def test_cancel_and_commands(self):
        self.event(action="edit:received")
        self.event(text="/cancel")
        self.assertIsNone(self.state().edit_field)
        self.event(action="reset")
        self.event(action="reset_cancel")
        self.assertIsNone(self.state().reset_at)
        self.event(text="/help")
        self.assertIn("Натисни поле", self.state().notice)
        self.event(text="/start@kassa_bot")
        self.assertIsNone(self.state().notice)

    def test_collection_accepts_several_amounts_and_writes_only_on_done(self):
        self.event(action="edit:ink")
        self.event(text="100")
        self.event(text="25,50")
        self.assertEqual(self.cash()["ink"], 0)
        state = self.state()
        self.assertEqual(state.edit_field, "ink")
        view = dashboard(self.cash(), state)
        self.assertIn("100, 25,50", view)
        self.assertIn("Разом: <b>125,50</b>", view)
        actions = [action for row in keyboard(self.cash(), state) for _, action in row]
        self.assertEqual(actions, ["ink_done", "ink_cancel"])
        self.event(action="ink_done")
        self.assertEqual(self.cash()["ink"], 12550)
        self.assertIsNone(self.state().edit_field)

    def test_collection_cancel_keeps_previous_ink_value(self):
        with self.store.transaction() as tx:
            tx.update_fields({"ink": 1000})
        self.event(action="edit:ink")
        self.event(text="20")
        self.event(action="ink_cancel")
        self.assertEqual(self.cash()["ink"], 1000)
        self.assertIsNone(self.state().edit_field)

    def test_report_and_dashboard_match_fixture(self):
        with self.store.transaction() as tx:
            data = tx.update_fields(FIXTURE)
        text = self.event(text="/report").report_text
        self.assertIn("Чистий дохід  4372", text)
        for hidden_label in ("Мої 1", "Мої 2", "Борг", "БАЛАНС ГРАВЦІВ"):
            self.assertNotIn(hidden_label, text)
        self.assertNotIn("<pre>", text)
        self.assertNotIn("Моно  0", text)
        full_report = report(data, False)
        self.assertNotIn("Моно  0", full_report)
        for hidden_label in ("Мої 1", "Мої 2", "Борг", "Баланс гравців"):
            self.assertNotIn(hidden_label, full_report)
        self.assertEqual(self.event(action="report").report_text, text)
        view = dashboard(data, State(OWNER, notice="<example>"))
        self.assertIn("Витрати сходяться", view)
        self.assertIn("&lt;example&gt;", view)
        self.assertLess(len(view), 4096)
        root_callbacks = [action for row in keyboard(data, State(OWNER)) for _, action in row]
        self.assertEqual(root_callbacks[:4], ["group:start", "group:game", "group:expense", "group:balance"])
        callbacks = []
        for group in ("start", "game", "expense", "balance"):
            callbacks.extend(action for row in keyboard(data, State(OWNER, current_group=group)) for _, action in row)
        self.assertTrue(all("edit:" + name in callbacks for name in FIELDS))
        self.assertTrue(all(len(action.encode()) <= 64 for action in root_callbacks + callbacks))

    def test_report_omits_zero_rows_and_empty_game_blocks(self):
        data = {name: 0 for name in FIELDS}
        self.assertEqual(report(data), "Немає ненульових сум для звіту.")
        data["received"] = 1583100
        self.assertEqual(report(data), "Отримав  15831\n\nЧистий дохід  -15831\nрозходи  15831")
        data["super_in"] = 100
        text = report(data)
        self.assertIn("супероматік\nвх  1\nвих  0\nдох  1", text)
        self.assertNotIn("чемпіон", text)
        data["super_out"] = 100
        self.assertIn("супероматік\nвх  1\nвих  1\nдох  0", report(data))

    def test_mobile_navigation_shows_one_short_section_at_a_time(self):
        self.event(action="group:expense")
        state = self.state()
        self.assertEqual(state.current_group, "expense")
        text = dashboard(self.cash(), state)
        self.assertIn("💸 ВИТРАТИ", text)
        self.assertIn("Комісія", text)
        self.assertNotIn("Баланс гравців", text)
        self.assertNotIn("<pre>", text)
        self.assertLess(len(text.splitlines()), 25)
        actions = [action for row in keyboard(self.cash(), state) for _, action in row]
        self.assertIn("edit:commission", actions)
        self.assertIn("back", actions)
        self.event(action="back")
        self.assertIsNone(self.state().current_group)

    def test_existing_database_is_migrated_for_mobile_navigation(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        import sqlite3
        db = sqlite3.connect(legacy)
        db.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, main_message_id INTEGER, edit_field TEXT, reset_at REAL, notice TEXT)")
        db.execute("INSERT INTO users VALUES (?, ?, NULL, NULL, NULL)", (OWNER, 77))
        db.commit()
        db.close()
        migrated = Store(legacy)
        with migrated.transaction() as tx:
            state = tx.state(OWNER)
            self.assertEqual(state.main_message_id, 77)
            self.assertIsNone(state.current_group)

    def test_complete_example_via_buttons(self):
        for name, cents in FIXTURE.items():
            self.event(action="edit:" + name)
            self.event(text=str(cents // 100))
            if name == "ink":
                self.event(action="ink_done")
        self.assertIn("На карті  15831", self.event(action="report").report_text)

    def test_duplicate_receipts_keep_new_arrivals_if_telegram_resets_ids(self):
        with self.store.transaction() as tx:
            for update_id in range(10000, 11000):
                tx.mark(update_id)
            tx.mark(1)
            self.assertTrue(tx.seen(1))
            self.assertFalse(tx.seen(10000))
            self.assertEqual(tx.db.execute("SELECT count(*) FROM processed").fetchone()[0], 1000)

    def test_history_replaces_daily_report_and_survives_reset_and_restart(self):
        now = datetime(2026, 10, 2, 12, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        with self.store.transaction() as tx:
            tx.update_fields(FIXTURE)
        first = self.event(action="report", now=now).report_text
        with self.store.transaction() as tx:
            tx.update_fields({"mono": 100})
        latest = self.event(text="/report", now=now + 1).report_text
        self.assertNotEqual(first, latest)
        with self.store.transaction() as tx:
            items = tx.reports()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["text"], latest)
        self.event(action="reset", now=now + 2)
        self.event(action="reset_confirm", now=now + 3)
        self.service = CashService(Store(self.path), frozenset({OWNER, OTHER}))
        self.assertIsNone(self.event(user=OTHER, action="saved:2026-10-02", now=now + 4).report_text)
        self.assertEqual(self.state(OTHER).current_group, "saved:2026-10-02:0")
        with self.store.transaction() as tx:
            self.assertEqual(tx.reports(), items)

    def test_history_pagination_total_and_month_rollover(self):
        now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        with self.store.transaction() as tx:
            for day in range(1, 16):
                tx.save_report(f"2026-10-{day:02}", f"Звіт {day}", -100 if day == 1 else 100)
        self.event(action="history:0", now=now)
        with self.store.transaction() as tx:
            items = tx.reports()
        text, rows = history_view(items, self.state())
        self.assertIn("13", text)
        self.assertEqual(rows[0][0], ("жовтень 15", "saved:2026-10-15"))
        self.assertEqual(sum(action.startswith("saved:") for row in rows for _, action in row), 7)
        self.event(action="history:2", now=now)
        _, rows = history_view(items, self.state())
        self.assertEqual(sum(action.startswith("saved:") for row in rows for _, action in row), 1)
        next_month = datetime(2026, 10, 31, 22, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertEqual(report_day(next_month), "2026-11-01")
        self.assertIsNone(self.event(action="saved:2026-10-15", now=next_month).report_text)
        with self.store.transaction() as tx:
            self.assertEqual(tx.reports(), [])
        text, rows = history_view([], self.state())
        self.assertIn("ще немає звітів", text)
        self.assertEqual(rows, [[("⬅️ До підсумків", "back")]])
