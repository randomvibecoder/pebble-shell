from __future__ import annotations

import sqlite3
from pathlib import Path


ALLOWED_KEYS = {"heartbeat_every_seconds"}


class RuntimeConfigStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get(self, key: str) -> str | None:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Unsupported runtime config key: {key}")
        with self._connect() as conn:
            row = conn.execute("select value from runtime_config where key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Unsupported runtime config key: {key}")
        if key == "heartbeat_every_seconds":
            seconds = int(value)
            if seconds < 0:
                raise ValueError("heartbeat_every_seconds must be >= 0")
            value = str(seconds)
        with self._connect() as conn:
            conn.execute(
                """
                insert into runtime_config(key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value, updated_at = current_timestamp
                """,
                (key, value.strip()),
            )

    def all(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("select key, value from runtime_config order by key").fetchall()
        return {row[0]: row[1] for row in rows if row[0] in ALLOWED_KEYS}

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists runtime_config (
                    key text primary key,
                    value text not null,
                    updated_at text not null default current_timestamp
                )
                """
            )
