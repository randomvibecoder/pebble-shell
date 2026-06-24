from __future__ import annotations

import base64
import fnmatch
import json
import mimetypes
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import BaseModel

from .cron import CronStore, dumps_cron_state
from .shell_audit import ShellAuditStore
from .exa_search import ExaSearchClient
from .memory import MemoryStore
from .process_manager import BackgroundProcessManager
from .runtime_config import RuntimeConfigStore
from .event_hooks import EventHookStore

MAX_READ_FILE_BYTES = 40_000
MAX_READ_FILE_CHARS = 40_000
BASH_OUTPUT_LIMIT_CHARS = 50_000
MAX_INSPECT_IMAGE_BYTES = 4_000_000
MAX_SEND_FILE_BYTES = 25_000_000
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FileSender = Callable[[Path], str]
TextSender = Callable[[str], str]
WebhookReplayer = Callable[[int], str]


class ToolResult(BaseModel):
    ok: bool
    output: str


class WorkspaceTools:
    def __init__(
        self,
        root: Path,
        shell_timeout_seconds: int,
        runtime_config: RuntimeConfigStore | None = None,
        event_hooks: EventHookStore | None = None,
        cron: CronStore | None = None,
        shell_audit: ShellAuditStore | None = None,
        memory: MemoryStore | None = None,
        exa_api_key: str = "",
        exa_base_url: str = "https://api.exa.ai",
        background_tasks: Any | None = None,
        openai_api_key: str = "",
        openai_base_url: str = "https://nano-gpt.com/api/v1",
        openai_model: str = "claude-haiku-4-5-20251001",
        openai_fallback_models: str = "openai/gpt-5.4",
        vision_client: Any | None = None,
        max_inspect_image_bytes: int = MAX_INSPECT_IMAGE_BYTES,
        file_sender: FileSender | None = None,
        text_sender: TextSender | None = None,
        webhook_replayer: WebhookReplayer | None = None,
        max_send_file_bytes: int = MAX_SEND_FILE_BYTES,
        cwd: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cwd = (cwd or self.root).resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.shell_timeout_seconds = shell_timeout_seconds
        self.runtime_config = runtime_config
        self.event_hooks = event_hooks
        self.cron = cron
        self.shell_audit_store = shell_audit
        self.memory = memory
        self.processes = BackgroundProcessManager(self.root, self.cwd)
        self.exa = ExaSearchClient(exa_api_key, exa_base_url)
        self.background_tasks = background_tasks
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url
        self.openai_model = openai_model
        self.openai_fallback_models = openai_fallback_models
        self.vision_client = vision_client
        self.max_inspect_image_bytes = max(1, max_inspect_image_bytes)
        self.file_sender = file_sender
        self.text_sender = text_sender
        self.webhook_replayer = webhook_replayer
        self.max_send_file_bytes = max(1, max_send_file_bytes)

    def definitions(self, include_background_tools: bool = True) -> list[dict[str, Any]]:
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": "ls",
                    "description": "List files under a path. Relative paths resolve from the current tool cwd; leading / starts at /workspace; .. is allowed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory or file path."},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "glob",
                    "description": "Find files by glob pattern. Relative paths resolve from the current tool cwd; leading / starts at /workspace; .. is allowed.",
                    "parameters": {
                        "type": "object",
                        "required": ["pattern"],
                        "properties": {
                            "pattern": {"type": "string", "description": "Glob pattern such as **/*.py or *.md."},
                            "path": {"type": "string", "description": "Directory to search from.", "default": "."},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grep",
                    "description": "Search text files with a regex pattern. Relative paths resolve from the current tool cwd; leading / starts at /workspace; .. is allowed.",
                    "parameters": {
                        "type": "object",
                        "required": ["pattern"],
                        "properties": {
                            "pattern": {"type": "string"},
                            "path": {"type": "string", "default": "."},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_image",
                    "description": "Inspect a local workspace image with the configured model. Use this for PNG/JPEG/WebP/GIF files instead of read.",
                    "parameters": {
                        "type": "object",
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string", "description": "Image path."},
                            "question": {
                                "type": "string",
                                "description": "Optional focused question about the image.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "websearch",
                    "description": "Search the web with the Exa API for current or external information. Requires EXA_API_KEY.",
                    "parameters": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string"},
                            "num_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a UTF-8 text file. Relative paths resolve from the current tool cwd; leading / starts at /workspace; .. is allowed.",
                    "parameters": {
                        "type": "object",
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string", "description": "File path."}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "description": "Write UTF-8 text to a file. Relative paths resolve from the current tool cwd; leading / starts at /workspace; .. is allowed.",
                    "parameters": {
                        "type": "object",
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "description": (
                        "Edit a UTF-8 workspace text file by replacing an exact old string with a new string. "
                        "Good for small targeted edits."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["path", "old", "new"],
                        "properties": {
                            "path": {"type": "string"},
                            "old": {"type": "string", "description": "Exact text to replace. Must occur in the file."},
                            "new": {"type": "string", "description": "Replacement text."},
                            "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "patch",
                    "description": (
                        "Apply a patch to workspace files. Supports Codex-style patches with "
                        "*** Begin Patch, *** Add File, *** Update File, *** Delete File, and *** End Patch. "
                        "Use for larger or multi-file edits."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["patch"],
                        "properties": {
                            "patch": {"type": "string", "description": "Patch text to apply."}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run a bash command inside the container. In a background worker, commands run from the assigned folder.",
                    "parameters": {
                        "type": "object",
                        "required": ["command"],
                        "properties": {
                            "command": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "description": "Runs a command in a terminal session. If it is still running after yield_time_ms, returns a session_id for ongoing interaction.",
                    "parameters": {
                        "type": "object",
                        "required": ["cmd"],
                        "properties": {
                            "cmd": {"type": "string"},
                            "login": {"type": "boolean"},
                            "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 30000},
                            "max_output_tokens": {"type": "integer"},
                            "shell": {"type": "string"},
                            "tty": {"type": "boolean"},
                            "workdir": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_stdin",
                    "description": "Write to or poll a running exec_command session by session_id. Use empty chars to poll recent output without writing.",
                    "parameters": {
                        "type": "object",
                        "required": ["session_id"],
                        "properties": {
                            "session_id": {"type": "integer"},
                            "chars": {"type": "string"},
                            "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 30000},
                            "max_output_tokens": {"type": "integer"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_runtime_config",
                    "description": "Read persisted runtime agent settings such as current model and heartbeat interval.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_runtime_config",
                    "description": "Persist a safe runtime config change. Supported keys: openai_model, heartbeat_every_seconds.",
                    "parameters": {
                        "type": "object",
                        "required": ["key", "value"],
                        "properties": {
                            "key": {
                                "type": "string",
                                "enum": ["openai_model", "heartbeat_every_seconds"],
                            },
                            "value": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_set",
                    "description": (
                        "Create or update a named internal localhost event hook. POST /webhooks/{name} records an event "
                        "and returns an event id/status immediately; it does not return the agent result. "
                        "When API auth is enabled, backend code should read /workspace/.pebble_shell/secrets/api_auth_token at runtime."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["name", "prompt"],
                        "properties": {
                            "name": {"type": "string"},
                            "prompt": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_list",
                    "description": "List registered HTTP webhook hooks and whether each hook is enabled.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_show",
                    "description": "Show one registered HTTP webhook hook, including its prompt and enabled state.",
                    "parameters": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_enable",
                    "description": "Enable a registered HTTP webhook hook so POST /webhooks/{name} can trigger it.",
                    "parameters": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_disable",
                    "description": "Disable a registered HTTP webhook hook without deleting its prompt or event history.",
                    "parameters": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_remove",
                    "description": "Remove a registered HTTP webhook hook. Existing event history remains inspectable.",
                    "parameters": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_events",
                    "description": "List recent HTTP webhook payload receipts and processing status for hooks such as suggestion boxes.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hook_event_replay",
                    "description": "Replay a prior webhook event by event id. This schedules a new foreground agent run with the original hook payload.",
                    "parameters": {
                        "type": "object",
                        "required": ["event_id"],
                        "properties": {
                            "event_id": {"type": "integer"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cron_job_save",
                    "description": "Create or update a persisted scheduled agent job. The scheduler runs the prompt every N seconds in this chat.",
                    "parameters": {
                        "type": "object",
                        "required": ["name", "prompt", "every_seconds"],
                        "properties": {
                            "name": {"type": "string"},
                            "prompt": {"type": "string"},
                            "every_seconds": {"type": "integer", "minimum": 60},
                            "enabled": {"type": "boolean"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cron_list",
                    "description": "List scheduled jobs and recent run results.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "jobs_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                            "runs_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cron_enable",
                    "description": "Pause or resume a scheduled job.",
                    "parameters": {
                        "type": "object",
                        "required": ["name", "enabled"],
                        "properties": {
                            "name": {"type": "string"},
                            "enabled": {"type": "boolean"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell_audit",
                    "description": "List recent shell command audit records.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    },
                },
            },
        ]
        if include_background_tools:
            definitions.append(_send_file_tool_definition())
        if include_background_tools or self.text_sender:
            definitions.append(_send_msg_tool_definition())
        if include_background_tools and self.background_tasks:
            definitions.extend(_background_tool_definitions())
        return definitions

    def run(self, name: str, raw_arguments: str | dict[str, Any]) -> ToolResult:
        try:
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, output=f"Invalid JSON arguments: {exc}")

        try:
            if name == "ls":
                return self.ls(arguments.get("path", "."), int(arguments.get("limit", 200)))
            if name == "glob":
                return self.glob(arguments["pattern"], arguments.get("path", "."), int(arguments.get("max_results", 100)))
            if name == "grep":
                return self.grep(arguments["pattern"], arguments.get("path", "."), int(arguments.get("max_results", 100)))
            if name == "read":
                return self.read(arguments["path"])
            if name == "write":
                return self.write(arguments["path"], arguments["content"])
            if name == "edit":
                return self.edit(arguments["path"], arguments["old"], arguments["new"], bool(arguments.get("replace_all", False)))
            if name == "patch":
                return self.patch(arguments["patch"])
            if name == "send_file":
                return self.send_file(arguments["path"])
            if name == "send_msg":
                return self.send_msg(arguments["msg"])
            if name == "bash":
                return self.bash(arguments["command"])
            if name == "exec_command":
                return self.exec_command(
                    arguments["cmd"],
                    int(arguments.get("yield_time_ms", 10000)),
                    int(arguments.get("max_output_tokens", 20000)),
                    bool(arguments.get("tty", False)),
                    arguments.get("workdir"),
                    arguments.get("shell"),
                    bool(arguments.get("login", True)),
                )
            if name == "write_stdin":
                return self.write_stdin(
                    int(arguments["session_id"]),
                    str(arguments.get("chars", "")),
                    int(arguments.get("yield_time_ms", 10000)),
                    int(arguments.get("max_output_tokens", 20000)),
                )
            if name == "read_image":
                return self.read_image(arguments["path"], arguments.get("question", "Describe this image."))
            if name == "websearch":
                return self.websearch(arguments["query"], int(arguments.get("num_results", 5)))
            if name == "get_runtime_config":
                return self.get_runtime_config()
            if name == "set_runtime_config":
                return self.set_runtime_config(arguments["key"], arguments["value"])
            if name == "hook_set":
                return self.hook_set(arguments["name"], arguments["prompt"])
            if name == "hook_list":
                return self.hook_list(int(arguments.get("limit", 20)))
            if name == "hook_show":
                return self.hook_show(arguments["name"])
            if name == "hook_enable":
                return self.hook_set_enabled(arguments["name"], True)
            if name == "hook_disable":
                return self.hook_set_enabled(arguments["name"], False)
            if name == "hook_remove":
                return self.hook_remove(arguments["name"])
            if name == "hook_events":
                return self.hook_events(int(arguments.get("limit", 20)))
            if name == "hook_event_replay":
                return self.hook_event_replay(int(arguments["event_id"]))
            if name == "cron_job_save":
                return self.cron_job_save(
                    arguments["name"],
                    arguments["prompt"],
                    int(arguments["every_seconds"]),
                    arguments.get("enabled", True),
                )
            if name == "cron_list":
                return self.cron_list(int(arguments.get("jobs_limit", 20)), int(arguments.get("runs_limit", 20)))
            if name == "cron_enable":
                return self.cron_enable(arguments["name"], bool(arguments["enabled"]))
            if name == "shell_audit":
                return self.shell_audit(int(arguments.get("limit", 20)))
            if name == "subagent_start":
                return self.subagent_start(arguments["prompt"], arguments["folder"])
            if name == "subagent_status":
                return self.subagent_status(arguments["job_id"])
            if name == "subagent_list":
                return self.subagent_list(int(arguments.get("limit", 10)), arguments.get("status"))
            if name == "subagent_dashboard":
                return self.subagent_dashboard(int(arguments.get("limit", 10)), arguments.get("status"))
            if name == "subagent_summary":
                return self.subagent_summary(arguments["job_id"])
            if name == "subagent_ask":
                return self.subagent_ask(arguments["job_id"], arguments["question"])
            if name == "subagent_cancel":
                return self.subagent_cancel(arguments["job_id"])
            if name == "subagent_pause":
                return self.subagent_pause(arguments["job_id"])
            if name == "subagent_send":
                return self.subagent_send(arguments["job_id"], arguments["message"])
            if name == "subagent_delete":
                return self.subagent_delete(arguments["job_id"])
            if name == "subagent_events":
                return self.subagent_events(arguments["job_id"], int(arguments.get("limit", 20)))
        except KeyError as exc:
            return ToolResult(ok=False, output=f"Missing required argument: {exc}")
        except Exception as exc:  # noqa: BLE001 - tool errors should return to the model.
            return ToolResult(ok=False, output=str(exc))

        return ToolResult(ok=False, output=f"Unknown tool: {name}")

    def ls(self, path: str = ".", limit: int = 200) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        limit = max(1, min(int(limit), 1000))
        if not target.exists():
            return ToolResult(ok=False, output=f"No such path: {path}")
        if target.is_file():
            return ToolResult(ok=True, output=_display_path(target, self.root))

        entries = []
        truncated = False
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if len(entries) >= limit:
                truncated = True
                break
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{_display_path(child, self.root)}{suffix}")
        if truncated:
            return ToolResult(ok=True, output="\n".join(entries) + f"\n[ls truncated at {limit} entries]")
        return ToolResult(ok=True, output="\n".join(entries) or "(empty)")

    def read(self, path: str) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {path}")
        data = target.read_bytes()
        if _looks_binary(data, target.suffix):
            return ToolResult(
                ok=False,
                output=(
                    f"Refusing to read likely binary file {_display_path(target, self.root)} into model context. "
                    "Use a purpose-built extractor/converter or shell command that returns a small text excerpt."
                ),
            )
        truncated = len(data) > MAX_READ_FILE_BYTES
        if truncated:
            data = data[:MAX_READ_FILE_BYTES]
        content = data.decode("utf-8", errors="replace")
        if len(content) > MAX_READ_FILE_CHARS:
            content = content[:MAX_READ_FILE_CHARS]
            truncated = True
        if truncated:
            content += (
                f"\n[read truncated at {min(MAX_READ_FILE_BYTES, MAX_READ_FILE_CHARS)} bytes/chars. "
                "Use targeted shell commands such as sed, rg, head, tail, wc, or file-specific extractors "
                "to inspect the remaining content.]"
            )
        return ToolResult(ok=True, output=content)

    def write(self, path: str, content: str) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output=f"Wrote {len(content.encode('utf-8'))} bytes to {_display_path(target, self.root)}")

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
        if old == "":
            return ToolResult(ok=False, output="old text cannot be empty")
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {path}")
        data = target.read_bytes()
        if _looks_binary(data, target.suffix):
            return ToolResult(ok=False, output=f"Refusing to edit likely binary file {_display_path(target, self.root)}")
        content = data.decode("utf-8", errors="replace")
        count = content.count(old)
        if count == 0:
            return ToolResult(ok=False, output=f"old text not found in {_display_path(target, self.root)}")
        if count > 1 and not replace_all:
            return ToolResult(ok=False, output=f"old text occurs {count} times; set replace_all=true or provide a more specific old string")
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        target.write_text(updated, encoding="utf-8")
        replacements = count if replace_all else 1
        return ToolResult(ok=True, output=f"Edited {_display_path(target, self.root)} with {replacements} replacement(s)")

    def patch(self, patch: str) -> ToolResult:
        try:
            changes = _parse_codex_patch(patch)
            outputs = []
            for change in changes:
                kind = change["kind"]
                path = str(change["path"])
                target = self._resolve(path)
                if kind == "add":
                    if target.exists():
                        return ToolResult(ok=False, output=f"Cannot add existing file: {path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = str(change["content"])
                    target.write_text(content, encoding="utf-8")
                    outputs.append(f"added {path}")
                elif kind == "delete":
                    if not target.is_file():
                        return ToolResult(ok=False, output=f"Cannot delete missing file: {path}")
                    target.unlink()
                    outputs.append(f"deleted {path}")
                elif kind == "update":
                    if not target.is_file():
                        return ToolResult(ok=False, output=f"Cannot update missing file: {path}")
                    data = target.read_bytes()
                    if _looks_binary(data, target.suffix):
                        return ToolResult(ok=False, output=f"Refusing to patch likely binary file {_display_path(target, self.root)}")
                    content = data.decode("utf-8", errors="replace")
                    updated = _apply_patch_hunks(content, change["hunks"], path)
                    target.write_text(updated, encoding="utf-8")
                    outputs.append(f"updated {path}")
                else:
                    return ToolResult(ok=False, output=f"Unsupported patch change kind: {kind}")
            return ToolResult(ok=True, output="\n".join(outputs) or "Patch had no changes")
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))

    def glob(self, pattern: str, path: str = ".", max_results: int = 100) -> ToolResult:
        try:
            base = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not base.exists():
            return ToolResult(ok=False, output=f"No such path: {path}")
        max_results = max(1, min(max_results, 1000))
        if base.is_file():
            relative = _display_path(base, self.root)
            matches = [relative] if fnmatch.fnmatch(base.name, pattern) or fnmatch.fnmatch(relative, pattern) else []
            return ToolResult(ok=True, output="\n".join(matches) or "(no matches)")
        matches = []
        for child in sorted(base.rglob("*")):
            if not child.is_file():
                continue
            relative_to_base = child.relative_to(base).as_posix()
            display = _display_path(child, self.root)
            if fnmatch.fnmatch(relative_to_base, pattern) or fnmatch.fnmatch(display, pattern):
                matches.append(display)
                if len(matches) >= max_results:
                    break
        return ToolResult(ok=True, output="\n".join(matches) or "(no matches)")

    def grep(self, pattern: str, path: str = ".", max_results: int = 100) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.exists():
            return ToolResult(ok=False, output=f"No such path: {path}")
        max_results = max(1, min(max_results, 1000))
        command = ["rg", "--line-number", "--color", "never", "--max-count", str(max_results), pattern, str(target)]
        completed = subprocess.run(command, cwd=self.cwd, check=False, text=True, capture_output=True, timeout=self.shell_timeout_seconds)
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        if completed.returncode == 1 and not output:
            return ToolResult(ok=True, output="(no matches)")
        if completed.returncode not in {0, 1}:
            return ToolResult(ok=False, output=output or f"rg exited {completed.returncode}")
        lines = output.splitlines()
        if len(lines) > max_results:
            output = "\n".join(lines[:max_results]) + "\n[grep results truncated]"
        return ToolResult(ok=True, output=output or "(no matches)")

    def send_file(self, path: str) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {path}")
        size = target.stat().st_size
        if size > self.max_send_file_bytes:
            return ToolResult(ok=False, output=f"File exceeds {self.max_send_file_bytes} bytes: {_display_path(target, self.root)}")
        if not self.file_sender:
            return ToolResult(ok=True, output=f"File ready at {_display_path(target, self.root)}; no file sender is configured")
        try:
            sent = self.file_sender(target)
        except Exception as exc:  # noqa: BLE001 - keep artifact path visible when Discord delivery is flaky.
            return ToolResult(
                ok=False,
                output=f"File send failed for {_display_path(target, self.root)}: {exc}",
            )
        return ToolResult(ok=True, output=sent or f"Sent {_display_path(target, self.root)} to the user")

    def send_msg(self, msg: str) -> ToolResult:
        msg = str(msg).strip()
        if not msg:
            return ToolResult(ok=False, output="send_msg requires a non-empty msg")
        if len(msg) > 500:
            return ToolResult(ok=False, output="send_msg msg must be 500 characters or fewer")
        if not self.text_sender:
            return ToolResult(ok=True, output="Progress message ready; no text sender is configured")
        sent = self.text_sender(msg)
        return ToolResult(ok=True, output=sent or "Sent progress message to the user")

    def bash(self, command: str) -> ToolResult:
        try:
            shlex.split(command)[0]
        except (IndexError, ValueError) as exc:
            return ToolResult(ok=False, output=f"Invalid shell command: {exc}")

        completed = subprocess.run(
            command,
            cwd=self.cwd,
            shell=True,
            check=False,
            text=True,
            capture_output=True,
            timeout=self.shell_timeout_seconds,
            executable="/bin/bash",
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
        if len(output) > BASH_OUTPUT_LIMIT_CHARS:
            full_path = _write_large_tool_output(output)
            output = (
                output[:BASH_OUTPUT_LIMIT_CHARS]
                + f"\n[bash output truncated at {BASH_OUTPUT_LIMIT_CHARS} chars. "
                + f"Full stdout/stderr saved at {full_path}; use bash commands such as sed, rg, head, tail, wc, or cat on that file to inspect specific parts.]"
            )
        if self.shell_audit_store:
            self.shell_audit_store.record(command, completed.returncode == 0, "normal", "Allowed inside Docker container", completed.returncode, output)
        return ToolResult(ok=completed.returncode == 0, output=output or f"exit code {completed.returncode}")

    def exec_command(
        self,
        cmd: str,
        yield_time_ms: int = 10000,
        max_output_tokens: int = 20000,
        tty: bool = False,
        workdir: str | None = None,
        shell: str | None = None,
        login: bool = True,
    ) -> ToolResult:
        try:
            shlex.split(cmd)[0]
        except (IndexError, ValueError) as exc:
            return ToolResult(ok=False, output=f"Invalid process command: {exc}")

        try:
            resolved_workdir = self._resolve(workdir) if workdir else self.cwd
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        status = self.processes.exec_command(cmd, yield_time_ms, max_output_tokens, tty, resolved_workdir, shell, login)
        if self.shell_audit_store:
            self.shell_audit_store.record(cmd, True, "normal", "Started terminal command", None, json.dumps(status, sort_keys=True))
        return ToolResult(ok=True, output=json.dumps(status, sort_keys=True))

    def write_stdin(self, session_id: int, chars: str = "", yield_time_ms: int = 10000, max_output_tokens: int = 20000) -> ToolResult:
        status = self.processes.write_stdin(session_id, chars, yield_time_ms, max_output_tokens)
        return ToolResult(ok=True, output=json.dumps(status, sort_keys=True))

    def read_image(self, path: str, question: str = "Describe this image.") -> ToolResult:
        if not self.vision_client and not self.openai_api_key:
            return ToolResult(ok=False, output="read_image requires OPENAI_API_KEY")
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {path}")
        if target.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            return ToolResult(ok=False, output=f"Unsupported image type: {target.suffix or '(none)'}")
        data = target.read_bytes()
        if len(data) > self.max_inspect_image_bytes:
            return ToolResult(ok=False, output=f"Image exceeds {self.max_inspect_image_bytes} bytes: {_display_path(target, self.root)}")
        content_type = mimetypes.guess_type(target.name)[0] or _image_content_type(target.suffix)
        data_url = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
        client = self.vision_client or OpenAI(api_key=self.openai_api_key, base_url=self.openai_base_url)
        errors = []
        for model in self._candidate_models():
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": question.strip() or "Describe this image."},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                )
                content = response.choices[0].message.content or ""
                return ToolResult(ok=True, output=content)
            except Exception as exc:  # noqa: BLE001 - read_image should use the configured fallback chain.
                errors.append(f"{model}: {exc}")
        return ToolResult(ok=False, output="All configured OpenAI-compatible image inspection models failed: " + " | ".join(errors))

    def websearch(self, query: str, num_results: int = 5) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(self.exa.search(query, num_results), sort_keys=True))

    def get_runtime_config(self) -> ToolResult:
        if not self.runtime_config:
            return ToolResult(ok=False, output="Runtime config store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.runtime_config.all(), sort_keys=True))

    def _candidate_models(self) -> list[str]:
        primary = self.openai_model
        if self.runtime_config:
            primary = self.runtime_config.get("openai_model") or primary
        models = [primary]
        for model in self.openai_fallback_models.split(","):
            model = model.strip()
            if model and model not in models:
                models.append(model)
        return models

    def set_runtime_config(self, key: str, value: str) -> ToolResult:
        if not self.runtime_config:
            return ToolResult(ok=False, output="Runtime config store is not enabled")
        self.runtime_config.set(key, value)
        return ToolResult(ok=True, output=f"Set {key}={value}")

    def hook_set(self, name: str, prompt: str) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        self.event_hooks.upsert_hook(name, prompt)
        self.event_hooks.record("hook", name, f"HTTP webhook hook {name}", {})
        return ToolResult(
            ok=True,
            output=(
                f"Saved hook {name}; POST /webhooks/{name} records a local event and returns an event id/status immediately. "
                "It does not return the agent result. Use an adapter-specific CLI/API for replies to external systems. "
                "If API auth is enabled, backend callers should read the bearer token at runtime from "
                "/workspace/.pebble_shell/secrets/api_auth_token."
            ),
        )

    def hook_list(self, limit: int = 20) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.event_hooks.list_hooks(limit), sort_keys=True))

    def hook_show(self, name: str) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        hook = self.event_hooks.get_hook(name)
        if not hook:
            return ToolResult(ok=False, output=f"Unknown hook: {name}")
        return ToolResult(ok=True, output=json.dumps(hook, sort_keys=True))

    def hook_set_enabled(self, name: str, enabled: bool) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        self.event_hooks.set_hook_enabled(name, enabled)
        return ToolResult(ok=True, output=f"Set hook {name} enabled={enabled}")

    def hook_remove(self, name: str) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        self.event_hooks.delete_hook(name)
        return ToolResult(ok=True, output=f"Removed hook {name}; existing event history was kept")

    def hook_events(self, limit: int = 20) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.event_hooks.list_webhook_events(max(1, min(limit, 50))), sort_keys=True))

    def hook_event_replay(self, event_id: int) -> ToolResult:
        if not self.event_hooks:
            return ToolResult(ok=False, output="Event hook store is not enabled")
        if not self.webhook_replayer:
            return ToolResult(ok=False, output="Webhook replay scheduler is not enabled")
        event = self.event_hooks.get_webhook_event(event_id)
        if not event:
            return ToolResult(ok=False, output=f"Unknown webhook event: {event_id}")
        return ToolResult(ok=True, output=self.webhook_replayer(event_id))

    def cron_job_save(
        self,
        name: str,
        prompt: str,
        every_seconds: int,
        enabled: bool = True,
    ) -> ToolResult:
        if not self.cron:
            return ToolResult(ok=False, output="Cron store is not enabled")
        self.cron.upsert_job(name, prompt, int(every_seconds), enabled=bool(enabled))
        return ToolResult(ok=True, output=f"Saved cron job {name} every {every_seconds} seconds")

    def cron_list(self, jobs_limit: int = 20, runs_limit: int = 20) -> ToolResult:
        if not self.cron:
            return ToolResult(ok=False, output="Cron store is not enabled")
        return ToolResult(ok=True, output=dumps_cron_state(self.cron, jobs_limit, runs_limit))

    def cron_enable(self, name: str, enabled: bool) -> ToolResult:
        if not self.cron:
            return ToolResult(ok=False, output="Cron store is not enabled")
        self.cron.set_enabled(name, enabled)
        return ToolResult(ok=True, output=f"Set cron job {name} enabled={enabled}")

    def shell_audit(self, limit: int = 20) -> ToolResult:
        if not self.shell_audit_store:
            return ToolResult(ok=False, output="Shell audit store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.shell_audit_store.recent(limit), sort_keys=True))

    def subagent_start(self, prompt: str, folder: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.start(prompt, folder)

    def subagent_status(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.status_tool(job_id)

    def subagent_list(self, limit: int = 10, status: str | None = None) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.list_tool(limit, status)

    def subagent_dashboard(self, limit: int = 10, status: str | None = None) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.status_table_tool(limit, status)

    def subagent_summary(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.recent_status_tool(job_id)

    def subagent_ask(self, job_id: str, question: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.ask_tool(job_id, question)

    def subagent_cancel(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.cancel_tool(job_id)

    def subagent_pause(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.pause_tool(job_id)

    def subagent_send(self, job_id: str, message: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.message_tool(job_id, message)

    def subagent_delete(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.finish_tool(job_id)

    def subagent_events(self, job_id: str, limit: int = 20) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.events_tool(job_id, limit)

    def _resolve(self, path: str) -> Path:
        raw = str(path or ".")
        if raw.startswith("/"):
            target = (self.root / raw.lstrip("/")).resolve()
        else:
            target = (self.cwd / raw).resolve()
        return target


def _display_path(path: Path, root: Path) -> str:
    path = path.resolve()
    root = root.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _looks_binary(data: bytes, suffix: str) -> bool:
    if suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz", ".tar", ".sqlite3", ".db"}:
        return True
    sample = data[:4096]
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    decoded = sample.decode("utf-8", errors="replace")
    return decoded.count("\ufffd") / max(1, len(decoded)) > 0.05


def _image_content_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix.lower(), "application/octet-stream")


def _write_large_tool_output(output: str) -> str:
    directory = Path("/tmp/pebble_shell_tool_outputs")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"bash_{int(time.time() * 1000)}.log"
    path.write_text(output, encoding="utf-8", errors="replace")
    return path.as_posix()


def _parse_codex_patch(patch: str) -> list[dict[str, Any]]:
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("Patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("Patch must end with *** End Patch")
    changes: list[dict[str, Any]] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            index += 1
            content_lines = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError(f"Add File lines must start with + for {path}")
                content_lines.append(lines[index][1:])
                index += 1
            changes.append({"kind": "add", "path": path, "content": "\n".join(content_lines) + ("\n" if content_lines else "")})
            continue
        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            changes.append({"kind": "delete", "path": path})
            index += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            index += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                hunk_line = lines[index]
                if hunk_line.startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif hunk_line.startswith((" ", "+", "-")):
                    current.append(hunk_line)
                elif hunk_line == "*** End of File":
                    pass
                else:
                    raise ValueError(f"Unsupported patch line for {path}: {hunk_line}")
                index += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"Update File has no hunks: {path}")
            changes.append({"kind": "update", "path": path, "hunks": hunks})
            continue
        if not line.strip():
            index += 1
            continue
        raise ValueError(f"Unsupported patch directive: {line}")
    return changes


def _apply_patch_hunks(content: str, hunks: list[list[str]], path: str) -> str:
    updated = content
    for hunk in hunks:
        old_lines = []
        new_lines = []
        for line in hunk:
            prefix = line[0]
            text = line[1:]
            if prefix in {" ", "-"}:
                old_lines.append(text)
            if prefix in {" ", "+"}:
                new_lines.append(text)
        old = "\n".join(old_lines)
        new = "\n".join(new_lines)
        old_candidates = [old]
        if old:
            old_candidates.append(old + "\n")
        replaced = False
        for old_candidate in old_candidates:
            if old_candidate and old_candidate in updated:
                replacement = new + ("\n" if old_candidate.endswith("\n") else "")
                updated = updated.replace(old_candidate, replacement, 1)
                replaced = True
                break
        if not replaced:
            raise ValueError(f"Patch hunk did not match {path}")
    return updated


def _background_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "subagent_start",
            "description": "Start a write-capable background worker for long implementation, testing, research, or debugging work. The folder is required; relative folders resolve from /workspace, leading / starts at /workspace, .. is allowed, and missing folders are created.",
                "parameters": {
                    "type": "object",
                    "required": ["prompt", "folder"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "folder": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_status",
                    "description": "Get raw status, result, counters, prompt, and recent events for one background worker job by job id.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_list",
                    "description": "List recent background worker jobs as structured JSON, optionally filtered by status. Use subagent_dashboard when you want concise stored supervisor status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "status": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_dashboard",
                "description": (
                    "Show Pebble concise structured supervisor status for background agents: elapsed time, model, status, "
                    "steps, model calls, token usage when available, deterministic recent activity from stored events/results, and warning flags. "
                    "This does not call an LLM."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "status": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_summary",
                "description": "Get a richer recent-status summary for one background worker. This may call the flash model for that single job only and falls back to stored events/results if flash fails.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_ask",
                "description": "Ask a no-tool one-shot LLM a focused question over one worker's stored context without resuming or changing that worker.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id", "question"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "question": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_cancel",
                "description": "Request cooperative cancellation for one background worker.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_pause",
                "description": "Request that one background worker pause after its current model/tool step finishes.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_send",
                "description": "Send a new foreground instruction to a running, pausing, paused, blocked, or completed background worker. Paused, blocked, and completed workers resume with the same job id, folder, and stored context.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id", "message"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_delete",
                "description": "Destructively delete one inactive background worker's records, queued messages, events, and stored context. Use only when the worker is definitely no longer needed or cleanup/storage pressure requires it. Active workers must be paused or canceled first.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {"job_id": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent_events",
                "description": "Read recent internal events for one background worker.",
                "parameters": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                },
            },
        },
    ]


def _send_file_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Send a workspace file to the user. Use after creating artifacts such as PDFs, reports, images, or archives.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "File path to send."}
                },
            },
        },
    }


def _send_msg_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "send_msg",
            "description": (
                "Send a short progress update to the user immediately without ending the current turn. "
                "Use sparingly during long foreground tasks. The final answer is sent normally without this tool."
            ),
            "parameters": {
                "type": "object",
                "required": ["msg"],
                "properties": {
                    "msg": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "Short user-visible progress update, ideally under 400 characters.",
                    }
                },
            },
        },
    }
