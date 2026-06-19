from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import CodingAgent


NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
Delivery = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class CronJob:
    name: str
    prompt: str
    channel_id: str
    every_seconds: int
    enabled: bool
    next_run_at: float


class CronStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def upsert_job(self, name: str, prompt: str, channel_id: str, every_seconds: int, enabled: bool = True) -> None:
        _validate_name(name)
        if every_seconds < 60:
            raise ValueError("cron every_seconds must be at least 60")
        if not prompt.strip():
            raise ValueError("cron prompt cannot be empty")
        next_run_at = time.time() + every_seconds
        with self._connect() as conn:
            conn.execute(
                """
                insert into cron_jobs(name, prompt, channel_id, every_seconds, enabled, next_run_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(name) do update set
                    prompt = excluded.prompt,
                    channel_id = excluded.channel_id,
                    every_seconds = excluded.every_seconds,
                    enabled = excluded.enabled,
                    next_run_at = excluded.next_run_at,
                    updated_at = current_timestamp
                """,
                (name, prompt.strip(), channel_id.strip(), every_seconds, int(enabled), next_run_at),
            )

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "update cron_jobs set enabled = ?, updated_at = current_timestamp where name = ?",
                (int(enabled), name),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown cron job: {name}")

    def get_job(self, name: str) -> CronJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "select name, prompt, channel_id, every_seconds, enabled, next_run_at from cron_jobs where name = ?",
                (name,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def due_jobs(self, now: float | None = None, limit: int = 10) -> list[CronJob]:
        now = now or time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """
                select name, prompt, channel_id, every_seconds, enabled, next_run_at
                from cron_jobs
                where enabled = 1 and next_run_at <= ?
                order by next_run_at asc
                limit ?
                """,
                (now, limit),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select name, prompt, channel_id, every_seconds, enabled, last_run_at, next_run_at, updated_at
                from cron_jobs
                order by name
                """
            ).fetchall()
        return [
            {
                "name": row[0],
                "prompt": row[1],
                "channel_id": row[2],
                "every_seconds": row[3],
                "enabled": bool(row[4]),
                "last_run_at": row[5],
                "next_run_at": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]

    def record_run(self, job: CronJob, content: str, steps: int, ok: bool = True) -> None:
        now = time.time()
        next_run_at = now + job.every_seconds
        with self._connect() as conn:
            conn.execute(
                """
                update cron_jobs
                set last_run_at = ?, next_run_at = ?, updated_at = current_timestamp
                where name = ?
                """,
                (now, next_run_at, job.name),
            )
            conn.execute(
                "insert into cron_runs(job_name, ok, steps, content) values (?, ?, ?, ?)",
                (job.name, int(ok), steps, content),
            )

    def list_runs(self, job_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "select job_name, ok, steps, content, created_at from cron_runs"
        params: list[Any] = []
        if job_name:
            query += " where job_name = ?"
            params.append(job_name)
        query += " order by id desc limit ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"job_name": row[0], "ok": bool(row[1]), "steps": row[2], "content": row[3], "created_at": row[4]}
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists cron_jobs (
                    name text primary key,
                    prompt text not null,
                    channel_id text not null,
                    every_seconds integer not null,
                    enabled integer not null default 1,
                    last_run_at real,
                    next_run_at real not null,
                    updated_at text not null default current_timestamp
                );

                create table if not exists cron_runs (
                    id integer primary key autoincrement,
                    job_name text not null,
                    ok integer not null,
                    steps integer not null,
                    content text not null,
                    created_at text not null default current_timestamp
                );
                """
            )


class CronRunner:
    def __init__(self, agent: "CodingAgent", store: CronStore, deliver: Delivery | None = None) -> None:
        self.agent = agent
        self.store = store
        self.deliver = deliver
        self._stop = asyncio.Event()

    async def serve(self, poll_seconds: int = 15) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> list[str]:
        outputs = []
        for job in self.store.due_jobs():
            outputs.append(await self.run_job(job.name))
        return outputs

    async def run_job(self, name: str) -> str:
        job = self.store.get_job(name)
        if not job:
            raise ValueError(f"Unknown cron job: {name}")
        content = f"Scheduled job `{job.name}` fired.\n\nJob instructions:\n{job.prompt}"
        try:
            if hasattr(self.agent, "run_internal_event"):
                response = await self.agent.run_internal_event(content, f"cron:{job.name}", delivery_route=job.channel_id)
            else:
                response = await self.agent.run(content, f"cron:{job.name}", job.channel_id)
        except Exception as exc:
            self.store.record_run(job, str(exc), steps=0, ok=False)
            raise
        self.store.record_run(job, response.content, response.steps, ok=True)
        if self.deliver:
            await self.deliver(job.channel_id, response.content)
        return response.content


def _row_to_job(row: sqlite3.Row | tuple[Any, ...]) -> CronJob:
    return CronJob(
        name=row[0],
        prompt=row[1],
        channel_id=row[2],
        every_seconds=int(row[3]),
        enabled=bool(row[4]),
        next_run_at=float(row[5]),
    )


def _validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError("name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens")


def dumps_cron_state(store: CronStore) -> str:
    return json.dumps({"jobs": store.list_jobs(), "runs": store.list_runs()}, sort_keys=True)
