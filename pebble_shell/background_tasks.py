from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools import ToolResult

if TYPE_CHECKING:
    from .agent import CodingAgent


ACTIVE_STATUSES = ("running", "pausing", "cancelling")
PAUSED_STATUSES = ("paused", "blocked")
TERMINAL_STATUSES = ("completed", "canceled")
MESSAGEABLE_STATUSES = ("running", "pausing", "paused", "blocked")
JOB_SELECT_COLUMNS = """
    id, title, prompt, folder, status, steps, result, error,
    created_at, updated_at, started_at, finished_at, model_calls, prompt_tokens,
    completion_tokens, total_tokens, last_model, self_check_retries, attention_summary
"""
SELF_CHECK_PROMPT = (
    "Did you verify that the original task has been completed?\n"
    "Reply with exactly one of: COMPLETE, BLOCKED, NEEDS_MORE_WORK.\n"
    "Do not include any other text."
)
NEEDS_MORE_WORK_PROMPT = (
    "You answered NEEDS_MORE_WORK. Continue working on the original task. "
    "Use tools to make concrete progress and verify the result before giving another final answer."
)
MAX_SELF_CHECK_RETRIES = 3


@dataclass(slots=True)
class BackgroundJob:
    id: str
    title: str
    prompt: str
    folder: str
    status: str
    steps: int
    result: str
    error: str
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    model_calls: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    last_model: str
    self_check_retries: int
    attention_summary: str


class BackgroundTaskStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.interrupt_running_jobs()

    def create_job(self, prompt: str, title: str, folder: str) -> BackgroundJob:
        job_id = _new_job_id()
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                insert into background_jobs(id, title, prompt, folder, status, steps, result, error,
                                            created_at, updated_at)
                values (?, ?, ?, ?, 'running', 0, '', '', ?, ?)
                """,
                (job_id, title.strip() or prompt.strip()[:80] or job_id, prompt.strip(), folder, now, now),
            )
        self.add_event(job_id, "running", "Background worker created and scheduled.")
        job = self.get_job(job_id)
        if not job:
            raise RuntimeError(f"Failed to create background job {job_id}")
        return job

    def start_job(self, job_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                update background_jobs
                set status = 'running', started_at = coalesce(started_at, ?), updated_at = ?
                where id = ? and status = 'running'
                """,
                (now, now, job_id),
            )
        self.add_event(job_id, "started", "Background worker started.")

    def complete_job(self, job_id: str, result: str, steps: int) -> None:
        self._finish(job_id, "completed", result=result, error="", steps=steps)
        self.add_event(job_id, "completed", result[:4000])

    def block_job(self, job_id: str, result: str, steps: int, summary: str = "") -> None:
        self._finish(job_id, "blocked", result=result, error="", steps=steps, attention_summary=summary)
        self.add_event(job_id, "blocked", (summary or result)[:4000])

    def fail_job(self, job_id: str, error: str, steps: int = 0) -> None:
        self._finish(job_id, "blocked", result="", error=error, steps=steps, attention_summary=error[:1000])
        self.add_event(job_id, "blocked", error[:4000])

    def cancel_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown background job: {job_id}")
        if job.status in TERMINAL_STATUSES:
            return
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "update background_jobs set status = 'cancelling', updated_at = ? where id = ?",
                (now, job_id),
            )
        self.add_event(job_id, "cancelling", "Cancellation requested.")

    def pause_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown background job: {job_id}")
        if job.status in TERMINAL_STATUSES:
            return
        now = time.time()
        with self._connect() as conn:
            conn.execute("update background_jobs set status = 'pausing', updated_at = ? where id = ?", (now, job_id))
        self.add_event(job_id, "pausing", "Pause requested.")

    def mark_paused(self, job_id: str, steps: int) -> None:
        self._finish(job_id, "paused", result="", error="", steps=steps)
        self.add_event(job_id, "paused", "Background worker paused.")

    def mark_cancelled(self, job_id: str, steps: int) -> None:
        self._finish(job_id, "canceled", result="", error="", steps=steps)
        self.add_event(job_id, "canceled", "Background worker canceled.")

    def interrupt_running_jobs(self) -> None:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                f"select id from background_jobs where status in ({','.join('?' for _ in ACTIVE_STATUSES)})",
                ACTIVE_STATUSES,
            ).fetchall()
            conn.execute(
                f"update background_jobs set status = 'blocked', finished_at = ?, updated_at = ?, "
                f"error = 'Service restarted before this background task completed.' "
                f"where status in ({','.join('?' for _ in ACTIVE_STATUSES)})",
                (now, now, *ACTIVE_STATUSES),
            )
        for row in rows:
            self.add_event(row[0], "blocked", "Service restarted before this background task completed.")

    def get_job(self, job_id: str) -> BackgroundJob | None:
        with self._connect() as conn:
            row = conn.execute(
                f"select {JOB_SELECT_COLUMNS} from background_jobs where id = ?",
                (job_id,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(self, limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        query = f"select {JOB_SELECT_COLUMNS} from background_jobs"
        params: list[Any] = []
        if status:
            query += " where status = ?"
            params.append(status)
        query += " order by created_at desc limit ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_job_dict(_row_to_job(row), include_prompt=False) for row in rows]

    def count_active(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                f"select count(*) from background_jobs where status in ({','.join('?' for _ in ACTIVE_STATUSES)})",
                ACTIVE_STATUSES,
            ).fetchone()
        return int(row[0])

    def active_jobs(self) -> list[dict[str, Any]]:
        jobs = []
        for status in ACTIVE_STATUSES:
            jobs.extend(self.list_jobs(limit=100, status=status))
        return sorted(jobs, key=lambda item: float(item["created_at"]), reverse=True)

    def add_event(self, job_id: str, kind: str, message: str, payload: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into background_events(job_id, kind, message, payload) values (?, ?, ?, ?)",
                (job_id, kind, message, json.dumps(payload or {}, sort_keys=True)),
            )

    def list_events(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select kind, message, payload, created_at
                from background_events
                where job_id = ?
                order by id desc
                limit ?
                """,
                (job_id, limit),
            ).fetchall()
        return [
            {
                "kind": row[0],
                "message": row[1],
                "payload": json.loads(row[2] or "{}"),
                "created_at": row[3],
            }
            for row in rows
        ]

    def enqueue_message(self, job_id: str, message: str) -> None:
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown background job: {job_id}")
        if job.status not in MESSAGEABLE_STATUSES:
            raise ValueError(f"Background job is not messageable: {job_id} has status {job.status}")
        message = message.strip()
        if not message:
            raise ValueError("background task message cannot be empty")
        with self._connect() as conn:
            conn.execute(
                "insert into background_messages(job_id, content) values (?, ?)",
                (job_id, message),
            )
            if job.status in PAUSED_STATUSES:
                conn.execute(
                    """
                    update background_jobs
                    set status = 'running', finished_at = null, self_check_retries = 0, attention_summary = '', updated_at = ?
                    where id = ?
                    """,
                    (time.time(), job_id),
                )
        self.add_event(job_id, "message_queued", message[:4000])

    def drain_messages(self, job_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, content
                from background_messages
                where job_id = ? and delivered_at is null
                order by id asc
                """,
                (job_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    f"update background_messages set delivered_at = current_timestamp where id in ({','.join('?' for _ in rows)})",
                    tuple(row["id"] for row in rows),
                )
        return [row["content"] for row in rows]

    def save_context(self, job_id: str, messages: list[dict[str, object]]) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "update background_jobs set context_json = ?, updated_at = ? where id = ?",
                (payload, now, job_id),
            )

    def set_summary(self, job_id: str, summary: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "update background_jobs set summary = ?, updated_at = ? where id = ?",
                (summary.strip(), now, job_id),
            )

    def get_summary(self, job_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("select summary from background_jobs where id = ?", (job_id,)).fetchone()
        return str(row[0] or "") if row else ""

    def get_context(self, job_id: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            row = conn.execute("select context_json from background_jobs where id = ?", (job_id,)).fetchone()
        if not row or not row[0]:
            return []
        return json.loads(row[0])

    def should_cancel(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.status == "cancelling")

    def should_pause(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.status == "pausing")

    def increment_self_check_retries(self, job_id: str) -> int:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "update background_jobs set self_check_retries = self_check_retries + 1, updated_at = ? where id = ?",
                (now, job_id),
            )
            row = conn.execute("select self_check_retries from background_jobs where id = ?", (job_id,)).fetchone()
        return int(row[0]) if row else 0

    def record_model_usage(
        self,
        job_id: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                update background_jobs
                set model_calls = model_calls + 1,
                    prompt_tokens = case when ? is null then prompt_tokens else coalesce(prompt_tokens, 0) + ? end,
                    completion_tokens = case when ? is null then completion_tokens else coalesce(completion_tokens, 0) + ? end,
                    total_tokens = case when ? is null then total_tokens else coalesce(total_tokens, 0) + ? end,
                    last_model = ?,
                    updated_at = ?
                where id = ?
                """,
                (
                    prompt_tokens,
                    prompt_tokens,
                    completion_tokens,
                    completion_tokens,
                    total_tokens,
                    total_tokens,
                    model,
                    now,
                    job_id,
                ),
            )

    def _finish(
        self,
        job_id: str,
        status: str,
        result: str,
        error: str,
        steps: int,
        attention_summary: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                update background_jobs
                set status = ?, result = ?, error = ?, steps = ?, finished_at = ?, updated_at = ?,
                    attention_summary = case when ? is null then attention_summary else ? end
                where id = ?
                """,
                (status, result, error, steps, now, now, attention_summary, attention_summary, job_id),
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
                create table if not exists background_jobs (
                    id text primary key,
                    title text not null,
                    prompt text not null,
                    folder text not null,
                    status text not null,
                    steps integer not null default 0,
                    result text not null default '',
                    error text not null default '',
                    summary text not null default '',
                    context_json text not null default '[]',
                    created_at real not null,
	                    updated_at real not null,
	                    started_at real,
	                    finished_at real,
	                    model_calls integer not null default 0,
	                    prompt_tokens integer,
	                    completion_tokens integer,
	                    total_tokens integer,
	                    last_model text not null default '',
	                    self_check_retries integer not null default 0,
	                    attention_summary text not null default ''
	                );

                create table if not exists background_events (
                    id integer primary key autoincrement,
                    job_id text not null,
                    kind text not null,
                    message text not null,
                    payload text not null default '{}',
                    created_at text not null default current_timestamp
                );

                create table if not exists background_messages (
                    id integer primary key autoincrement,
                    job_id text not null,
                    content text not null,
                    created_at text not null default current_timestamp,
                    delivered_at text
                );

                create index if not exists idx_background_jobs_status on background_jobs(status, created_at);
                create index if not exists idx_background_events_job on background_events(job_id, id);
                create index if not exists idx_background_messages_job on background_messages(job_id, id);
                """
            )
            columns = {row["name"] for row in conn.execute("pragma table_info(background_jobs)").fetchall()}
            if "summary" not in columns:
                conn.execute("alter table background_jobs add column summary text not null default ''")
            migrations = {
                "model_calls": "alter table background_jobs add column model_calls integer not null default 0",
                "prompt_tokens": "alter table background_jobs add column prompt_tokens integer",
                "completion_tokens": "alter table background_jobs add column completion_tokens integer",
                "total_tokens": "alter table background_jobs add column total_tokens integer",
                "last_model": "alter table background_jobs add column last_model text not null default ''",
                "self_check_retries": "alter table background_jobs add column self_check_retries integer not null default 0",
                "attention_summary": "alter table background_jobs add column attention_summary text not null default ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)


class BackgroundTaskService:
    def __init__(self, agent: "CodingAgent", store: BackgroundTaskStore, max_active: int = 4) -> None:
        self.agent = agent
        self.store = store
        self.max_active = max(1, max_active)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, concurrent.futures.Future[None]] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self, prompt: str, folder: str) -> ToolResult:
        prompt = prompt.strip()
        if not prompt:
            return ToolResult(ok=False, output="background task prompt cannot be empty")
        try:
            folder = _normalize_workspace_folder(folder)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if self.store.count_active() >= self.max_active:
            return ToolResult(ok=False, output=f"Maximum active background tasks reached: {self.max_active}")
        loop = self._loop
        if loop is None or loop.is_closed():
            return ToolResult(ok=False, output="Background task runner is not attached to a running event loop")
        title = prompt[:80]
        job = self.store.create_job(prompt, title, folder)
        (self.agent.settings.agent_workspace / folder).mkdir(parents=True, exist_ok=True)
        self._schedule_job(job.id)
        self._notify_created(job.id, folder, prompt)
        return ToolResult(ok=True, output=json.dumps(self.status(job.id), sort_keys=True))

    def _notify_created(self, job_id: str, folder: str, prompt: str) -> None:
        if not self.agent._deliver or not self._loop or self._loop.is_closed():
            return
        excerpt = prompt.replace("\n", " ")[:100]
        self._loop.create_task(self.agent._deliver(f"[background agent created] id={job_id} folder=/{folder} prompt={excerpt}"))

    def status(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown background job: {job_id}")
        result = _job_dict(job, include_prompt=True)
        result["events"] = self.store.list_events(job_id, limit=10)
        return result

    def status_tool(self, job_id: str) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(self.status(job_id), sort_keys=True))

    def list_tool(self, limit: int = 10, status: str | None = None) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(self.store.list_jobs(limit, status), sort_keys=True))

    def status_table_tool(self, limit: int = 10, status: str | None = None) -> ToolResult:
        loop = self._loop
        if loop is None or loop.is_closed():
            return ToolResult(ok=False, output="Background task runner is not attached to a running event loop")
        future = asyncio.run_coroutine_threadsafe(self.status_table(limit, status), loop)
        return ToolResult(ok=True, output=json.dumps(future.result(), sort_keys=True))

    async def status_yaml(self, limit: int = 10, status: str | None = None) -> str:
        table = await self.status_table(limit, status)
        return _render_status_yaml(table["jobs"], limit, status)

    async def status_table(self, limit: int = 10, status: str | None = None) -> dict[str, Any]:
        rows = []
        flash_available = True
        for item in self.store.list_jobs(limit, status):
            job = self.store.get_job(str(item["id"]))
            if not job:
                continue
            events = self.store.list_events(job.id, limit=20)
            if flash_available:
                try:
                    activity = await self._summarize_recent_activity(job, events)
                except Exception:  # noqa: BLE001 - one flash outage should not slow every row in the table.
                    flash_available = False
                    activity = _fallback_recent_activity(job, events)
            else:
                activity = _fallback_recent_activity(job, events)
            rows.append(_status_row(job, events, activity))
        return {"markdown": _render_status_table(rows), "jobs": rows}

    def events_tool(self, job_id: str, limit: int = 20) -> ToolResult:
        if not self.store.get_job(job_id):
            return ToolResult(ok=False, output=f"Unknown background job: {job_id}")
        return ToolResult(ok=True, output=json.dumps(self.store.list_events(job_id, limit), sort_keys=True))

    def cancel_tool(self, job_id: str) -> ToolResult:
        self.store.cancel_job(job_id)
        return ToolResult(ok=True, output=json.dumps(self.status(job_id), sort_keys=True))

    def pause_tool(self, job_id: str) -> ToolResult:
        self.store.pause_job(job_id)
        return ToolResult(ok=True, output=json.dumps(self.status(job_id), sort_keys=True))

    def progress_sender(self, job_id: str):
        def send(text: str) -> str:
            message = text.strip()
            if not message:
                raise ValueError("progress message cannot be empty")
            self.store.add_event(job_id, "progress", message[:4000])
            loop = self._loop
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(self._wake_foreground(job_id, "progress", message), loop)
            return "Sent progress update to foreground Pebble"

        return send

    def message_tool(self, job_id: str, message: str) -> ToolResult:
        self.store.enqueue_message(job_id, message)
        job = self.store.get_job(job_id)
        if job and job.status == "running":
            self._schedule_job(job_id)
        return ToolResult(ok=True, output=f"Queued message for background job {job_id}")

    def ask_tool(self, job_id: str, question: str) -> ToolResult:
        loop = self._loop
        if loop is None or loop.is_closed():
            return ToolResult(ok=False, output="Background task runner is not attached to a running event loop")
        future = asyncio.run_coroutine_threadsafe(self.ask(job_id, question), loop)
        return ToolResult(ok=True, output=future.result())

    async def ask(self, job_id: str, question: str) -> str:
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown background job: {job_id}")
        context = self.store.get_context(job_id)
        question = question.strip()
        if not question:
            raise ValueError("background task question cannot be empty")
        messages = [
            {
                "role": "system",
                "content": (
                    "You answer questions about one background worker run. Use only the provided stored context. "
                    "You have no tools. If the context does not contain the answer, say that plainly."
                ),
            },
            {
                "role": "user",
                "content": f"Background job {job.id} ({job.title}) stored context:\n{json.dumps(context, ensure_ascii=False)}\n\nQuestion: {question}",
            },
        ]
        try:
            response = await self.agent._chat_completion(messages=messages, tool_choice="none", source=f"background:{job.id}")
            return response.choices[0].message.content or ""
        except Exception:
            return await self._ask_chunked(job, context, question)

    async def _ask_chunked(self, job: BackgroundJob, context: list[dict[str, object]], question: str) -> str:
        chunks = _chunk_context(context)
        extracts = []
        for index, chunk in enumerate(chunks, start=1):
            response = await self.agent._chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "Extract only facts relevant to the question from this background worker context chunk. No tools.",
                    },
                    {"role": "user", "content": f"Question: {question}\n\nChunk {index}/{len(chunks)}:\n{chunk}"},
                ],
                tool_choice="none",
                source=f"background:{job.id}",
            )
            extracts.append(response.choices[0].message.content or "")
        response = await self.agent._chat_completion(
            messages=[
                {"role": "system", "content": "Answer the question using only these extracted facts. No tools."},
                {"role": "user", "content": f"Job {job.id}: {job.title}\nQuestion: {question}\n\nExtracts:\n" + "\n\n".join(extracts)},
            ],
            tool_choice="none",
            source=f"background:{job.id}",
        )
        return response.choices[0].message.content or ""

    async def summarize_attention(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if not job:
            return f"Unknown background job: {job_id}"
        events = self.store.list_events(job_id, limit=30)
        context = self.store.get_context(job_id)[-20:]
        try:
            response = await self.agent._flash_completion(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize a background worker needing foreground attention. No tools. "
                            "Explain what went wrong, what happened in the last few steps, evidence, and a practical next action. "
                            "Keep it concise."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Job: {job.id}\nTitle: {job.title}\nStatus: {job.status}\nTask: {job.prompt}\n\n"
                            f"Recent events:\n{json.dumps(events, ensure_ascii=False)}\n\n"
                            f"Recent context:\n{json.dumps(context, ensure_ascii=False)}"
                        ),
                    },
                ],
                tool_choice="none",
            )
            return response.choices[0].message.content or _fallback_attention_summary(job, events)
        except Exception:  # noqa: BLE001 - attention state must not become a job failure because flash is unavailable.
            return _fallback_attention_summary(job, events)

    async def _summarize_recent_activity(self, job: BackgroundJob, events: list[dict[str, Any]]) -> str:
        context = self.store.get_context(job.id)[-12:]
        response = await self.agent._flash_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarize one background worker for a status table. No tools. "
                        "Return one compact Markdown sentence in this exact shape: "
                        "Done: <recent concrete completed work with evidence>. Now: <current focus or terminal state>. "
                        "Use only provided context/events. If only startup was reported, say no concrete work is verified. "
                        "Keep under 220 characters."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Job: {job.id}\nTitle: {job.title}\nStatus: {job.status}\nResult: {job.result[:1200]}\n"
                        f"Error: {job.error[:1200]}\nRecent events:\n{json.dumps(events, ensure_ascii=False)}\n"
                        f"Recent context:\n{json.dumps(context, ensure_ascii=False)}"
                    ),
                },
            ],
            tool_choice="none",
        )
        return _single_line(response.choices[0].message.content or "")

    def _schedule_job(self, job_id: str) -> bool:
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        if job_id in self._tasks and not self._tasks[job_id].done():
            return True
        self._tasks[job_id] = asyncio.run_coroutine_threadsafe(self._run_job(job_id), loop)  # type: ignore[assignment]
        return True

    async def _run_job(self, job_id: str) -> None:
        steps = 0
        try:
            self.store.start_job(job_id)
            job = self.store.get_job(job_id)
            if not job:
                raise ValueError(f"Unknown background job: {job_id}")
            while True:
                response = await self.agent.run_background_task(job)
                steps += response.steps
                if self.store.should_cancel(job_id):
                    self.store.mark_cancelled(job_id, steps)
                    await self._wake_foreground(job_id, "canceled")
                    return
                if self.store.should_pause(job_id):
                    self.store.mark_paused(job_id, steps)
                    await self._wake_foreground(job_id, "paused")
                    return
                decision = await self._self_check(job_id, response.content)
                if decision == "COMPLETE":
                    self.store.complete_job(job_id, response.content, steps)
                    await self._wake_foreground(job_id, "completed")
                    return
                if decision == "BLOCKED":
                    summary = await self.summarize_attention(job_id)
                    self.store.block_job(job_id, response.content, steps, summary)
                    await self._wake_foreground(job_id, "blocked")
                    return
                retries = self.store.increment_self_check_retries(job_id)
                self.store.add_event(job_id, "self_check_needs_more_work", f"Retry {retries}/{MAX_SELF_CHECK_RETRIES}")
                if retries >= MAX_SELF_CHECK_RETRIES:
                    summary = await self.summarize_attention(job_id)
                    self.store.block_job(job_id, response.content, steps, summary)
                    await self._wake_foreground(job_id, "blocked")
                    return
                self.store.enqueue_message(job_id, NEEDS_MORE_WORK_PROMPT)
                job = self.store.get_job(job_id)
                if not job:
                    raise ValueError(f"Unknown background job: {job_id}")
        except asyncio.CancelledError:
            self.store.mark_cancelled(job_id, steps)
            await self._wake_foreground(job_id, "canceled")
            raise
        except Exception as exc:  # noqa: BLE001 - background failures should be captured in job state.
            self.store.fail_job(job_id, str(exc), steps)
            await self._wake_foreground(job_id, "blocked")
        finally:
            self._tasks.pop(job_id, None)

    async def _self_check(self, job_id: str, final_answer: str) -> str:
        context = self.store.get_context(job_id)
        if not context or context[-1].get("role") != "assistant" or context[-1].get("content") != final_answer:
            context.append({"role": "assistant", "content": final_answer})
        context.append({"role": "user", "content": SELF_CHECK_PROMPT})
        self.store.save_context(job_id, context)
        response = await self.agent._chat_completion(
            messages=context,
            tool_choice="none",
            background_job_id=job_id,
            source=f"background:{job_id}",
        )
        decision = _normalize_self_check(response.choices[0].message.content or "")
        context.append({"role": "assistant", "content": decision})
        self.store.save_context(job_id, context)
        self.store.add_event(job_id, "self_check", decision)
        return decision

    async def _wake_foreground(self, job_id: str, event: str, progress_message: str = "") -> None:
        job = self.store.get_job(job_id)
        if not job:
            return
        prefix = f"[background agent {event}] id={job.id} folder=/{job.folder}"
        progress = f"Progress message: {progress_message[:4000]}\n\n" if progress_message else ""
        prompt = (
            f"{prefix}\n\n"
            f"Background task `{job.id}` emitted `{event}`.\n\n"
            f"{progress}"
            f"Title: {job.title}\n"
            f"Status: {job.status}\n"
            f"Folder: {job.folder}\n"
            f"Result: {job.result[:4000] or '(none)'}\n"
            f"Error: {job.error[:4000] or '(none)'}\n\n"
            f"Attention summary: {job.attention_summary[:4000] or '(none)'}\n\n"
            "You are the foreground supervisor. Decide what, if anything, to tell the user."
        )
        try:
            response = await self.agent.run_internal_event(prompt, f"background:{job.id}")
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(job_id, "foreground_wakeup_failed", str(exc))
            return
        self.store.add_event(job_id, "foreground_wakeup", response.content[:4000])
        if self.agent._deliver and response.content.strip():
            await self.agent._deliver(response.content)


def _new_job_id() -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"bg_{day}_{secrets.token_hex(3)}"


def _normalize_workspace_folder(folder: str) -> str:
    raw = folder.strip()
    if not raw:
        raise ValueError("background task folder cannot be empty")
    normalized = Path(raw.lstrip("/"))
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise ValueError("background task folder must stay inside /workspace and cannot contain . or .. path segments")
    return normalized.as_posix()


def _row_to_job(row: sqlite3.Row) -> BackgroundJob:
    return BackgroundJob(
        id=row["id"],
        title=row["title"],
        prompt=row["prompt"],
        folder=row["folder"],
        status=row["status"],
        steps=int(row["steps"]),
        result=row["result"] or "",
        error=row["error"] or "",
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        model_calls=int(row["model_calls"] or 0),
        prompt_tokens=int(row["prompt_tokens"]) if row["prompt_tokens"] is not None else None,
        completion_tokens=int(row["completion_tokens"]) if row["completion_tokens"] is not None else None,
        total_tokens=int(row["total_tokens"]) if row["total_tokens"] is not None else None,
        last_model=row["last_model"] or "",
        self_check_retries=int(row["self_check_retries"] or 0),
        attention_summary=row["attention_summary"] or "",
    )


def _job_dict(job: BackgroundJob, include_prompt: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": job.id,
        "title": job.title,
        "folder": job.folder,
        "status": job.status,
        "steps": job.steps,
        "result": job.result[:2000],
        "error": job.error[:2000],
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "model_calls": job.model_calls,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": job.completion_tokens,
        "total_tokens": job.total_tokens,
        "last_model": job.last_model,
        "self_check_retries": job.self_check_retries,
        "attention_summary": job.attention_summary[:2000],
    }
    if include_prompt:
        data["prompt"] = job.prompt
    return data


def _status_row(job: BackgroundJob, events: list[dict[str, Any]], recent_activity: str) -> dict[str, Any]:
    flags = _job_flags(job, events)
    return {
        "job_id": job.id,
        "title": job.title,
        "model": job.last_model or "unknown",
        "status": _display_status(job),
        "elapsed": _elapsed(job),
        "steps": job.steps,
        "model_calls": job.model_calls,
        "tokens": str(job.total_tokens) if job.total_tokens is not None else "unknown",
        "tool_calls": sum(1 for event in events if event["kind"] == "tool_call"),
        "recent_activity": recent_activity,
        "flags": flags,
        "suspicious_completion": "early-complete" in flags,
    }


def _render_status_table(rows: list[dict[str, Any]]) -> str:
    header = "| job_id | model | status | elapsed | steps | tokens | recent_activity | flags |"
    sep = "|---|---|---:|---:|---:|---:|---|---|"
    if not rows:
        return header + "\n" + sep + "\n"
    lines = [header, sep]
    for row in rows:
        lines.append(
            "| {job_id} | {model} | {status} | {elapsed} | {steps} | {tokens} | {recent_activity} | {flags} |".format(
                job_id=_md_cell(str(row["job_id"])),
                model=_md_cell(str(row["model"])),
                status=_md_cell(str(row["status"])),
                elapsed=_md_cell(str(row["elapsed"])),
                steps=row["steps"],
                tokens=_md_cell(str(row["tokens"])),
                recent_activity=_md_cell(str(row["recent_activity"])),
                flags=_md_cell(", ".join(row["flags"]) or "none"),
            )
        )
    return "\n".join(lines)


def _render_status_yaml(rows: list[dict[str, Any]], limit: int, status: str | None) -> str:
    lines = [
        "background_agents:",
        f"  limit: {max(1, min(int(limit), 100))}",
        f"  status_filter: {_yaml_scalar(status or 'all')}",
        f"  count: {len(rows)}",
        "  jobs:",
    ]
    if not rows:
        lines.append("    []")
        return "\n".join(lines)
    for row in rows:
        lines.extend(
            [
                f"    - job_id: {_yaml_scalar(str(row['job_id']))}",
                f"      title: {_yaml_scalar(str(row.get('title', '')))}",
                f"      model: {_yaml_scalar(str(row['model']))}",
                f"      status: {_yaml_scalar(str(row['status']))}",
                f"      elapsed: {_yaml_scalar(str(row['elapsed']))}",
                f"      steps: {int(row['steps'])}",
                f"      model_calls: {int(row.get('model_calls', 0))}",
                f"      tokens: {_yaml_scalar(str(row['tokens']))}",
                f"      tool_calls: {int(row.get('tool_calls', 0))}",
                f"      suspicious_completion: {_yaml_bool(bool(row.get('suspicious_completion')))}",
                "      flags:",
            ]
        )
        flags = row.get("flags") or []
        if flags:
            lines.extend(f"        - {_yaml_scalar(str(flag))}" for flag in flags)
        else:
            lines.append("        []")
        lines.extend(
            [
                "      recent_activity: >-",
                f"        {_single_line(str(row['recent_activity']))}",
            ]
        )
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    text = _single_line(value)
    if text == "":
        return '""'
    if text.lower() in {"true", "false", "null"}:
        return json.dumps(text)
    if any(char in text for char in ":#{}[]&,*?|-<>=!%@`'\"\\") or text.strip() != text:
        return json.dumps(text)
    return text


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def _display_status(job: BackgroundJob) -> str:
    if job.status == "completed" and _looks_blocked(job.result):
        return "blocked?"
    return job.status


def _job_flags(job: BackgroundJob, events: list[dict[str, Any]]) -> list[str]:
    flags = []
    if job.steps <= 2 and job.status in {"completed", "blocked"}:
        text = job.result.lower()
        if any(phrase in text for phrase in ("worker started", "running now", "i'll check", "i will check", "started.")):
            flags.append("early-complete")
    if job.self_check_retries >= MAX_SELF_CHECK_RETRIES:
        flags.append("retry-cap")
    if _looks_blocked(job.result) and job.status == "completed":
        flags.append("blocked-text")
    if any("quote-api.jup.ag" in event["message"] or "/v6" in event["message"] for event in events):
        flags.append("stale-api")
    return flags


def _fallback_recent_activity(job: BackgroundJob, events: list[dict[str, Any]]) -> str:
    done = "no concrete work is verified"
    if job.status == "completed" and job.result:
        done = _single_line(job.result[:120])
    else:
        for event in events:
            if event["kind"] in {"completed", "tool_call", "self_check", "blocked", "paused", "canceled"}:
                done = f"{event['kind']}: {_single_line(event['message'])[:100]}"
                break

    if job.status in TERMINAL_STATUSES or job.status in PAUSED_STATUSES:
        now = f"{job.status}; inspect `{job.id}` for details"
    elif job.status == "running":
        now = "running"
    else:
        now = job.status
    return f"Done: {done}. Now: {now}."


def _fallback_attention_summary(job: BackgroundJob, events: list[dict[str, Any]]) -> str:
    latest = next((event for event in events if event["kind"] not in {"started", "running"}), None)
    latest_text = "no recent diagnostic event"
    if latest:
        latest_text = f"{latest['kind']}: {_single_line(latest['message'])[:500]}"
    result = _single_line(job.result)[:500] or "(no final worker result)"
    return (
        f"Background job {job.id} needs foreground attention. "
        f"Status: {job.status}. Latest event: {latest_text}. "
        f"Worker result: {result}. "
        "Next action: inspect the job events/context, then send a focused background_task_message or cancel it."
    )


def _looks_blocked(text: str) -> bool:
    return any(marker in text.lower() for marker in ("blocked", "cannot complete", "can't complete", "failed due"))


def _elapsed(job: BackgroundJob) -> str:
    if job.started_at is None:
        return _format_duration(max(0, time.time() - job.created_at))
    end = job.finished_at or (time.time() if job.status in ACTIVE_STATUSES else job.updated_at)
    return _format_duration(max(0, end - job.started_at))


def _format_duration(seconds: float) -> str:
    seconds_i = int(seconds)
    if seconds_i < 60:
        return f"{seconds_i}s"
    minutes, sec = divmod(seconds_i, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _md_cell(text: str) -> str:
    return _single_line(text).replace("|", "\\|")


def _single_line(text: str) -> str:
    return " ".join(text.strip().split())


def _normalize_self_check(text: str) -> str:
    normalized = text.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized.startswith("COMPLETE"):
        return "COMPLETE"
    if normalized.startswith("BLOCKED"):
        return "BLOCKED"
    if normalized.startswith("NEEDS_MORE_WORK") or normalized.startswith("NEED_MORE_WORK"):
        return "NEEDS_MORE_WORK"
    return "NEEDS_MORE_WORK"


def _chunk_context(context: list[dict[str, object]], max_chars: int = 24000) -> list[str]:
    chunks: list[str] = []
    current: list[dict[str, object]] = []
    current_size = 0
    for message in context:
        rendered = json.dumps(message, ensure_ascii=False)
        if current and current_size + len(rendered) > max_chars:
            chunks.append(json.dumps(current, ensure_ascii=False))
            current = []
            current_size = 0
        current.append(message)
        current_size += len(rendered)
    if current:
        chunks.append(json.dumps(current, ensure_ascii=False))
    return chunks or ["[]"]
