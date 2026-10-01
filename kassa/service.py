"""Cash workflow, independent of the Telegram transport."""

import time
import json
from dataclasses import dataclass

from .cash import FIELDS, MoneyError, parse_money, calculate
from .history import report_day
from .storage import Store
from .views import report

HELP = "Натисни поле → введи число. /start — оновити, /cancel — скасувати ввід, /report — звіт."


def ink_entries(state) -> list[int]:
    """Return the uncommitted collection amounts stored in a user session."""
    try:
        entries = json.loads(state.ink_entries or "[]")
    except json.JSONDecodeError:
        return []
    return entries if isinstance(entries, list) and all(type(value) is int for value in entries) else []


def clear_ink_draft(state) -> None:
    state.ink_entries = None
    if state.edit_field == "ink":
        state.edit_field = None


@dataclass(frozen=True)
class Outcome:
    refresh: bool = True
    report_text: str | None = None
    delete_input: bool = False


class CashService:
    def __init__(self, store: Store, allowed_users: frozenset[int], hide_zero_balances: bool = True):
        self.store = store
        self.allowed_users = allowed_users
        self.hide_zero_balances = hide_zero_balances

    def authorized(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return user_id in self.allowed_users and chat_type == "private" and chat_id == user_id

    def process(self, user_id: int, update_id: int, *, text: str = "",
                action: str | None = None, message_id: int | None = None,
                now: float | None = None) -> Outcome:
        if user_id not in self.allowed_users:
            raise PermissionError("Немає доступу.")
        now = time.time() if now is None else now
        day = report_day(now)
        with self.store.transaction() as tx:
            tx.prune_reports(day[:7])
            if tx.seen(update_id):
                return Outcome(refresh=False)
            state = tx.state(user_id)
            state.notice = None
            outcome = Outcome()
            if action is not None:
                if message_id != state.main_message_id or state.main_message_id is None:
                    tx.mark(update_id)
                    return Outcome(refresh=False)
                if action.startswith("history:") and state.reset_at is None:
                    page = action[8:]
                    if page.isascii() and page.isdigit() and len(page) <= 4:
                        state.current_group = "history:" + str(min(int(page), max(0, (len(tx.reports()) - 1) // 7)))
                        clear_ink_draft(state)
                elif action.startswith("saved:") and state.reset_at is None:
                    saved = tx.report_text(action[6:])
                    if saved is None:
                        state.notice = "Звіт уже недоступний. Історія містить лише поточний місяць."
                    else:
                        page = (state.current_group.split(":")[1]
                                if state.current_group and state.current_group.startswith("history:") else "0")
                        state.current_group = f"saved:{action[6:]}:{page}"
                        clear_ink_draft(state)
                elif action.startswith("edit:"):
                    name = action[5:]
                    if name in FIELDS and state.reset_at is None:
                        state.edit_field = name
                        state.current_group = FIELDS[name].group
                        state.ink_entries = "[]" if name == "ink" else None
                elif action.startswith("group:"):
                    group = action[6:]
                    if group in {"start", "game", "expense", "balance"} and state.reset_at is None:
                        state.current_group = group
                        clear_ink_draft(state)
                        state.edit_field = None
                elif action == "back":
                    state.current_group = None
                    clear_ink_draft(state)
                    state.edit_field = None
                elif action == "ink_done":
                    if state.edit_field == "ink" and state.reset_at is None:
                        entries = ink_entries(state)
                        try:
                            tx.update_fields({"ink": sum(entries)})
                        except MoneyError as error:
                            state.notice = "❌ " + str(error)
                        else:
                            clear_ink_draft(state)
                elif action == "ink_cancel":
                    clear_ink_draft(state)
                elif action == "report":
                    if state.reset_at is None:
                        outcome = Outcome(report_text=report(tx.cash(), self.hide_zero_balances))
                        tx.save_report(day, outcome.report_text, calculate(tx.cash())["net_income"])
                elif action == "reset":
                    clear_ink_draft(state)
                    state.reset_at = now
                elif action == "reset_cancel":
                    clear_ink_draft(state)
                    state.reset_at = None
                elif action == "reset_confirm":
                    if state.reset_at is not None:
                        if not 0 <= now - state.reset_at <= 120:
                            state.notice = "Підтвердження застаріло. Натисни «НОВА КАСА» ще раз."
                        else:
                            tx.reset()
                            state.notice = "✅ Нова каса: суму «На карті» перенесено в «Отримав» (Старт). Решту полів очищено."
                            state.current_group = None
                        state.reset_at = None
                        clear_ink_draft(state)
                else:
                    state.notice = "Невідома кнопка. Натисни /start."
            else:
                text = text.strip()
                command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
                if text in {"📚 Історія звітів", "Історія звітів"}:
                    state.current_group = "history:0"
                    clear_ink_draft(state)
                    state.reset_at = None
                elif text == "💰 Каса":
                    state.current_group = None
                    clear_ink_draft(state)
                    state.reset_at = None
                elif command in {"/start", "/cancel", "/help"}:
                    clear_ink_draft(state)
                    state.reset_at = None
                    if command == "/start":
                        state.current_group = None
                    state.notice = HELP if command == "/help" else None
                elif command == "/report":
                    outcome = Outcome(report_text=report(tx.cash(), self.hide_zero_balances))
                    tx.save_report(day, outcome.report_text, calculate(tx.cash())["net_income"])
                elif state.reset_at is not None:
                    state.notice = "Спочатку підтвердь або скасуй очищення."
                elif state.edit_field is None:
                    state.notice = "Спочатку натисни кнопку потрібного поля. /help — підказка."
                else:
                    try:
                        value = parse_money(text)
                        if state.edit_field == "ink":
                            entries = ink_entries(state) + [value]
                            # Check the final value now, before accepting the item into the draft.
                            calculate({**tx.cash(), "ink": sum(entries)})
                            state.ink_entries = json.dumps(entries)
                        else:
                            tx.update_fields({state.edit_field: value})
                    except MoneyError as error:
                        state.notice = "❌ " + str(error)
                    else:
                        if state.edit_field != "ink":
                            state.edit_field = None
                        outcome = Outcome(delete_input=True)
            tx.save(state)
            tx.mark(update_id)
            return outcome
