"""Ukrainian dashboard, editable fields and plain-text reports."""

from html import escape
import json
from .cash import FIELDS, INCOME_SOURCES, calculate, fields_in, format_money
from .storage import State

REPORT_HIDDEN_BALANCES = {"my_1", "my_2", "debt", "players_balance"}
MONTHS = ("січень", "лютий", "березень", "квітень", "травень", "червень",
          "липень", "серпень", "вересень", "жовтень", "листопад", "грудень")


def history_view(reports: list[dict], state: State) -> tuple[str, list]:
    pages = max(1, (len(reports) + 6) // 7)
    page = min(int(state.current_group.split(":")[1]), pages - 1)
    total = sum(item["net_income"] for item in reports)
    text = f"<b>Чистий дохід за місяць: {format_money(total, True)}</b>\n\n📚 Історія звітів"
    rows = []
    for item in reports[page * 7:(page + 1) * 7]:
        _, month, day = item["day"].split("-")
        rows.append([(f"{MONTHS[int(month) - 1]} {int(day)}", "saved:" + item["day"])])
    if not reports:
        text += "\nЗа поточний місяць ще немає звітів."
    else:
        text += f"\nОстанній звіт за кожен день. Сторінка {page + 1}/{pages}."
    navigation = []
    if page > 0:
        navigation.append(("⬅️ Новіші", f"history:{page - 1}"))
    if page + 1 < pages:
        navigation.append(("Старіші ➡️", f"history:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("⬅️ До підсумків", "back")])
    if state.notice:
        text += "\n" + escape(state.notice)
    return text, rows


def dashboard(data: dict[str, int], state: State, monthly_income: int = 0) -> str:
    result = calculate(data)
    def row(label: str, value: int) -> str:
        return f"{escape(label)}: <b>{format_money(value, grouped=True)}</b>"

    lines = ["<b>💰 КАСА</b>", row("Чистий дохід за місяць", monthly_income), "", row("Старт", result["start_balance"]),
             row("Загальний дохід", result["total_income"]),
             row("Теоретичний баланс", result["theoretical_balance"]),
             row("На карті", result["on_card"]),
             row("Фактичний розхід", result["actual_expense"]),
             row("Чистий дохід", result["net_income"])]
    difference = result["expense_difference"]
    lines.extend(["", f"{'✅' if difference == 0 else '⚠️'} Різниця витрат: <b>{format_money(difference, True)}</b>"])
    if difference == 0:
        lines.append("Витрати сходяться")
    elif difference > 0:
        lines.append("Не розписано витрат: " + format_money(difference, True))
    else:
        lines.append("Розписані витрати перевищують фактичні на " + format_money(-difference, True))
    group = state.current_group
    if group:
        titles = {"start": "🌅 СТАРТ ДНЯ", "game": "🎮 ІГРИ",
                  "expense": "💸 ВИТРАТИ", "balance": "💳 ЗАЛИШКИ"}
        lines.extend(["", f"<b>{titles[group]}</b>"])
        lines.extend(row(FIELDS[name].label, data[name]) for name in fields_in(group))
        if group == "game":
            lines.extend(row(source[0] + " — дохід", result[source[4]]) for source in INCOME_SOURCES)
        elif group == "expense":
            lines.append(row("Внесені витрати", result["listed_expenses"]))

    text = "\n".join(lines)
    if state.reset_at is not None:
        text += "\n⚠️ Почати нову касу? Сума «На карті» перейде в «Отримав» (Старт), решта полів очиститься. Підтвердження діє 2 хвилини."
    elif state.edit_field:
        text += "\n✏️ Редагування: " + escape(FIELDS[state.edit_field].label)
        if state.edit_field == "ink":
            try:
                entries = json.loads(state.ink_entries or "[]")
            except json.JSONDecodeError:
                entries = []
            entries = entries if isinstance(entries, list) and all(type(value) is int for value in entries) else []
            values = ", ".join(format_money(value, True) for value in entries) or "ще немає"
            text += f"\nДодані суми: <b>{values}</b>"
            text += f"\nРазом: <b>{format_money(sum(entries), True)}</b>"
            text += "\nНадішли наступну суму або натисни «✅ Готово»."
        else:
            text += "\nНадішли нове значення. /cancel — скасувати."
    if state.notice:
        text += "\n" + escape(state.notice)
    return text


def keyboard(data: dict[str, int], state: State) -> list[list[tuple[str, str]]]:
    if state.reset_at is not None:
        return [[("✅ Очистити", "reset_confirm"), ("❌ Скасувати", "reset_cancel")]]
    if state.edit_field == "ink":
        return [[("✅ Готово", "ink_done"), ("❌ Скасувати", "ink_cancel")]]
    if state.current_group:
        names = fields_in(state.current_group)
        rows = [[(f"{FIELDS[name].label} · {format_money(data[name], True)}", "edit:" + name)
                 for name in names[index:index + 2]] for index in range(0, len(names), 2)]
        rows.append([("⬅️ До підсумків", "back")])
        return rows
    rows = [
        [("🌅 Старт", "group:start"), ("🎮 Ігри", "group:game")],
        [("💸 Витрати", "group:expense"), ("💳 Залишки", "group:balance")],
        [("📄 Готовий звіт", "report")],
        [("🔄 Нова каса", "reset")],
        [("📚 Історія звітів", "history:0")],
    ]
    return rows


def report(data: dict[str, int], hide_zero_balances: bool = True) -> str:
    # Keep the legacy argument compatible; reports now omit every zero value.
    result = calculate(data)
    blocks = []

    def add_block(
        values: list[tuple[str, int]], title: str | None = None, keep_zeroes: bool = False
    ) -> None:
        lines = [f"{label}  {format_money(value)}" for label, value in values
                 if keep_zeroes or value != 0]
        if lines:
            blocks.append("\n".join(([title] if title else []) + lines))

    add_block([("Отримав", data["received"]), ("ЗП", data["salary"])])
    add_block([("ІНК", data["ink"]), ("приход", data["incoming"])])
    for _, label, incoming, outgoing, key in INCOME_SOURCES:
        values = [("вх", data[incoming]), ("вих", data[outgoing]), ("дох", result[key])]
        add_block(values, label, keep_zeroes=any(value != 0 for _, value in values))
    add_block([("Загальний дохід", result["total_income"]), ("Чистий дохід", result["net_income"]),
               ("розходи", result["actual_expense"])]
              + [(FIELDS[name].label.lower(), data[name]) for name in fields_in("expense")]
              + [("На карті", result["on_card"])])
    add_block([(FIELDS[name].label, data[name]) for name in fields_in("balance")
               if name not in REPORT_HIDDEN_BALANCES])
    return "\n\n".join(blocks) or "Немає ненульових сум для звіту."
