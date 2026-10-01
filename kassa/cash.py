"""Cash fields and formulas. All monetary values are integer kopecks."""

import re
from dataclasses import dataclass
from collections.abc import Mapping

MAX_CENTS = 1_000_000_000_000  # 10 billion UAH


class MoneyError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    label: str
    group: str


FIELDS = {
    "received": Field("Отримав", "start"),
    "salary": Field("ЗП", "start"),
    "ink": Field("ІНК", "start"),
    "incoming": Field("Приход", "start"),
    "champion_in": Field("Чемп ВХ", "game"),
    "champion_out": Field("Чемп ВИХ", "game"),
    "super_in": Field("Супер ВХ", "game"),
    "super_out": Field("Супер ВИХ", "game"),
    "green_in": Field("Грін ВХ", "game"),
    "green_out": Field("Грін ВИХ", "game"),
    "commission": Field("Комісія", "expense"),
    "otkat": Field("Откат", "expense"),
    "double": Field("Дабл", "expense"),
    "action": Field("Акція", "expense"),
    "safe": Field("Сейф", "expense"),
    "referrals": Field("Реферали", "expense"),
    "other_expenses": Field("Інші", "expense"),
    "mono": Field("Моно", "balance"),
    "abank": Field("Абанк", "balance"),
    "privat": Field("Приват", "balance"),
    "alliance": Field("Альянс", "balance"),
    "my_1": Field("Мої 1", "balance"),
    "my_2": Field("Мої 2", "balance"),
    "debt": Field("Борг", "balance"),
    "players_balance": Field("Баланс гравців", "balance"),
}

INCOME_SOURCES = (
    ("ЧЕМПІОН", "чемпіон", "champion_in", "champion_out", "champion_income"),
    ("СУПЕРОМАТІК", "супероматік", "super_in", "super_out", "super_income"),
    ("ГРІН", "грін", "green_in", "green_out", "green_income"),
)


def fields_in(group: str) -> list[str]:
    return [name for name, field in FIELDS.items() if field.group == group]


def validate_cents(value: int) -> int:
    if type(value) is not int or abs(value) > MAX_CENTS:
        raise MoneyError("Сума має бути в межах ±10 млрд грн.")
    return value


def parse_money(text: str) -> int:
    value = re.sub(r"\s", "", text).replace(",", ".")
    if len(value) > 32 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]{1,2})?", value):
        raise MoneyError("Введіть число, наприклад -5 000,50 (до 2 знаків після коми).")
    whole, _, fraction = value.lstrip("-").partition(".")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    return validate_cents(-cents if value.startswith("-") else cents)


def format_money(cents: int, grouped: bool = False) -> str:
    validate_cents(cents)
    whole, fraction = divmod(abs(cents), 100)
    text = f"{whole:,}".replace(",", " ") if grouped else str(whole)
    if fraction:
        text += ("," if grouped else ".") + f"{fraction:02d}"
    return ("-" if cents < 0 else "") + text


def calculate(data: Mapping[str, int]) -> dict[str, int]:
    for name, value in data.items():
        if name not in FIELDS:
            raise MoneyError("Невідоме поле каси.")
        validate_cents(value)
    def total(group: str) -> int:
        return sum(data.get(name, 0) for name in fields_in(group))
    result = {"start_balance": total("start")}
    for _, _, incoming, outgoing, key in INCOME_SOURCES:
        result[key] = data.get(incoming, 0) - data.get(outgoing, 0)
    result["total_income"] = sum(result[source[4]] for source in INCOME_SOURCES)
    result["theoretical_balance"] = result["start_balance"] + result["total_income"]
    result["listed_expenses"] = total("expense")
    result["on_card"] = total("balance")
    result["actual_expense"] = result["theoretical_balance"] - result["on_card"]
    result["net_income"] = result["total_income"] - result["actual_expense"]
    result["expense_difference"] = result["actual_expense"] - result["listed_expenses"]
    for value in result.values():
        validate_cents(value)
    return result
