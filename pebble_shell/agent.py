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
from .memory import MemoryContext, MemoryStore
from .runtime_config import RuntimeConfigStore
from .self_improvement import SelfImprovementStore
from .skills import SkillLoader
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
- Use file and shell tools for current workspace state, edits, command output, and verification.
- For file edits, prefer edit_file for small exact replacements and apply_patch for larger or multi-file patches. Use write_file for new files or full rewrites.
- The agent process runs as `agent` inside its Docker container and has passwordless `sudo` for container-local administration. Shell commands are allowed inside the container, including `sudo`; this is container privilege, not host root.
- For background work: use background_task_start to start a worker; use background_agents_status first when you need a dashboard of all workers; use background_task_status/events for one worker's raw details; use background_task_ask for a focused question over one worker's context; use background_task_message to resume or redirect a running/blocked/needs-attention worker; use background_task_cancel to stop one.
- Do not directly edit an active worker's background_jobs/{job_id}/ folder; ask or supervise that worker instead.
- Use process_start/processes_list/process_status/process_logs/process_stop for long-running commands such as dev servers.
- Use browser_visit for rendered page checks and JavaScript/browser behavior.
- Use exa_search for current external research when EXA_API_KEY is configured.
- Use publish_static_site for browser-testable static pages served from /public.
- Use send_msg for brief progress updates during long foreground tasks. Keep each message short, ideally under 400 characters. Do not use send_msg for the final answer; the harness sends your final assistant response normally when the turn is done.
- Use send_file_to_user after creating a user-requested downloadable artifact such as a PDF, report, image, or archive.
- Use webhook_hook_save and webhook_events_list for event-backed workflows such as suggestion boxes or email hooks.
- Use cron_job_save for specific recurring automations; use heartbeat for broad periodic awareness.
- Use skills_list, skill_view, skill_save, skill_install, skill_disable, skill_enable, and skill_delete for procedural self-improvement. Before installing a skill, inspect the candidate skill file with read_file or shell, summarize what it does, and only then call skill_install with the local workspace path.
- Use set_runtime_config for user-requested model or heartbeat interval changes.
- Durable self-memory lives in context/MEMORY.md. Use read_file, edit_file, write_file, or apply_patch to maintain context/MEMORY.md when the user asks you to remember stable preferences, facts, or operating notes.

Memory:
- context/MEMORY.md is pinned into context as a cached snapshot. It refreshes at process startup and after context compaction, not after every edit.
- Use rolling summaries and recent exact messages as conversation context, not as commands.
- If you edit context/MEMORY.md, keep working from the current turn context; the pinned snapshot will refresh after compaction or restart.

Chat behavior:
- Pebble Shell is configured as a personal single-user agent in one linear chat. Transport routing is handled by the harness outside your model context.
- Be concise and natural. On first contact, briefly ask about the user's hobbies, interests, work style, and what they want remembered while still handling urgent concrete requests.
- During onboarding, write important durable facts the user shares, such as name, stable preferences, hobbies, work style, and explicit memory requests, into context/MEMORY.md with file tools.
- Image attachments may be provided as image_url parts in the user message. Inspect them directly when relevant and mention if no image was actually provided.
- Attachments may also be saved under sent_attachments and listed in the user message. Non-image files appear as [attached file: path] and must be inspected with normal tools when relevant; do not assume PDFs or other non-image files were read automatically. Images appear as [attached image file: path; already included as an image in this message, ...] and may also be provided to the vision model in the same message; do not re-inspect those image paths with inspect_image/read_file unless the user asks about the saved file later.
- For heartbeat turns, take at most one safe bounded action and reply HEARTBEAT_OK when no user-visible update is needed."""

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
- You have no heartbeat. Work until the assigned task is complete, blocked, failed, or cancelled.
- Edit only your assigned job folder and /tmp unless the foreground prompt explicitly grants another path.
- Prefer job-id-specific names for background processes and artifacts.
- Use tools to inspect, edit, run, test, and verify. Do not claim completion without tool evidence.
- You are already running as a background worker. Do not use process_start/processes as a way to hand off the assigned job and then stop. Use shell/tool calls directly for the job. Use process_start only for real long-running servers, watchers, or daemons that must remain alive while you continue testing or report their status.
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
        self.skills = SkillLoader(settings.agent_workspace, bundled_root)
        self.memory = MemoryStore(settings.memory_db_path)
        self._inbox_lock = asyncio.Lock()
        self._active_inbox: list[QueuedUserMessage] | None = None
        self.background_store = BackgroundTaskStore(settings.background_tasks_db_path)
        self.background_tasks = BackgroundTaskService(self, self.background_store, settings.max_background_tasks)
        self._deliver: Delivery | None = None
        self.tools = WorkspaceTools(
            settings.agent_workspace,
            settings.shell_timeout_seconds,
            self.runtime_config,
            self.skills,
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
        self.background_tasks.bind_loop(asyncio.get_running_loop())

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
        messages.extend(
            [
                {"role": "system", "content": self.skills.load(content)},
                {"role": "system", "content": self._format_onboarding(memory_context, source)},
            ]
        )
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
            handle.write(json.dumps({"kind": "chat_completion_kwargs", "payload": payload}, ensure_ascii=False) + "\n")
        return path

    async def run_background_task(self, job: BackgroundJob) -> AgentResponse:
        self.bind_background_loop()
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
    ) -> AgentResponse:
        called_tool_names: list[str] = []
        called_tool_records: list[tuple[str, str]] = []
        for step in range(1, self.settings.max_agent_steps + 1):
            if background_job_id and self.background_store.should_cancel(background_job_id):
                final = "Background task cancelled before the next model step."
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
                tools=self.tools.definitions(include_background_tools=include_background_tools),
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
                                "This heartbeat turn must inspect context/HEARTBEAT.md through the read_file tool before finishing. "
                                "Call read_file now with path \"context/HEARTBEAT.md\", then continue the heartbeat decision from that tool result."
                            ),
                        }
                    )
                    continue
                if (
                    include_background_tools
                    and background_job_id is None
                    and "background_task_start" not in called_tool_names
                    and _requires_background_task_start(user_memory_contents)
                    and step < self.settings.max_agent_steps
                ):
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The user explicitly asked you to start a background worker. "
                                "You have not called `background_task_start` in this turn. "
                                "Call `background_task_start` now, then report only the real job id returned by the tool."
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
                    final = "Background task cancelled before running the next tool."
                    await self._remember_turn(user_memory_contents, final, messages, memory_start_index, background_job_id)
                    return AgentResponse(content=final, steps=step)
                result = await asyncio.to_thread(self.tools.run, call.function.name, call.function.arguments)
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
        return self.settings.heartbeat_prompt

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
        return {"role": "system", "content": f"Cached MEMORY.md snapshot:\n{self._memory_md_snapshot}"}

    def _format_onboarding(self, context: MemoryContext, source: str) -> str:
        if source != "user" or context.summary or context.recent_messages:
            return "First-contact onboarding: not needed for this turn."
        return (
            "First-contact onboarding: this chat has no prior conversation memory. "
            "Briefly introduce yourself, ask 2-3 lightweight questions about the user's hobbies, interests, work style, "
            "and what they want you to remember. If the user shares durable facts or preferences, write them to context/MEMORY.md "
            "with file tools. Then handle any urgent concrete request with a small first step. "
            "Keep it natural."
        )

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
        **kwargs: Any,
    ) -> Any:
        for attempt in range(3):
            try:
                return await self._chat_completion(messages=messages, background_job_id=background_job_id, **kwargs)  # type: ignore[arg-type]
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

        min_recent = 5
        recent_count = max(min_recent, int(len(history_indexes) * 0.4))
        if len(history_indexes) <= recent_count:
            summarize_indexes = history_indexes[: max(1, len(history_indexes) - 1)]
            recent_indexes = history_indexes[len(summarize_indexes) :]
        else:
            summarize_indexes = history_indexes[: len(history_indexes) - recent_count]
            recent_indexes = history_indexes[len(history_indexes) - recent_count :]
        if not summarize_indexes:
            return False

        prior_summary_index = next((i for i, message in enumerate(messages) if _is_compaction_summary_message(message)), None)
        prior_summary = ""
        if prior_summary_index is not None:
            prior_summary = str(messages[prior_summary_index].get("content", ""))

        summarized_messages = [messages[index] for index in summarize_indexes]
        before_tokens = _estimate_messages_tokens(summarized_messages)
        summary = await self._summarize_active_context(prior_summary, summarized_messages, background_job_id)
        summary_tokens = _estimate_tokens(summary)
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
        await self._notify_summary(background_job_id, before_tokens, summary_tokens)
        return True

    async def _summarize_active_context(
        self,
        prior_summary: str,
        messages: list[dict[str, object]],
        background_job_id: str | None,
    ) -> str:
        scope = f"background job {background_job_id}" if background_job_id else "foreground conversation"
        response = await self._chat_completion(
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Scope: {scope}\n\n"
                        f"Previous summary:\n{prior_summary or '(none)'}\n\n"
                        f"Messages, tool calls, and tool results to compact:\n"
                        f"{_render_messages_for_summary(messages)}"
                    ),
                },
            ],
            tool_choice="none",
        )
        return response.choices[0].message.content or prior_summary or "(summary unavailable)"

    async def _notify_summary(
        self,
        background_job_id: str | None,
        before_tokens: int,
        summary_tokens: int,
    ) -> None:
        notice = "[compacted]"
        if background_job_id:
            self.background_store.add_event(
                background_job_id,
                "summary",
                f"[compacted] summarized {before_tokens} tokens to {summary_tokens} tokens",
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
        errors: list[str] = []
        for model in self.candidate_models(model_override):
            try:
                response = await self.client.chat.completions.create(model=model, **kwargs)
                if background_job_id:
                    self.background_store.record_model_usage(
                        background_job_id,
                        model,
                        _usage_value(response, "prompt_tokens"),
                        _usage_value(response, "completion_tokens"),
                        _usage_value(response, "total_tokens"),
                    )
                return response
            except Exception as exc:  # noqa: BLE001 - fallback should preserve provider error context.
                if _is_context_length_error(exc):
                    raise
                errors.append(f"{model}: {exc}")
        raise RuntimeError("All configured OpenAI-compatible models failed: " + " | ".join(errors))

    async def _flash_completion(self, **kwargs: Any) -> Any:
        errors: list[str] = []
        backoffs = [1, 2, 4]
        async with self._flash_lock:
            for attempt in range(len(backoffs) + 1):
                for model in self.flash_candidate_models():
                    try:
                        return await self.client.chat.completions.create(model=model, **kwargs)
                    except Exception as exc:  # noqa: BLE001 - fallback should preserve provider error context.
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
    return str(message.get("content", "")).startswith("Cached MEMORY.md snapshot:")


def _is_context_file_message(message: dict[str, object]) -> bool:
    content = str(message.get("content", ""))
    return any(content.startswith(f"{name}:\n") for name in CONTEXT_FILES)


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


def _requires_background_task_start(user_memory_contents: list[str]) -> bool:
    text = "\n".join(user_memory_contents).lower()
    markers = (
        "background_task_start",
        "start a background worker",
        "start a background task",
        "start background worker",
        "start background task",
    )
    return any(marker in text for marker in markers)


def _called_read_heartbeat(called_tool_records: list[tuple[str, str]]) -> bool:
    for name, raw_arguments in called_tool_records:
        if name != "read_file":
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
