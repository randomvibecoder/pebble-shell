from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any


NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class SelfImprovementStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def record(self, kind: str, name: str, description: str, data: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into improvements(kind, name, description, data) values (?, ?, ?, ?)",
                (kind, name, description, json.dumps(data or {}, sort_keys=True)),
            )

    def list_records(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select kind, name, description, data, created_at from improvements order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [
            {
                "kind": row[0],
                "name": row[1],
                "description": row[2],
                "data": json.loads(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    def upsert_hook(self, name: str, prompt: str) -> None:
        _validate_name(name)
        if not prompt.strip():
            raise ValueError("hook prompt cannot be empty")
        with self._connect() as conn:
            conn.execute(
                """
                insert into webhook_hooks(name, prompt)
                values (?, ?)
                on conflict(name) do update set
                    prompt = excluded.prompt,
                    enabled = 1,
                    updated_at = current_timestamp
                """,
                (name, prompt.strip()),
            )

    def get_hook(self, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select name, prompt, enabled from webhook_hooks where name = ?",
                (name,),
            ).fetchone()
        if not row:
            return None
        return {"name": row[0], "prompt": row[1], "enabled": bool(row[2])}

    def list_hooks(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select name, prompt, enabled, updated_at from webhook_hooks order by name",
            ).fetchall()
        return [
            {"name": row[0], "prompt": row[1], "enabled": bool(row[2]), "updated_at": row[3]}
            for row in rows
        ]

    def record_webhook_event(self, name: str, payload: dict[str, Any], background: bool) -> int:
        _validate_name(name)
        with self._connect() as conn:
            cursor = conn.execute(
                "insert into webhook_events(name, payload, background) values (?, ?, ?)",
                (name, json.dumps(payload, sort_keys=True), int(background)),
            )
            return int(cursor.lastrowid)

    def mark_webhook_event_processing(self, event_id: int) -> None:
        self._update_webhook_event(event_id, "processing")

    def mark_webhook_event_completed(self, event_id: int, result: str) -> None:
        self._update_webhook_event(event_id, "completed", result_excerpt=result[:1000])

    def mark_webhook_event_failed(self, event_id: int, error: str) -> None:
        self._update_webhook_event(event_id, "failed", error=error[:1000])

    def _update_webhook_event(
        self,
        event_id: int,
        status: str,
        result_excerpt: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update webhook_events
                set status = ?,
                    result_excerpt = coalesce(?, result_excerpt),
                    error = coalesce(?, error),
                    processed_at = case when ? in ('completed', 'failed') then current_timestamp else processed_at end
                where id = ?
                """,
                (status, result_excerpt, error, status, event_id),
            )

    def list_webhook_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, name, payload, background, status, result_excerpt, error, created_at, processed_at
                from webhook_events
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "payload": json.loads(row[2]),
                "background": bool(row[3]),
                "status": row[4],
                "result_excerpt": row[5],
                "error": row[6],
                "created_at": row[7],
                "processed_at": row[8],
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists improvements (
                    id integer primary key autoincrement,
                    kind text not null,
                    name text not null,
                    description text not null,
                    data text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists webhook_hooks (
                    name text primary key,
                    prompt text not null,
                    enabled integer not null default 1,
                    updated_at text not null default current_timestamp
                );

                create table if not exists webhook_events (
                    id integer primary key autoincrement,
                    name text not null,
                    payload text not null,
                    background integer not null,
                    status text not null default 'received',
                    result_excerpt text,
                    error text,
                    created_at text not null default current_timestamp,
                    processed_at text
                );
                create index if not exists idx_webhook_events_name on webhook_events(name, id);
                """
            )
            _ensure_column(conn, "webhook_events", "status", "text not null default 'received'")
            _ensure_column(conn, "webhook_events", "result_excerpt", "text")
            _ensure_column(conn, "webhook_events", "error", "text")
            _ensure_column(conn, "webhook_events", "processed_at", "text")


def _validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError("name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")
