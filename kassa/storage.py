"""Transactional SQLite storage for one shared cash register and user sessions."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from collections.abc import Iterator, Mapping

from .cash import FIELDS, MoneyError, calculate


@dataclass
class State:
    user_id: int
    main_message_id: int | None = None
    edit_field: str | None = None
    reset_at: float | None = None
    notice: str | None = None
    current_group: str | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.transaction() as tx:
            tx.db.execute("CREATE TABLE IF NOT EXISTS cash (field TEXT PRIMARY KEY, cents INTEGER NOT NULL)")
            tx.db.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, main_message_id INTEGER,
                edit_field TEXT, reset_at REAL, notice TEXT, current_group TEXT)""")
            user_columns = {row["name"] for row in tx.db.execute("PRAGMA table_info(users)")}
            if "current_group" not in user_columns:
                tx.db.execute("ALTER TABLE users ADD COLUMN current_group TEXT")
            tx.db.execute("""CREATE TABLE IF NOT EXISTS processed (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, update_id INTEGER UNIQUE NOT NULL)""")
            tx.db.execute("""CREATE TABLE IF NOT EXISTS reports (
                day TEXT PRIMARY KEY, text TEXT NOT NULL, net_income INTEGER NOT NULL)""")
            tx.db.executemany("INSERT OR IGNORE INTO cash VALUES (?, 0)", [(name,) for name in FIELDS])
            calculate(tx.cash())

    @contextmanager
    def transaction(self) -> Iterator["Transaction"]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield Transaction(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class Transaction:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def prune_reports(self, month: str) -> None:
        self.db.execute("DELETE FROM reports WHERE substr(day, 1, 7) != ?", (month,))

    def save_report(self, day: str, text: str, net_income: int) -> None:
        self.db.execute("""INSERT INTO reports VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET text=excluded.text,
            net_income=excluded.net_income""", (day, text, net_income))

    def reports(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM reports ORDER BY day DESC")]

    def report_text(self, day: str) -> str | None:
        row = self.db.execute("SELECT text FROM reports WHERE day=?", (day,)).fetchone()
        return row["text"] if row else None

    def cash(self) -> dict[str, int]:
        return {row["field"]: row["cents"] for row in self.db.execute("SELECT * FROM cash")}

    def update_fields(self, updates: Mapping[str, int]) -> dict[str, int]:
        if any(name not in FIELDS for name in updates):
            raise MoneyError("Невідоме поле каси.")
        data = {**self.cash(), **updates}
        calculate(data)  # Validate the entire candidate before writing any field.
        self.db.executemany("UPDATE cash SET cents=? WHERE field=?", [(value, name) for name, value in updates.items()])
        return data

    def state(self, user_id: int) -> State:
        row = self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return State(**dict(row)) if row else State(user_id)

    def save(self, state: State) -> None:
        self.db.execute("""INSERT INTO users
            (user_id, main_message_id, edit_field, reset_at, notice, current_group)
            VALUES (:user_id, :main_message_id, :edit_field, :reset_at, :notice, :current_group)
            ON CONFLICT(user_id) DO UPDATE SET main_message_id=excluded.main_message_id,
            edit_field=excluded.edit_field, reset_at=excluded.reset_at,
            notice=excluded.notice, current_group=excluded.current_group""", asdict(state))

    def reset(self) -> None:
        on_card = calculate(self.cash())["on_card"]
        self.update_fields({name: on_card if name == "received" else 0 for name in FIELDS})
        self.db.execute("""UPDATE users SET edit_field=NULL, reset_at=NULL, current_group=NULL,
            notice='Нову касу розпочато: суму «На карті» перенесено в «Отримав». /start — оновити показники.'""")

    def seen(self, update_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone() is not None

    def mark(self, update_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO processed (update_id) VALUES (?)", (update_id,))
        self.db.execute("DELETE FROM processed WHERE sequence NOT IN (SELECT sequence FROM processed ORDER BY sequence DESC LIMIT 1000)")
