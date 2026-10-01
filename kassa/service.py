"""Cash workflow, independent of the Telegram transport."""

import time
from dataclasses import dataclass

from .cash import FIELDS, MoneyError, parse_money, calculate
from .history import report_day
from .storage import Store
from .views import report

HELP = "Натисни поле → введи число. /start — оновити, /cancel — скасувати ввід, /report — звіт."


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
                        state.edit_field = None
                elif action.startswith("saved:") and state.reset_at is None:
                    saved = tx.report_text(action[6:])
                    if saved is None:
                        state.notice = "Звіт уже недоступний. Історія містить лише поточний місяць."
                    else:
                        page = (state.current_group.split(":")[1]
                                if state.current_group and state.current_group.startswith("history:") else "0")
                        state.current_group = f"saved:{action[6:]}:{page}"
                        state.edit_field = None
                elif action.startswith("edit:"):
                    name = action[5:]
                    if name in FIELDS and state.reset_at is None:
                        state.edit_field = name
                        state.current_group = FIELDS[name].group
                elif action.startswith("group:"):
                    group = action[6:]
                    if group in {"start", "game", "expense", "balance"} and state.reset_at is None:
                        state.current_group = group
                        state.edit_field = None
                elif action == "back":
                    state.current_group = None
                    state.edit_field = None
                elif action == "report":
                    if state.reset_at is None:
                        outcome = Outcome(report_text=report(tx.cash(), self.hide_zero_balances))
                        tx.save_report(day, outcome.report_text, calculate(tx.cash())["net_income"])
                elif action == "reset":
                    state.edit_field = None
                    state.reset_at = now
                elif action == "reset_cancel":
                    state.edit_field = None
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
                        state.edit_field = None
                else:
                    state.notice = "Невідома кнопка. Натисни /start."
            else:
                text = text.strip()
                command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
                if text in {"📚 Історія звітів", "Історія звітів"}:
                    state.current_group = "history:0"
                    state.edit_field = None
                    state.reset_at = None
                elif text == "💰 Каса":
                    state.current_group = None
                    state.edit_field = None
                    state.reset_at = None
                elif command in {"/start", "/cancel", "/help"}:
                    state.edit_field = None
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
                        tx.update_fields({state.edit_field: value})
                    except MoneyError as error:
                        state.notice = "❌ " + str(error)
                    else:
                        state.edit_field = None
                        outcome = Outcome(delete_input=True)
            tx.save(state)
            tx.mark(update_id)
            return outcome
