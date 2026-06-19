from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class MemoryContext:
    summary: str
    recent_messages: list[tuple[str, str]]
    recent_raw_messages: list[dict[str, object]]


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def add_message(self, role: str, content: str, raw_message: dict[str, object] | None = None) -> None:
        raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
        with self._connect() as conn:
            conn.execute(
                "insert into messages(role, content, raw_json) values (?, ?, ?)",
                (role, content, raw_json),
            )

    def set_contact(self, name: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into contacts(name, value) values (?, ?)
                on conflict(name) do update set value = excluded.value, updated_at = current_timestamp
                """,
                (name, value),
            )

    def get_contact(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("select value from contacts where name = ?", (name,)).fetchone()
        return row[0] if row else None

    def record_heartbeat(self, content: str, notified: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into heartbeat_runs(content, notified) values (?, ?)",
                (content, int(notified)),
            )

    def get_context(
        self,
        query: str,
        recent_limit: int,
        recent_token_budget: int | None = None,
    ) -> MemoryContext:
        del query
        with self._connect() as conn:
            summary = conn.execute("select content from summary where id = 1").fetchone()
            recent_rows = conn.execute(
                """
                select role, content, raw_json
                from messages
                order by id desc
                limit ?
                """,
                (recent_limit,),
            ).fetchall()

        legacy_recent = list(reversed([(row["role"], row["content"]) for row in recent_rows]))
        raw_recent = [_row_to_raw_message(row) for row in reversed(recent_rows)]
        recent_messages, recent_raw_messages = _fit_recent_messages(
            legacy_recent,
            raw_recent,
            recent_token_budget,
        )
        return MemoryContext(
            summary=summary[0] if summary else "",
            recent_messages=recent_messages,
            recent_raw_messages=recent_raw_messages,
        )

    def upsert_summary(self, content: str, through_message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into summary(id, content, through_message_id)
                values (1, ?, ?)
                on conflict(id) do update set
                    content = excluded.content,
                    through_message_id = excluded.through_message_id,
                    updated_at = current_timestamp
                """,
                (content.strip(), through_message_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode = wal")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists messages (
                    id integer primary key autoincrement,
                    role text not null,
                    content text not null,
                    raw_json text,
                    created_at text not null default current_timestamp
                );

                create table if not exists summary (
                    id integer primary key check(id = 1),
                    content text not null,
                    through_message_id integer not null default 0,
                    updated_at text not null default current_timestamp
                );

                create table if not exists contacts (
                    name text primary key,
                    value text not null,
                    updated_at text not null default current_timestamp
                );

                create table if not exists heartbeat_runs (
                    id integer primary key autoincrement,
                    content text not null,
                    notified integer not null,
                    created_at text not null default current_timestamp
                );
                """
            )


def _row_to_raw_message(row: sqlite3.Row) -> dict[str, object]:
    raw_json = row["raw_json"]
    if raw_json:
        try:
            raw = json.loads(raw_json)
            if isinstance(raw, dict) and isinstance(raw.get("role"), str):
                return raw
        except json.JSONDecodeError:
            pass
    return {"role": row["role"], "content": row["content"]}


def _fit_recent_messages(
    messages: list[tuple[str, str]],
    raw_messages: list[dict[str, object]],
    token_budget: int | None,
) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
    if token_budget is None or token_budget <= 0:
        return messages, raw_messages
    selected: list[tuple[str, str]] = []
    selected_raw: list[dict[str, object]] = []
    used = 0
    for (role, content), raw_message in zip(reversed(messages), reversed(raw_messages), strict=True):
        cost = _estimate_tokens(role) + _estimate_tokens(content)
        if selected and used + cost > token_budget:
            break
        if not selected and cost > token_budget:
            content = _truncate_to_token_budget(content, max(1, token_budget - _estimate_tokens(role)))
            raw_message = {**raw_message, "content": content}
            cost = _estimate_tokens(role) + _estimate_tokens(content)
        selected.append((role, content))
        selected_raw.append(raw_message)
        used += cost
    return list(reversed(selected)), list(reversed(selected_raw))


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    max_chars = max(1, token_budget * 4)
    if len(text) <= max_chars:
        return text
    marker = "[older text truncated]\n"
    available = max(1, max_chars - len(marker))
    return marker + text[-available:]
