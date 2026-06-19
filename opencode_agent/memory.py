from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")


@dataclass(slots=True)
class MemoryContext:
    summary: str
    pinned_memories: list[str]
    recent_messages: list[tuple[str, str]]
    recent_raw_messages: list[dict[str, object]]
    retrieved_memories: list[str]
    retrieved_documents: list[str]


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def add_message(self, channel_id: str, role: str, content: str, raw_message: dict[str, object] | None = None) -> None:
        raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
        with self._connect() as conn:
            conn.execute(
                "insert into messages(channel_id, role, content, raw_json) values (?, ?, ?, ?)",
                (channel_id, role, content, raw_json),
            )

    def set_last_contact(self, channel_id: str) -> None:
        self.set_contact("last_channel_id", channel_id)

    def set_contact(self, name: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into contacts(name, value) values (?, ?)
                on conflict(name) do update set value = excluded.value, updated_at = current_timestamp
                """,
                (name, value),
            )

    def get_last_contact(self) -> str | None:
        return self.get_contact("last_channel_id")

    def get_contact(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("select value from contacts where name = ?", (name,)).fetchone()
        return row[0] if row else None

    def record_heartbeat(self, channel_id: str, content: str, notified: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into heartbeat_runs(channel_id, content, notified) values (?, ?, ?)",
                (channel_id, content, int(notified)),
            )

    def add_memory(self, channel_id: str, content: str) -> None:
        content = content.strip()
        if not content:
            return
        with self._connect() as conn:
            conn.execute(
                "insert into memories(channel_id, content, keywords) values (?, ?, ?)",
                (channel_id, content, " ".join(sorted(_tokens(content)))),
            )

    def record_pinned_memory(self, content: str, limit: int = 99) -> None:
        content = content.strip()
        if not content:
            return
        with self._connect() as conn:
            conn.execute("insert into pinned_memories(content) values (?)", (content,))
            conn.execute(
                """
                delete from pinned_memories
                where id not in (
                    select id
                    from pinned_memories
                    order by id desc
                    limit ?
                )
                """,
                (max(1, limit),),
            )

    def list_pinned_memories(self, limit: int = 99) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select content
                from pinned_memories
                order by id asc
                limit ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [row[0] for row in rows]

    def index_document(self, source: str, content: str, channel_id: str = "global") -> int:
        chunks = list(_chunk_text(content))
        with self._connect() as conn:
            conn.execute("delete from document_chunks where channel_id = ? and source = ?", (channel_id, source))
            for ordinal, chunk in enumerate(chunks):
                conn.execute(
                    """
                    insert into document_chunks(channel_id, source, ordinal, content, keywords)
                    values (?, ?, ?, ?, ?)
                    """,
                    (channel_id, source, ordinal, chunk, " ".join(sorted(_tokens(chunk)))),
                )
        return len(chunks)

    def delete_document(self, source: str, channel_id: str = "global") -> int:
        with self._connect() as conn:
            cursor = conn.execute("delete from document_chunks where channel_id = ? and source = ?", (channel_id, source))
            return cursor.rowcount

    def search_documents(self, query: str, channel_id: str = "global", limit: int = 5) -> list[str]:
        channels = ("global", channel_id) if channel_id != "global" else ("global",)
        placeholders = ",".join("?" for _ in channels)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select source, ordinal, content, keywords
                from document_chunks
                where channel_id in ({placeholders})
                order by id desc
                limit 1000
                """,
                channels,
            ).fetchall()
        return self._retrieve_documents(query, rows, limit)

    def get_context(
        self,
        channel_id: str,
        query: str,
        recent_limit: int,
        recent_token_budget: int | None = None,
    ) -> MemoryContext:
        with self._connect() as conn:
            summary = conn.execute(
                "select content from summaries where channel_id = ?",
                (channel_id,),
            ).fetchone()
            recent_rows = conn.execute(
                """
                select role, content, raw_json
                from messages
                where channel_id = ?
                order by id desc
                limit ?
                """,
                (channel_id, recent_limit),
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
            pinned_memories=[],
            recent_messages=recent_messages,
            recent_raw_messages=recent_raw_messages,
            retrieved_memories=[],
            retrieved_documents=[],
        )

    def count_messages_since_summary(self, channel_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                select count(*)
                from messages
                where channel_id = ?
                  and id > coalesce((select through_message_id from summaries where channel_id = ?), 0)
                """,
                (channel_id, channel_id),
            ).fetchone()
        return int(row[0])

    def unsummarized_messages(self, channel_id: str, limit: int = 50) -> list[tuple[int, str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, role, content
                from messages
                where channel_id = ?
                  and id > coalesce((select through_message_id from summaries where channel_id = ?), 0)
                order by id asc
                limit ?
                """,
                (channel_id, channel_id, limit),
            ).fetchall()
        return [(int(row[0]), row[1], row[2]) for row in rows]

    def upsert_summary(self, channel_id: str, content: str, through_message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into summaries(channel_id, content, through_message_id)
                values (?, ?, ?)
                on conflict(channel_id) do update set
                    content = excluded.content,
                    through_message_id = excluded.through_message_id,
                    updated_at = current_timestamp
                """,
                (channel_id, content.strip(), through_message_id),
            )

    def _retrieve(self, query: str, rows: list[sqlite3.Row], limit: int) -> list[str]:
        query_terms = _tokens(query)
        if not query_terms:
            return []
        scored = []
        for content, keywords in rows:
            score = sum((Counter(keywords.split()) & Counter(query_terms)).values())
            if score:
                scored.append((score, content))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [content for _, content in scored[:limit]]

    def _retrieve_documents(self, query: str, rows: Iterable[sqlite3.Row], limit: int) -> list[str]:
        query_terms = _tokens(query)
        if not query_terms:
            return []
        query_counter = Counter(query_terms)
        scored = []
        for row in rows:
            keyword_counter = Counter(row["keywords"].split())
            overlap = keyword_counter & query_counter
            if not overlap:
                continue
            score = sum(overlap.values())
            source = row["source"]
            ordinal = row["ordinal"]
            scored.append((score, source, ordinal, f"[{source}#{ordinal}]\n{row['content']}"))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [content for _, _, _, content in scored[:limit]]

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
                    channel_id text not null,
                    role text not null,
                    content text not null,
                    raw_json text,
                    created_at text not null default current_timestamp
                );
                create index if not exists idx_messages_channel_id on messages(channel_id, id);

                create table if not exists summaries (
                    channel_id text primary key,
                    content text not null,
                    through_message_id integer not null default 0,
                    updated_at text not null default current_timestamp
                );

                create table if not exists memories (
                    id integer primary key autoincrement,
                    channel_id text not null,
                    content text not null,
                    keywords text not null,
                    created_at text not null default current_timestamp
                );
                create index if not exists idx_memories_channel_id on memories(channel_id, id);

                create table if not exists pinned_memories (
                    id integer primary key autoincrement,
                    content text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists contacts (
                    name text primary key,
                    value text not null,
                    updated_at text not null default current_timestamp
                );

                create table if not exists heartbeat_runs (
                    id integer primary key autoincrement,
                    channel_id text not null,
                    content text not null,
                    notified integer not null,
                    created_at text not null default current_timestamp
                );
                create index if not exists idx_heartbeat_runs_channel_id on heartbeat_runs(channel_id, id);

                create table if not exists document_chunks (
                    id integer primary key autoincrement,
                    channel_id text not null,
                    source text not null,
                    ordinal integer not null,
                    content text not null,
                    keywords text not null,
                    created_at text not null default current_timestamp,
                    unique(channel_id, source, ordinal)
                );
                create index if not exists idx_document_chunks_channel_source on document_chunks(channel_id, source);
                """
            )
            columns = {row["name"] for row in conn.execute("pragma table_info(messages)").fetchall()}
            if "raw_json" not in columns:
                conn.execute("alter table messages add column raw_json text")


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_RE.finditer(text)}


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


def _chunk_text(text: str, max_chars: int = 1800, overlap_chars: int = 200) -> Iterable[str]:
    cleaned = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not cleaned:
        return
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + max_chars)
        if end < len(cleaned):
            split_at = cleaned.rfind("\n\n", start, end)
            if split_at <= start:
                split_at = cleaned.rfind("\n", start, end)
            if split_at > start:
                end = split_at
        chunk = cleaned[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(cleaned):
            break
        start = max(end - overlap_chars, start + 1)
