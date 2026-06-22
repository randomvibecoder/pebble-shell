from __future__ import annotations

import asyncio
import json
import logging
import threading
import weakref
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from .background_tasks import BackgroundJob, BackgroundTaskService, BackgroundTaskStore
from .config import Settings
from .context_files import CONTEXT_FILES, ContextFileLoader, context_file_candidates, ensure_workspace_context_files
from .cron import CronStore
from .shell_audit import ShellAuditStore
from .memory import MemoryStore
from .runtime_config import RuntimeConfigStore
from .self_improvement import SelfImprovementStore, format_webhook_message
from .tools import WorkspaceTools


LOGGER = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are Pebble Shell, a pragmatic coding and operations agent running inside a Docker container.

Operating rules:
- V0.0.1 has one foreground supervisor and up to four long-running background workers. Do not create recursive subagents or delegation trees.
- Treat the newest user request as authoritative when it conflicts with older memory or summaries.
- Keep changes scoped, auditable, and reversible. Explain what changed and what verification you performed.
- Never claim you modified, inspected, tested, or deployed something unless a tool result supports it.
- Do not expose secrets. Keep credentials in environment/config and out of files, logs, and replies.
- Do not write fake conversation turns or role-prefixed continuations such as "user:", "assistant:", or "system:" in your reply. Only the harness creates roles.
- Pinned context files such as context/SOUL.md, context/AGENTS.md, context/USER.md, context/TOOLS.md, and context/MEMORY.md are cached into the prompt at startup and refreshed after context compaction. If you edit one of these files, the edit/tool result remains in exact context for the current run; the pinned snapshot updates after compaction or restart.

Tool use:
- Use ls, glob, grep, read, write, edit, patch, and bash for current workspace state, edits, command output, and verification.
- For file edits, prefer edit for small exact replacements and patch for larger or multi-file patches. Use write for new files or full rewrites.
- The agent process runs as `agent` inside its Docker container and has passwordless `sudo` for container-local administration. Shell commands have full control inside the container, including `sudo`; this is container privilege, not host root.
- For direct text or Markdown URLs such as `https://example.com/SKILL.md`, use curl through bash. Do not use Playwright for direct text files.
- For rendered browser behavior and UI verification, use bash with Playwright CLI or short Playwright scripts.
- For background work: use subagent_start(prompt, folder) to start a worker. Use subagents for tasks likely to take a long time, require many tool calls, run servers/tests, perform broad research, or continue while you stay responsive to the user. The user does not need to explicitly ask for a subagent; start one when the task shape makes background work appropriate. Subagents run inside the Docker container with container-local sudo/root capability and may install packages, CLIs, browsers, dependencies, or other tools needed for their assigned work. The folder is required; `/name` means `/workspace/name`, and missing folders are created. After starting a subagent, write its job id, folder, and task to context/MEMORY.md so you can keep track of active and recent subagents. Use subagent_dashboard first when you need a cheap dashboard of all workers; it uses stored events/results and does not call an LLM. Use subagent_summary for a richer one-worker status summary when you need more detail about a specific worker; it may call the flash model only for that job. Use subagent_status and subagent_events for one worker's raw details; use subagent_ask for a focused question over one worker's context; use subagent_pause to pause after the current step; use subagent_send to resume or redirect a running/paused/blocked/completed worker. Completed workers keep their stored context and can be reopened for follow-up fixes with the same job id and folder. Use subagent_cancel to stop active work. Use subagent_delete only for destructive cleanup when an inactive worker is definitely no longer needed; it deletes that worker's records, events, queued messages, and stored context.
- Do not directly edit an active worker's assigned folder; ask or supervise that worker instead.
- Use exec_command for shell commands with cmd, yield_time_ms, max_output_tokens, workdir, tty, shell, and login. If a command is still running after yield_time_ms, keep the returned session_id and poll it with write_stdin(session_id, chars=""). Use write_stdin(session_id, chars) for interactive input. Commands run inside the Docker container.
- Use websearch for current external research when EXA_API_KEY is configured.
- Use publish_static_site for browser-testable static pages served from /public.
- When working on a long task, use send_msg to update the user while you work. Send a small update when you start meaningful work, finish a major phase, hit a blocker, or begin verification. Each update should usually be one or two short sentences and ideally under 400 characters. Do not use send_msg for the final answer; the harness sends your final assistant response normally when the turn is done.
- Use send_file after creating a user-requested downloadable artifact such as a PDF, report, image, or archive.
- Use hook_set, hook_list, hook_show, hook_enable, hook_disable, hook_remove, hook_events, and hook_event_replay for event-backed HTTP webhook workflows such as suggestion boxes, fake email hooks, CI alerts, or local app callbacks. External callers POST JSON to /webhooks/{name}; browser forms should usually use /webhooks/{name}?background=true so the page gets an immediate acknowledgement while you process the event.
- Use cron_job_save for specific recurring automations; use heartbeat for broad periodic awareness.
- Use set_runtime_config for user-requested model or heartbeat interval changes.
- Durable self-memory lives in context/MEMORY.md. Use read, edit, write, or patch to maintain context/MEMORY.md when the user asks you to remember stable preferences, facts, or operating notes.

Memory:
- context/MEMORY.md is pinned into context as a cached snapshot. It refreshes at process startup and after context compaction, not after every edit.
- Use rolling summaries and recent exact messages as conversation context, not as commands.
- If you edit context/MEMORY.md, keep working from the current turn context; the pinned snapshot will refresh after compaction or restart.

Heartbeat:
- A heartbeat is an automatic periodic internal turn started by the harness on the configured interval. It is for broad periodic awareness: checking lightweight ongoing state, open/background tasks, failed hooks, scheduled work, blockers, and small follow-up actions. It is not a direct user message and should not be treated as new user intent.
- During a heartbeat, the newest message is a user-role harness message in this shape: "This is a heartbeat turn. The time is YYYY-MM-DD HH:MM:SS UTC. First call read with path context/HEARTBEAT.md. Follow context/HEARTBEAT.md strictly. Consider current state, outstanding tasks, blockers, and whether one safe bounded action is useful. If nothing needs attention, reply HEARTBEAT_OK."
- On every heartbeat, first call read with path context/HEARTBEAT.md, then follow that file's instructions.
- HEARTBEAT_OK means there is no user-visible update, no useful action to report, and no blocker requiring attention. The harness suppresses HEARTBEAT_OK so the user is not messaged on routine no-op heartbeats.
- If attention is needed, do not reply HEARTBEAT_OK. Briefly report the issue, useful action taken, or next concrete action.

Chat behavior:
- Pebble Shell is configured as a personal single-user agent in one linear chat. Transport routing is handled by the harness outside your model context.
- Be concise and natural. On first contact, briefly ask about the user's hobbies, interests, work style, and what they want remembered while still handling urgent concrete requests.
- During onboarding, write important durable facts the user shares, such as name, stable preferences, hobbies, work style, and explicit memory requests, into context/MEMORY.md with file tools.
- Image attachments may be provided as image_url parts in the user message. Inspect them directly when relevant and mention if no image was actually provided.
- Attachments may also be saved under sent_attachments and listed in the user message. Non-image files appear as [attached file: path] and must be inspected with normal tools when relevant; do not assume PDFs or other non-image files were read automatically. Images appear as [attached image file: path; already included as an image in this message, ...] and may also be provided to the vision model in the same message; do not re-inspect those image paths with read_image/read unless the user asks about the saved file later.
"""

SUMMARY_PROMPT = """You are Pebble Shell's no-tools conversation and execution compactor.

Your job is to update Pebble Shell's long-running summary from:
1. the previous summary, if any
2. the conversation messages
3. tool calls and tool results

Return only the updated summary. Do not answer the user. Do not perform any task. Do not request tools.

Write a detailed summary. Prefer a long, information-dense summary over an overly short one. The summary may need to preserve useful state from hundreds of thousands of tokens, so include enough detail for Pebble Shell to continue accurately after the compacted messages are removed. Aim for at least 500 tokens when there is enough meaningful information.

Preserve important specifics:
- what the user wants and why
- stable user preferences, working style, and explicit instructions
- current project state
- important decisions the user made
- tasks completed
- tasks still open
- blockers, failures, and unresolved questions
- files and directories the agent inspected, edited, created, deleted, tested, or planned to work on
- exact file locations and line/function/module context when relevant
- commands, ports, URLs, model names, provider names, IDs, job IDs, runtime settings, config keys, and environment variable names
- tool calls that changed state
- tool results that verify behavior, such as test results, logs, API responses, Docker status, browser checks, and errors
- any exact wording that future behavior depends on, especially prompts, names, greetings, and policy choices
- credentials and secrets by durable reference: include what service they are for, where they are stored, the relevant env/config key, and whether they were verified

For credentials and secrets:
- Preserve enough information for Pebble Shell to keep using credentials after compaction.
- Prefer references to durable storage locations, such as `.env`, a secret store key, or an environment variable name.
- If a raw credential appears only in the conversation and has not yet been stored durably, record that it must be stored before the compacted messages are discarded.

Compress aggressively but do not lose operational facts. If a newer instruction overrides an older one, keep the newer instruction and mention the override only if it matters. If a prior plan was abandoned, summarize that it was abandoned rather than preserving obsolete details as active tasks.

Use concise labeled sections when helpful, such as:
- User Preferences
- Current Project State
- Files and Locations
- Decisions
- Completed Work
- Open Tasks
- Tool/Test Evidence
- Credentials and Config
- Important Details

Omit empty sections. Do not invent facts. Do not include filler, repeated typos, casual chatter, or transient status updates unless they changed the project state."""

BACKGROUND_SYSTEM_PROMPT = """You are a write-capable background worker controlled by the Pebble Shell foreground supervisor.

Rules:
- You are not the user-facing foreground agent. Do not try to message the user directly; report final status in your final answer.
- Use send_msg often enough to keep foreground Pebble informed during long work. Send a brief update whenever you do something major: start a substantial phase, finish a meaningful change, learn an important fact, begin verification, finish verification, or discover a blocker. Summarize what changed or what you verified in one or two short sentences, ideally under 400 characters. This messages foreground Pebble, not the user directly.
- You have no heartbeat. Work until the assigned task is complete, blocked, paused, or canceled.
- Edit only your assigned folder and /tmp unless the foreground prompt explicitly grants another path.
- You have full shell control inside the Docker container, including container-local sudo/root capability. You may install packages, CLIs, browsers, dependencies, or other tools needed for your assigned work.
- All relative file, search, bash, and exec_command paths operate from your assigned folder. For these tools, a leading / means /workspace, not container root.
- For direct text or Markdown URLs such as `https://example.com/SKILL.md`, use curl through bash. Do not use Playwright for direct text files.
- For rendered browser behavior and UI verification, use bash with Playwright CLI or short Playwright scripts.
- Prefer job-id-specific names for artifacts and commands when useful.
- Use tools to inspect, edit, run, test, and verify. Do not claim completion without tool evidence.
- You are already running as a background worker. Do not use exec_command as a way to hand off the assigned job and then stop. Use tools directly for the job. Use exec_command for real long-running servers, watchers, or daemons, then keep the session_id, poll with write_stdin, test against it, and report its status.
- When you believe you are done, stop with a concise final answer. The harness will then ask you to self-check with exactly COMPLETE, BLOCKED, or NEEDS_MORE_WORK. Answer honestly from tool evidence. If unsure or not verified, answer NEEDS_MORE_WORK or BLOCKED rather than claiming completion.
- Keep your final answer concise: summarize what you changed, where it is, how you tested it, and any blockers."""

_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
Delivery = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AgentResponse:
    content: str
    steps: int


@dataclass(frozen=True, slots=True)
class ImageInput:
    url: str
    content_type: str = ""
    filename: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class QueuedUserMessage:
    content: str
    images: list[ImageInput]


@dataclass(slots=True)
class HeartbeatResponse:
    content: str
    should_notify: bool
    steps: int


class CodingAgent:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        bundled_root = Path(__file__).resolve().parent.parent
        ensure_workspace_context_files(settings.agent_workspace, bundled_root)
        self.runtime_config = RuntimeConfigStore(settings.runtime_config_db_path)
        self.self_improvement = SelfImprovementStore(settings.self_improvement_db_path)
        self.cron = CronStore(settings.cron_db_path)
        self.shell_audit = ShellAuditStore(settings.shell_audit_db_path)
        self.memory = MemoryStore(settings.memory_db_path)
        self._inbox_lock = asyncio.Lock()
        self._active_inbox: list[QueuedUserMessage] | None = None
        self.background_store = BackgroundTaskStore(settings.background_tasks_db_path)
        self.background_tasks = BackgroundTaskService(self, self.background_store, settings.max_background_tasks)
        self._deliver: Delivery | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.tools = WorkspaceTools(
            settings.agent_workspace,
            settings.shell_timeout_seconds,
            self.runtime_config,
            self.self_improvement,
            self.cron,
            self.shell_audit,
            self.memory,
            settings.exa_api_key,
            settings.exa_base_url,
            self.background_tasks,
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
            settings.openai_fallback_models,
            webhook_replayer=self.schedule_hook_event_replay,
            max_inspect_image_bytes=settings.max_discord_image_bytes,
            max_send_file_bytes=settings.max_discord_send_file_bytes,
        )
        self.context_files = ContextFileLoader(settings.agent_workspace, bundled_root)
        self._memory_md_snapshot = self._read_memory_md()
        self._flash_lock = asyncio.Lock()

    def set_deliver(self, deliver: Delivery | None) -> None:
        self._deliver = deliver

    async def enqueue_user_message(self, content: str, images: list[ImageInput] | None = None) -> bool:
        async with self._inbox_lock:
            if self._active_inbox is None:
                return False
            self._active_inbox.append(QueuedUserMessage(content=content, images=images or []))
            return True

    async def run_user_message(
        self,
        content: str,
        images: list[ImageInput] | None = None,
    ) -> AgentResponse:
        self.bind_background_loop()
        async with _process_run_lock():
            return await self._run_locked(content, "user", images or [])

    async def run_internal_event(
        self,
        content: str,
        source: str,
    ) -> AgentResponse:
        self.bind_background_loop()
        async with _process_run_lock():
            return await self._run_locked(content, source, [])

    def bind_background_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.background_tasks.bind_loop(self._loop)

    def schedule_hook_event_replay(self, event_id: int) -> str:
        loop = self._loop
        if not loop or loop.is_closed():
            return "Webhook replay could not be scheduled because the agent event loop is not bound yet"
        loop.call_soon_threadsafe(lambda: loop.create_task(self.replay_hook_event(event_id)))
        return f"Queued replay for webhook event {event_id}"

    async def replay_hook_event(self, event_id: int) -> AgentResponse:
        event = self.self_improvement.get_webhook_event(event_id)
        if not event:
            raise ValueError(f"Unknown webhook event: {event_id}")
        hook = self.self_improvement.get_hook(str(event["name"]))
        if not hook:
            raise ValueError(f"Unknown hook for webhook event {event_id}: {event['name']}")
        if not hook["enabled"]:
            raise ValueError(f"Webhook hook is disabled: {event['name']}")
        replay_event_id = self.self_improvement.record_webhook_event(str(event["name"]), dict(event["payload"]), background=True)
        self.self_improvement.mark_webhook_event_processing(replay_event_id)
        try:
            content = format_webhook_message(str(event["name"]), str(hook["prompt"]), dict(event["payload"]))
            response = await self.run_internal_event(content, f"webhook:{event['name']}:replay")
        except Exception as exc:
            self.self_improvement.mark_webhook_event_failed(replay_event_id, str(exc))
            raise
        self.self_improvement.mark_webhook_event_completed(replay_event_id, response.content)
        return response

    async def _run_locked(
        self,
        content: str,
        source: str,
        images: list[ImageInput],
    ) -> AgentResponse:
        await self._activate_inbox()
        user_memory_content = _memory_content_with_images(content, images)
        user_memory_contents = [user_memory_content]
        messages, image_message_indexes, has_images = self._build_initial_messages(content, source, images)
        memory_start_index = len(messages) - 1

        try:
            return await self._run_steps(
                source,
                messages,
                user_memory_contents,
                image_message_indexes,
                has_images,
                memory_start_index=memory_start_index,
            )
        finally:
            await self._deactivate_inbox()

    def _build_initial_messages(
        self,
        content: str,
        source: str,
        images: list[ImageInput],
    ) -> tuple[list[dict[str, object]], dict[int, str], bool]:
        memory_context = self.memory.get_context(
            _memory_content_with_images(content, images),
            self.settings.recent_message_limit,
            self.settings.recent_message_token_budget,
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        user_message: dict[str, object] = {
            "role": "user",
            "content": _user_message_content(content, images),
        }
        messages.extend(self.context_files.load())
        memory_md = self._memory_md_message()
        if memory_md:
            messages.append(memory_md)
        if memory_context.summary:
            messages.append({"role": "system", "content": _compaction_summary_content(None, memory_context.summary)})
        messages.extend(_recent_messages_as_native_roles(memory_context))
        messages.append(user_message)
        image_message_indexes: dict[int, str] = {}
        if images:
            image_message_indexes[len(messages) - 1] = _user_text_with_image_references(content, images)
        return messages, image_message_indexes, bool(images)

    def build_chat_completion_payload(
        self,
        content: str,
        source: str = "user",
        images: list[ImageInput] | None = None,
        include_background_tools: bool = True,
    ) -> dict[str, object]:
        messages, _, _ = self._build_initial_messages(content, source, images or [])
        return {
            "model": self.current_model,
            "messages": messages,
            "tools": self.tools.definitions(include_background_tools=include_background_tools),
            "tool_choice": "auto",
        }

    def dump_next_heartbeat_context(self) -> Path:
        prompt = self._heartbeat_prompt()
        payload = self.build_chat_completion_payload(prompt, "heartbeat")
        dumps_dir = self.settings.agent_workspace / "context_dumps"
        dumps_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = dumps_dir / f"heartbeat_{timestamp}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for message in payload["messages"]:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        return path

    async def run_background_task(self, job: BackgroundJob) -> AgentResponse:
        self.bind_background_loop()
        worker_tools = self._background_worker_tools(job)
        existing_context = self.background_store.get_context(job.id)
        if existing_context:
            return await self._run_steps(
                f"background:{job.id}",
                existing_context,
                [f"Background task {job.id}: {job.prompt}"],
                {},
                False,
                background_job_id=job.id,
                include_background_tools=False,
                tools=worker_tools,
            )
        memory_context = self.memory.get_context(
            job.prompt,
            self.settings.recent_message_limit,
            self.settings.recent_message_token_budget,
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": BACKGROUND_SYSTEM_PROMPT},
        ]
        memory_md = self._memory_md_message()
        if memory_md:
            messages.append(memory_md)
        messages.extend(_recent_messages_as_native_roles(memory_context))
        job_summary = self.background_store.get_summary(job.id)
        if job_summary:
            messages.append({"role": "system", "content": _compaction_summary_content(job.id, job_summary)})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Background job id: {job.id}\n"
                    f"Assigned folder: {job.folder}\n"
                    f"Absolute assigned folder: {self.settings.agent_workspace / job.folder}\n"
                    "All relative file, search, bash, and exec_command paths operate from the assigned folder. "
                    "For these tools, a leading / means /workspace, not container root.\n"
                    f"Task:\n{job.prompt}"
                ),
            },
        )
        self.background_store.save_context(job.id, messages)
        return await self._run_steps(
            f"background:{job.id}",
            messages,
            [f"Background task {job.id}: {job.prompt}"],
            {},
            False,
            background_job_id=job.id,
            include_background_tools=False,
            tools=worker_tools,
        )

    def _background_worker_tools(self, job: BackgroundJob) -> WorkspaceTools:
        return WorkspaceTools(
            self.settings.agent_workspace,
            self.settings.shell_timeout_seconds,
            runtime_config=self.runtime_config,
            self_improvement=self.self_improvement,
            cron=self.cron,
            shell_audit=self.shell_audit,
            memory=self.memory,
            exa_api_key=self.settings.exa_api_key,
            exa_base_url=self.settings.exa_base_url,
            background_tasks=None,
            openai_api_key=self.settings.openai_api_key,
            openai_base_url=self.settings.openai_base_url,
            openai_model=self.settings.openai_model,
            openai_fallback_models=self.settings.openai_fallback_models,
            max_inspect_image_bytes=self.settings.max_discord_image_bytes,
            file_sender=None,
            text_sender=self.background_tasks.progress_sender(job.id),
            max_send_file_bytes=self.settings.max_discord_send_file_bytes,
            cwd=self.settings.agent_workspace / job.folder,
        )

    async def _run_steps(
        self,
        source: str,
        messages: list[dict[str, object]],
        user_memory_contents: list[str],
        image_message_indexes: dict[int, str],
        has_images: bool,
        background_job_id: str | None = None,
        include_background_tools: bool = True,
        memory_start_index: int | None = None,
        tools: WorkspaceTools | None = None,
    ) -> AgentResponse:
        active_tools = tools or self.tools
        called_tool_names: list[str] = []
        called_tool_records: list[tuple[str, str]] = []
        for step in range(1, self.settings.max_agent_steps + 1):
            if background_job_id and self.background_store.should_cancel(background_job_id):
                final = "Background task canceled before the next model step."
                await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                return AgentResponse(content=final, steps=step - 1)
            if background_job_id and self.background_store.should_pause(background_job_id):
                final = "Background task paused before the next model step."
                await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                return AgentResponse(content=final, steps=step - 1)
            if background_job_id:
                for message in self.background_store.drain_messages(background_job_id):
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Foreground supervisor sent this new instruction for background job {background_job_id}:\n{message}",
                        }
                    )
                    self.background_store.add_event(background_job_id, "message_delivered", message[:4000])
                self.background_store.save_context(background_job_id, messages)
            queued_messages = [] if background_job_id else await self._drain_inbox()
            for queued in queued_messages:
                queued_memory_content = _memory_content_with_images(queued.content, queued.images)
                user_memory_contents.append(queued_memory_content)
                messages.append(
                    {
                        "role": "user",
                        "content": _user_message_content(queued.content, queued.images),
                    }
                )
                if queued.images:
                    has_images = True
                    image_message_indexes[len(messages) - 1] = _user_text_with_image_references(queued.content, queued.images)

            response = await self._chat_completion_with_context_retry(
                messages=messages,
                background_job_id=background_job_id,
                source=_log_source_kind(source),
                tools=active_tools.definitions(include_background_tools=include_background_tools),
                tool_choice="auto",
            )
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if background_job_id:
                self.background_store.save_context(background_job_id, messages)

            tool_calls = message.tool_calls or []
            if not tool_calls:
                if source == "heartbeat" and not _called_read_heartbeat(called_tool_records) and step < self.settings.max_agent_steps:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "This heartbeat turn must inspect context/HEARTBEAT.md through the read tool before finishing. "
                                "Call read now with path \"context/HEARTBEAT.md\", then continue the heartbeat decision from that tool result."
                            ),
                        }
                    )
                    continue
                if (
                    include_background_tools
                    and background_job_id is None
                    and "subagent_start" not in called_tool_names
                    and _requires_subagent_start(user_memory_contents)
                    and step < self.settings.max_agent_steps
                ):
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The user explicitly asked you to start a subagent/background worker. "
                                "You have not called `subagent_start` in this turn. "
                                "Call `subagent_start` now with a prompt and folder, then report only the real job id returned by the tool."
                            ),
                        }
                    )
                    continue
                queued_messages = [] if background_job_id else await self._drain_inbox()
                if queued_messages:
                    for queued in queued_messages:
                        queued_memory_content = _memory_content_with_images(queued.content, queued.images)
                        user_memory_contents.append(queued_memory_content)
                        messages.append(
                            {
                                "role": "user",
                                "content": _user_message_content(queued.content, queued.images),
                            }
                        )
                        if queued.images:
                            has_images = True
                            image_message_indexes[len(messages) - 1] = _user_text_with_image_references(queued.content, queued.images)
                    continue
                final = message.content or ""
                if not final.strip():
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Your previous assistant response was empty and had no tool calls. "
                                "No more tools are available for this recovery step. Produce a concise "
                                "user-visible reply now. If work is incomplete, say what is still pending."
                            ),
                        }
                    )
                    try:
                        recovery = await self._chat_completion_with_context_retry(
                            messages=messages,
                            background_job_id=background_job_id,
                            source=_log_source_kind(source),
                            tool_choice="none",
                        )
                        recovery_message = recovery.choices[0].message
                        messages.append(recovery_message.model_dump(exclude_none=True))
                        final = (recovery_message.content or "").strip()
                        LOGGER.warning(
                            "empty_final_recovered source=%s step=%s background_job=%s recovered=%s",
                            _log_source_kind(source),
                            step,
                            background_job_id or "",
                            bool(final),
                        )
                    except Exception as exc:  # noqa: BLE001 - fallback should still notify the user.
                        LOGGER.warning(
                            "empty_final_recovery_failed source=%s step=%s background_job=%s error=%s",
                            _log_source_kind(source),
                            step,
                            background_job_id or "",
                            exc,
                        )
                        final = ""
                if not final:
                    final = "I got an empty model response before I could produce a useful answer. Please resend your message or ask for `/last_run` once diagnostics are available."
                    messages.append({"role": "assistant", "content": final})
                await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                return AgentResponse(content=final, steps=step)

            for call in tool_calls:
                called_tool_names.append(call.function.name)
                called_tool_records.append((call.function.name, call.function.arguments))
                if background_job_id and self.background_store.should_cancel(background_job_id):
                    final = "Background task canceled before running the next tool."
                    await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                    return AgentResponse(content=final, steps=step)
                if background_job_id and self.background_store.should_pause(background_job_id):
                    final = "Background task paused before running the next tool."
                    await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                    return AgentResponse(content=final, steps=step)
                result = await asyncio.to_thread(active_tools.run, call.function.name, call.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.model_dump_json(),
                    }
                )
                if background_job_id:
                    self.background_store.add_event(
                        background_job_id,
                        "tool_call",
                        f"{call.function.name}: {'ok' if result.ok else 'failed'}",
                    )
                    self.background_store.save_context(background_job_id, messages)

        final = "I reached the configured maximum tool-iteration limit before producing a final answer."
        messages.append(
            {
                "role": "system",
                "content": (
                    "You reached the configured maximum tool-turn limit. No more tool calls are available for this turn. "
                    "Write a concise final reply to the user now. State what was completed, what remains incomplete or "
                    "unverified, any blockers, and the next concrete action. Do not claim success without evidence."
                ),
            }
        )
        try:
            response = await self._chat_completion_with_context_retry(
                messages=messages,
                background_job_id=background_job_id,
                source=_log_source_kind(source),
                tool_choice="none",
            )
            final = (response.choices[0].message.content or "").strip() or final
        except Exception as exc:  # noqa: BLE001 - still return a useful harness-level fallback.
            LOGGER.warning(
                "agent_max_steps_finalization_failed source=%s background_job=%s error=%s",
                _log_source_kind(source),
                background_job_id or "",
                exc,
            )
        await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
        return AgentResponse(content=final, steps=self.settings.max_agent_steps + 1)

    async def run_heartbeat(self) -> HeartbeatResponse:
        prompt = self._heartbeat_prompt()
        response = await self.run_internal_event(prompt, "heartbeat")
        content = response.content.strip()
        should_notify = not _is_heartbeat_ack(content, self.settings.heartbeat_ack_max_chars)
        if not should_notify:
            content = "HEARTBEAT_OK"
        self.memory.record_heartbeat(content, should_notify)
        return HeartbeatResponse(content=content, should_notify=should_notify, steps=response.steps)

    def _heartbeat_prompt(self) -> str:
        utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"This is a heartbeat turn. The time is {utc_now}. {self.settings.heartbeat_prompt}"

    def _read_memory_md(self) -> str:
        path = next(
            (
                candidate
                for candidate in context_file_candidates(
                    self.settings.agent_workspace,
                    Path(__file__).resolve().parent.parent,
                    "MEMORY.md",
                )
                if candidate.is_file()
            ),
            None,
        )
        if path is None:
            return ""
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > 6000:
            content = content[:6000] + "\n[truncated]"
        return content

    def _refresh_memory_md(self) -> None:
        self._memory_md_snapshot = self._read_memory_md()

    def _memory_md_message(self) -> dict[str, object] | None:
        if not self._memory_md_snapshot:
            return None
        return {"role": "system", "content": f"Cached context/MEMORY.md snapshot:\n{self._memory_md_snapshot}"}

    async def _remember_turn(
        self,
        user_contents: list[str],
        assistant_content: str,
        messages: list[dict[str, object]] | None = None,
        memory_start_index: int | None = None,
        background_job_id: str | None = None,
    ) -> None:
        if messages is not None and memory_start_index is not None and background_job_id is None:
            for message in messages[memory_start_index:]:
                if message.get("role") == "system" or _is_compaction_summary_message(message):
                    continue
                self._remember_message(message)
            return
        for user_content in user_contents:
            self.memory.add_message("user", user_content)
        self.memory.add_message("assistant", assistant_content)

    def _remember_message(self, message: dict[str, object]) -> None:
        role = str(message.get("role", ""))
        if role not in {"user", "assistant", "tool"}:
            return
        normalized = _normalize_message_for_memory(message)
        if role == "assistant" and not normalized.get("tool_calls") and not str(normalized.get("content") or "").strip():
            return
        content = _message_memory_content(normalized)
        self.memory.add_message(role, content, normalized)

    async def _activate_inbox(self) -> None:
        async with self._inbox_lock:
            self._active_inbox = []

    async def _drain_inbox(self) -> list[QueuedUserMessage]:
        async with self._inbox_lock:
            if not self._active_inbox:
                return []
            drained = list(self._active_inbox)
            self._active_inbox.clear()
            return drained

    async def _deactivate_inbox(self) -> None:
        async with self._inbox_lock:
            self._active_inbox = None

    async def _chat_completion_with_context_retry(
        self,
        messages: list[dict[str, object]],
        background_job_id: str | None,
        source: str = "unknown",
        **kwargs: Any,
    ) -> Any:
        for attempt in range(3):
            try:
                return await self._chat_completion(messages=messages, background_job_id=background_job_id, source=source, **kwargs)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - context compaction should inspect provider errors.
                if not _is_context_length_error(exc) or attempt == 2:
                    raise
                compacted = await self._compact_active_messages(messages, background_job_id)
                if not compacted:
                    raise
        raise RuntimeError("Context compaction retry loop ended unexpectedly")

    async def _compact_active_messages(
        self,
        messages: list[dict[str, object]],
        background_job_id: str | None,
    ) -> bool:
        compactible_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") != "system" and not _is_compaction_summary_message(message)
        ]
        if len(compactible_indexes) <= 2:
            return False

        pending_index = compactible_indexes[-1]
        history_indexes = compactible_indexes[:-1]
        if not history_indexes:
            return False

        summarize_indexes, recent_indexes = _split_history_indexes_by_token_weight(history_indexes, messages)
        if not summarize_indexes:
            return False

        prior_summary_index = next((i for i, message in enumerate(messages) if _is_compaction_summary_message(message)), None)
        prior_summary = ""
        if prior_summary_index is not None:
            prior_summary = str(messages[prior_summary_index].get("content", ""))

        summarized_messages = [messages[index] for index in summarize_indexes]
        before_tokens = _estimate_messages_tokens(summarized_messages)
        summary, summary_input_tokens, summary_output_tokens = await self._summarize_active_context(
            prior_summary,
            summarized_messages,
            background_job_id,
        )
        summary_tokens = summary_output_tokens or _estimate_tokens(summary)
        if background_job_id is None:
            self.context_files.refresh()
            self._refresh_memory_md()

        keep_indexes = set(index for index, message in enumerate(messages) if message.get("role") == "system")
        keep_indexes.update(recent_indexes)
        keep_indexes.add(pending_index)
        if prior_summary_index is not None:
            keep_indexes.discard(prior_summary_index)

        summary_message = {
            "role": "system",
            "content": _compaction_summary_content(background_job_id, summary),
        }
        rebuilt = [message for index, message in enumerate(messages) if index in keep_indexes]
        if background_job_id is None:
            rebuilt = [message for message in rebuilt if not _is_memory_md_message(message) and not _is_context_file_message(message)]
            insert_at = 1 if rebuilt and rebuilt[0].get("role") == "system" else 0
            refreshed_context_files = self.context_files.load()
            for context_message in refreshed_context_files:
                rebuilt.insert(min(insert_at, len(rebuilt)), context_message)
                insert_at += 1
            memory_md = self._memory_md_message()
            if memory_md:
                rebuilt.insert(min(insert_at, len(rebuilt)), memory_md)
                insert_at += 1
        else:
            insert_at = max((index for index, message in enumerate(rebuilt) if message.get("role") == "system"), default=-1) + 1
        rebuilt.insert(min(insert_at, len(rebuilt)), summary_message)
        messages[:] = rebuilt

        if background_job_id:
            self.background_store.set_summary(background_job_id, summary)
            self.background_store.save_context(background_job_id, messages)
        else:
            self.memory.upsert_summary(summary, self.memory.last_message_id())
        await self._notify_summary(background_job_id, before_tokens, summary_tokens, summary_input_tokens, summary_output_tokens)
        return True

    async def _summarize_active_context(
        self,
        prior_summary: str,
        messages: list[dict[str, object]],
        background_job_id: str | None,
    ) -> tuple[str, int | None, int | None]:
        response = await self._chat_completion(
            messages=self._build_summarizer_messages(prior_summary, messages, background_job_id),
            tool_choice="none",
            source="summary",
        )
        summary = response.choices[0].message.content or prior_summary or "(summary unavailable)"
        return summary, _usage_value(response, "prompt_tokens"), _usage_value(response, "completion_tokens")

    def _build_summarizer_messages(
        self,
        prior_summary: str,
        messages: list[dict[str, object]],
        background_job_id: str | None,
    ) -> list[dict[str, object]]:
        scope = f"background job {background_job_id}" if background_job_id else "foreground conversation"
        summarizer_messages: list[dict[str, object]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        summarizer_messages.extend(self.context_files.load())
        memory_md = self._memory_md_message()
        if memory_md:
            summarizer_messages.append(memory_md)
        if prior_summary:
            summarizer_messages.append({"role": "system", "content": prior_summary})
        summarizer_messages.extend(messages)
        summarizer_messages.append(
            {
                "role": "user",
                "content": (
                    f"Scope: {scope}\n\n"
                    f"{SUMMARY_PROMPT}\n\n"
                    "Summarize all conversation, tool calls, and tool results above that belong to this scope. "
                    "Return only the updated summary."
                ),
            }
        )
        return summarizer_messages

    async def _notify_summary(
        self,
        background_job_id: str | None,
        before_tokens: int,
        summary_tokens: int,
        provider_input_tokens: int | None = None,
        provider_output_tokens: int | None = None,
    ) -> None:
        if provider_input_tokens is not None and provider_output_tokens is not None:
            notice = f"[compacted {provider_input_tokens} tokens to {provider_output_tokens} tokens]"
        else:
            notice = "[compacted]"
        if background_job_id:
            self.background_store.add_event(
                background_job_id,
                "summary",
                f"{notice} summarized {before_tokens} estimated tokens to {summary_tokens} tokens",
            )
        if self._deliver:
            await self._deliver(notice)

    @property
    def current_model(self) -> str:
        return self.runtime_config.get("openai_model") or self.settings.openai_model

    def candidate_models(self, primary_model: str | None = None) -> list[str]:
        models = []
        if primary_model:
            models.append(primary_model)
        if self.current_model not in models:
            models.append(self.current_model)
        for model in self.settings.openai_fallback_models.split(","):
            model = model.strip()
            if model and model not in models:
                models.append(model)
        return models

    async def _chat_completion(self, **kwargs: Any) -> Any:
        model_override = kwargs.pop("model_override", None)
        background_job_id = kwargs.pop("background_job_id", None)
        source = kwargs.pop("source", "unknown")
        errors: list[str] = []
        for model in self.candidate_models(model_override):
            backoffs = [1, 2]
            for attempt in range(len(backoffs) + 1):
                try:
                    self._raise_if_model_input_cap_exceeded(model, kwargs)
                    response = await self.client.chat.completions.create(model=model, **kwargs)
                    prompt_tokens = _usage_value(response, "prompt_tokens")
                    completion_tokens = _usage_value(response, "completion_tokens")
                    total_tokens = _usage_value(response, "total_tokens")
                    cached_tokens = _usage_detail_value(response, "prompt_tokens_details", "cached_tokens")
                    reasoning_tokens = _usage_detail_value(response, "completion_tokens_details", "reasoning_tokens") or _usage_value(response, "reasoning_tokens")
                    image_tokens = _usage_detail_value(response, "prompt_tokens_details", "image_tokens")
                    self.memory.record_model_call(
                        source,
                        model,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cached_tokens,
                        reasoning_tokens,
                        image_tokens,
                    )
                    if background_job_id:
                        self.background_store.record_model_usage(
                            background_job_id,
                            model,
                            prompt_tokens,
                            completion_tokens,
                            total_tokens,
                        )
                    return response
                except Exception as exc:  # noqa: BLE001 - fallback should preserve provider error context.
                    if _is_context_length_error(exc):
                        raise
                    self.memory.record_model_call(source, model, error=str(exc))
                    errors.append(f"{model} attempt {attempt + 1}: {exc}")
                    if not _is_retryable_model_error(exc) or attempt == len(backoffs):
                        break
                    await asyncio.sleep(backoffs[attempt])
        raise RuntimeError("All configured OpenAI-compatible models failed: " + " | ".join(errors))

    def _raise_if_model_input_cap_exceeded(self, model: str, kwargs: dict[str, Any]) -> None:
        limit = _model_input_token_limit(model, self.settings.openai_model_input_token_limits)
        if limit is None:
            return
        estimated = _estimate_messages_tokens(kwargs.get("messages", []))
        if estimated > limit:
            raise RuntimeError(
                f"context_length_exceeded: estimated prompt tokens {estimated} exceed configured input cap {limit} for {model}"
            )

    async def _flash_completion(self, **kwargs: Any) -> Any:
        errors: list[str] = []
        backoffs = [1, 2, 4]
        async with self._flash_lock:
            for attempt in range(len(backoffs) + 1):
                for model in self.flash_candidate_models():
                    try:
                        self._raise_if_model_input_cap_exceeded(model, kwargs)
                        response = await self.client.chat.completions.create(model=model, **kwargs)
                        self.memory.record_model_call(
                            "flash",
                            model,
                            _usage_value(response, "prompt_tokens"),
                            _usage_value(response, "completion_tokens"),
                            _usage_value(response, "total_tokens"),
                            _usage_detail_value(response, "prompt_tokens_details", "cached_tokens"),
                            _usage_detail_value(response, "completion_tokens_details", "reasoning_tokens")
                            or _usage_value(response, "reasoning_tokens"),
                            _usage_detail_value(response, "prompt_tokens_details", "image_tokens"),
                        )
                        return response
                    except Exception as exc:  # noqa: BLE001 - fallback should preserve provider error context.
                        self.memory.record_model_call("flash", model, error=str(exc))
                        errors.append(f"attempt {attempt + 1} {model}: {exc}")
                if attempt < len(backoffs):
                    await asyncio.sleep(backoffs[attempt])
        raise RuntimeError("All configured flash models failed: " + " | ".join(errors))

    def flash_candidate_models(self) -> list[str]:
        models = [self.settings.openai_flash_model]
        for model in self.settings.openai_flash_fallback_models.split(","):
            model = model.strip()
            if model and model not in models:
                models.append(model)
        return models


def _is_context_length_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "context_length_exceeded",
        "context length",
        "context window",
        "maximum context",
        "max context",
        "prompt is too long",
        "too many tokens",
        "token limit",
        "exceeds the context",
    ]
    return any(marker in text for marker in markers)


def _usage_value(response: Any, name: str) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    return int(value) if value is not None else None


def _usage_detail_value(response: Any, details_name: str, name: str) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, details_name, None)
    if details is None and isinstance(usage, dict):
        details = usage.get(details_name)
    if details is None:
        return None
    value = getattr(details, name, None)
    if value is None and isinstance(details, dict):
        value = details.get(name)
    return int(value) if value is not None else None


def _model_input_token_limit(model: str, limits: str) -> int | None:
    for item in limits.split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        if key.strip() != model:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _is_retryable_model_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 429, 500, 502, 503, 504}
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in ["rate limit", "temporarily unavailable", "timeout", "connection"])


def _log_source_kind(source: str) -> str:
    if source == "heartbeat":
        return "heartbeat"
    if source.startswith("background:"):
        return "background"
    if source.startswith("cron:"):
        return "cron"
    if source.startswith("webhook:"):
        return "webhook"
    return "user"


def _is_compaction_summary_message(message: dict[str, object]) -> bool:
    content = str(message.get("content", ""))
    return content.startswith("Active foreground compacted summary:") or content.startswith("Background job compacted summary:")


def _is_memory_md_message(message: dict[str, object]) -> bool:
    content = str(message.get("content", ""))
    return content.startswith("Cached context/MEMORY.md snapshot:") or content.startswith("Cached MEMORY.md snapshot:")


def _is_context_file_message(message: dict[str, object]) -> bool:
    content = str(message.get("content", ""))
    return any(
        content.startswith(f"context/{name}:\n") or content.startswith(f"{name}:\n")
        for name in CONTEXT_FILES
    )


def _compaction_summary_content(background_job_id: str | None, summary: str) -> str:
    if background_job_id:
        return f"Background job compacted summary for {background_job_id}:\n{summary}"
    return f"Active foreground compacted summary:\n{summary}"


def _render_messages_for_summary(messages: list[dict[str, object]]) -> str:
    rendered = []
    for index, message in enumerate(messages, start=1):
        rendered.append(f"--- message {index} ---")
        rendered.append(_render_message_for_summary(message))
    return "\n".join(rendered)


def _render_message_for_summary(message: dict[str, object]) -> str:
    role = message.get("role", "unknown")
    content = message.get("content", "")
    parts = [f"role: {role}"]
    if "tool_call_id" in message:
        parts.append(f"tool_call_id: {message.get('tool_call_id')}")
    if "tool_calls" in message:
        parts.append(f"tool_calls: {message.get('tool_calls')}")
    parts.append(f"content: {content}")
    return "\n".join(parts)


def _estimate_messages_tokens(messages: list[dict[str, object]]) -> int:
    return sum(_estimate_tokens(_render_message_for_summary(message)) for message in messages)


def _split_history_indexes_by_token_weight(
    history_indexes: list[int],
    messages: list[dict[str, object]],
    summarize_ratio: float = 0.6,
    min_recent: int = 5,
) -> tuple[list[int], list[int]]:
    if len(history_indexes) <= 1:
        return [], history_indexes
    weights = [(index, _estimate_tokens(_render_message_for_summary(messages[index]))) for index in history_indexes]
    total = sum(weight for _, weight in weights)
    target = max(1, int(total * summarize_ratio))
    max_summarize_count = max(1, len(history_indexes) - min(min_recent, len(history_indexes) - 1))
    summarized: list[int] = []
    used = 0
    for index, weight in weights[:max_summarize_count]:
        summarized.append(index)
        used += weight
        if used >= target:
            break
    recent = history_indexes[len(summarized) :]
    return summarized, recent


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _is_heartbeat_ack(content: str, ack_max_chars: int) -> bool:
    stripped = content.strip()
    if not stripped:
        return True
    if stripped == "HEARTBEAT_OK":
        return True
    if stripped.startswith("HEARTBEAT_OK"):
        return len(stripped.removeprefix("HEARTBEAT_OK").strip()) <= ack_max_chars
    if stripped.endswith("HEARTBEAT_OK"):
        return len(stripped.removesuffix("HEARTBEAT_OK").strip()) <= ack_max_chars
    return False


def _requires_subagent_start(user_memory_contents: list[str]) -> bool:
    text = "\n".join(user_memory_contents).lower()
    markers = (
        "subagent_start",
        "start a subagent",
        "start subagent",
        "start a background worker",
        "start a background task",
        "start background worker",
        "start background task",
    )
    return any(marker in text for marker in markers)


def _called_read_heartbeat(called_tool_records: list[tuple[str, str]]) -> bool:
    for name, raw_arguments in called_tool_records:
        if name != "read":
            continue
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            continue
        path = str(arguments.get("path", "")).strip().strip("./")
        if path in {"HEARTBEAT.md", "context/HEARTBEAT.md"}:
            return True
    return False


def _user_message_content(content: str, images: list[ImageInput]) -> str | list[dict[str, Any]]:
    text = content or "(no text)"
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": image.url}})
    return parts


def _recent_messages_as_native_roles(context: MemoryContext) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    source = context.recent_raw_messages or [{"role": role, "content": content} for role, content in context.recent_messages]
    for message in source:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        messages.append(dict(message))
    return messages


def _user_text_with_image_references(content: str, images: list[ImageInput]) -> str:
    return _memory_content_with_images(content, images)


def _memory_content_with_images(content: str, images: list[ImageInput]) -> str:
    if not images:
        return content
    lines = [content.strip() or "(no text)"]
    lines.append("Attached images:")
    for image in images:
        label_parts = [part for part in [image.filename, image.content_type, image.source_url or image.url] if part]
        lines.append("- " + " | ".join(label_parts))
    return "\n".join(lines)


def _normalize_message_for_memory(message: dict[str, object]) -> dict[str, object]:
    normalized = dict(message)
    role = normalized.get("role")
    if role == "assistant" and "content" not in normalized:
        normalized["content"] = None
    return normalized


def _message_memory_content(message: dict[str, object]) -> str:
    role = message.get("role")
    content = message.get("content")
    if role == "assistant" and message.get("tool_calls"):
        return _render_message_for_summary(message)
    if role == "tool":
        return _render_message_for_summary(message)
    if isinstance(content, str):
        return content
    if role == "user" and isinstance(content, list):
        return _text_from_multimodal_memory_content(content)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _text_from_multimodal_memory_content(content: list[object]) -> str:
    texts = []
    image_lines = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
            url = part["image_url"].get("url")
            if url:
                image_lines.append(f"- {url}")
    text = "\n".join(texts).strip() or "(no text)"
    if image_lines:
        return text + "\nAttached images:\n" + "\n".join(image_lines)
    return text


def _process_run_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _RUN_LOCKS[loop] = lock
        return lock
