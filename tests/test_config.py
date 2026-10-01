import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kassa.config import ConfigError, Settings

TOKEN = "123456789:" + "a" * 35


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_env(self, content):
        (self.root / ".env").write_text(content, encoding="utf-8-sig")

    def test_load_quotes_bom_and_relative_database(self):
        self.write_env(f'# Local configuration\nTELEGRAM_TOKEN="{TOKEN}"\nALLOWED_USERS=752963390, 42\n')
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.load(self.root)
        self.assertEqual(settings.allowed_users, frozenset({752963390, 42}))
        self.assertEqual(settings.database_path, self.root / "data/kassa.sqlite3")
        self.assertTrue(settings.hide_zero_balances)
        self.assertNotIn(TOKEN, repr(settings))

    def test_environment_overrides_file(self):
        self.write_env(f"TELEGRAM_TOKEN={TOKEN}\nALLOWED_USERS=42\n")
        with patch.dict(os.environ, {"ALLOWED_USERS": "752963390", "REPORT_HIDE_ZERO_BALANCES": "false"}, clear=True):
            settings = Settings.load(self.root)
        self.assertEqual(settings.allowed_users, frozenset({752963390}))
        self.assertFalse(settings.hide_zero_balances)

    def test_reject_missing_token_empty_users_and_bad_boolean(self):
        for values in ({}, {"TELEGRAM_TOKEN": TOKEN}, {"TELEGRAM_TOKEN": TOKEN, "ALLOWED_USERS": "0"},
                       {"TELEGRAM_TOKEN": TOKEN, "ALLOWED_USERS": "42", "REPORT_HIDE_ZERO_BALANCES": "maybe"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(ConfigError):
                Settings.load(self.root)
