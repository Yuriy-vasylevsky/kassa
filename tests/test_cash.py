import unittest

from kassa.cash import MAX_CENTS, MoneyError, calculate, parse_money, format_money

FIXTURE = {name: value * 100 for name, value in {
    "received": 23292, "ink": -11833, "champion_in": 3060,
    "super_in": 2155, "super_out": 193, "commission": 50,
    "otkat": 200, "action": 400, "abank": 5737, "privat": 720,
    "alliance": 10145, "debt": -771,
}.items()}


class CashTests(unittest.TestCase):
    def test_all_formulas_match_example(self):
        expected = {"start_balance": 11459, "champion_income": 3060, "super_income": 1962, "green_income": 0,
                    "total_income": 5022, "theoretical_balance": 16481, "listed_expenses": 650,
                    "on_card": 15831, "actual_expense": 650, "net_income": 4372, "expense_difference": 0}
        self.assertEqual(calculate(FIXTURE), {name: value * 100 for name, value in expected.items()})

    def test_green_income_is_included_in_total(self):
        result = calculate({"green_in": 4200, "green_out": 1200})
        self.assertEqual(result["green_income"], 3000)
        self.assertEqual(result["total_income"], 3000)

    def test_parse_and_format_money(self):
        for value in ("-5 000,50", "-5000.50", "-5\u00a0000,50", "-5\u202f000,50"):
            self.assertEqual(parse_money(value), -500050)
        self.assertEqual(parse_money("0.01"), 1)
        self.assertEqual(format_money(-1183350, True), "-11 833,50")
        self.assertEqual(format_money(500050), "5000.50")
        self.assertEqual(parse_money("10000000000"), MAX_CENTS)

    def test_invalid_amounts(self):
        for value in ("abc", "500грн", "", "1.234", "1e3", "NaN", "inf", "1,2,3", "10000000000.01", "9" * 100):
            with self.subTest(value=value), self.assertRaises(MoneyError):
                parse_money(value)
        for value in (True, 1.2, "100", MAX_CENTS + 1):
            with self.assertRaises(MoneyError):
                calculate({"received": value})

    def test_cents_do_not_use_floating_point(self):
        self.assertEqual(calculate({"received": parse_money("0.1"), "salary": parse_money("0.2")})["start_balance"], 30)

    def test_total_limits_and_negative_balances(self):
        with self.assertRaises(MoneyError):
            calculate({"received": MAX_CENTS, "salary": 1})
        result = calculate({"received": 10000, "debt": -500})
        self.assertEqual(result["on_card"], -500)
        self.assertEqual(result["actual_expense"], 10500)
