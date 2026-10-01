"""Local configuration. Environment variables take precedence over .env."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key.strip()):
            raise ConfigError(f"Неправильний рядок {number} у .env.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_users: frozenset[int]
    database_path: Path
    hide_zero_balances: bool = True

    @classmethod
    def load(cls, root: Path = ROOT) -> "Settings":
        values = {**read_env(root / ".env"), **os.environ}
        token = values.get("TELEGRAM_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("Додайте справжній TELEGRAM_TOKEN від @BotFather у .env.")
        users = values.get("ALLOWED_USERS", "").strip()
        if not re.fullmatch(r"[0-9]+(?:\s*,\s*[0-9]+)*", users):
            raise ConfigError("Додайте ALLOWED_USERS=752963390 у .env (ID через кому).")
        allowed = frozenset(int(value.strip()) for value in users.split(","))
        if any(value <= 0 for value in allowed):
            raise ConfigError("ALLOWED_USERS має містити додатні числові ID.")
        hide = values.get("REPORT_HIDE_ZERO_BALANCES", "true").strip().lower()
        if hide not in {"true", "false"}:
            raise ConfigError("REPORT_HIDE_ZERO_BALANCES: вкажіть true або false.")
        db = Path(values.get("DATABASE_PATH", "data/kassa.sqlite3"))
        return cls(token, allowed, db if db.is_absolute() else root / db, hide == "true")
