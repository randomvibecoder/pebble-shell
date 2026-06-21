from __future__ import annotations

import base64
import fnmatch
import json
import mimetypes
import shutil
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
from .public_sites import list_public_sites
from .runtime_config import RuntimeConfigStore
from .self_improvement import SelfImprovementStore

MAX_READ_FILE_BYTES = 40_000
MAX_READ_FILE_CHARS = 40_000
BASH_OUTPUT_LIMIT_CHARS = 50_000
MAX_INSPECT_IMAGE_BYTES = 4_000_000
MAX_SEND_FILE_BYTES = 25_000_000
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FileSender = Callable[[Path], str]
TextSender = Callable[[str], str]


class ToolResult(BaseModel):
    ok: bool
    output: str


class WorkspaceTools:
    def __init__(
        self,
        root: Path,
        shell_timeout_seconds: int,
        runtime_config: RuntimeConfigStore | None = None,
        self_improvement: SelfImprovementStore | None = None,
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
        max_send_file_bytes: int = MAX_SEND_FILE_BYTES,
        cwd: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cwd = (cwd or self.root).resolve()
        if self.cwd != self.root and self.root not in self.cwd.parents:
            raise ValueError(f"Tool cwd escapes workspace: {self.cwd}")
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.shell_timeout_seconds = shell_timeout_seconds
        self.runtime_config = runtime_config
        self.self_improvement = self_improvement
        self.cron = cron
        self.shell_audit = shell_audit
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
        self.max_send_file_bytes = max(1, max_send_file_bytes)

    def definitions(self, include_background_tools: bool = True) -> list[dict[str, Any]]:
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": "ls",
                    "description": "List files under a workspace path. In a background worker, relative paths resolve from the assigned folder; leading / means /workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory or file path."}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "glob",
                    "description": "Find workspace files by glob pattern. In a background worker, relative paths resolve from the assigned folder; leading / means /workspace.",
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
                    "description": "Search text files with a regex pattern. In a background worker, relative paths resolve from the assigned folder; leading / means /workspace.",
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
                    "description": "Read a UTF-8 text file. In a background worker, relative paths resolve from the assigned folder; leading / means /workspace.",
                    "parameters": {
                        "type": "object",
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string", "description": "Workspace-relative file path."}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "description": "Write UTF-8 text to a file. In a background worker, relative paths resolve from the assigned folder; leading / means /workspace.",
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
                    "name": "publish_static_site",
                    "description": "Publish a workspace file or directory under /public/{name} so it can be opened from the agent HTTP service.",
                    "parameters": {
                        "type": "object",
                        "required": ["source_path", "name"],
                        "properties": {
                            "source_path": {"type": "string", "description": "Workspace-relative file or directory to publish."},
                            "name": {"type": "string", "description": "Public site slug, using letters, numbers, underscores, or hyphens."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "public_sites_list",
                    "description": "List static sites currently published under /public.",
                    "parameters": {"type": "object", "properties": {}},
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
                    "name": "process_start",
                    "description": "Start a long-running background process inside the workspace, such as a dev server.",
                    "parameters": {
                        "type": "object",
                        "required": ["name", "command"],
                        "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "processes_list",
                    "description": "List background processes started by this agent.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "process_status",
                    "description": "Inspect one background process status.",
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
                    "name": "process_logs",
                    "description": "Read recent logs for one background process.",
                    "parameters": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "process_stop",
                    "description": "Stop one background process.",
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
                    "name": "webhook_hook_save",
                    "description": "Create or update a named webhook hook. POST /webhooks/{name} triggers an agent run with the hook prompt and JSON payload in this chat.",
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
                    "name": "self_improvements_list",
                    "description": "List recent self-improvements, registered hooks, and recent webhook events.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webhook_events_list",
                    "description": "List recent webhook payload receipts and processing status for hooks such as suggestion boxes.",
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
                    "name": "cron_jobs_list",
                    "description": "List scheduled jobs and recent run results.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cron_job_set_enabled",
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
                    "name": "shell_audit_recent",
                    "description": "List recent shell command audit records.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        if include_background_tools:
            definitions.append(_send_file_tool_definition())
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
                return self.ls(arguments.get("path", "."))
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
            if name == "publish_static_site":
                return self.publish_static_site(arguments["source_path"], arguments["name"])
            if name == "send_file":
                return self.send_file(arguments["path"])
            if name == "send_msg":
                return self.send_msg(arguments["msg"])
            if name == "public_sites_list":
                return self.public_sites_list()
            if name == "bash":
                return self.bash(arguments["command"])
            if name == "process_start":
                return self.process_start(arguments["name"], arguments["command"])
            if name == "processes_list":
                return self.processes_list()
            if name == "process_status":
                return self.process_status(arguments["name"])
            if name == "process_logs":
                return self.process_logs(arguments["name"], int(arguments.get("max_chars", 4000)))
            if name == "process_stop":
                return self.process_stop(arguments["name"])
            if name == "read_image":
                return self.read_image(arguments["path"], arguments.get("question", "Describe this image."))
            if name == "websearch":
                return self.websearch(arguments["query"], int(arguments.get("num_results", 5)))
            if name == "get_runtime_config":
                return self.get_runtime_config()
            if name == "set_runtime_config":
                return self.set_runtime_config(arguments["key"], arguments["value"])
            if name == "webhook_hook_save":
                return self.webhook_hook_save(arguments["name"], arguments["prompt"])
            if name == "self_improvements_list":
                return self.self_improvements_list()
            if name == "webhook_events_list":
                return self.webhook_events_list(int(arguments.get("limit", 20)))
            if name == "cron_job_save":
                return self.cron_job_save(
                    arguments["name"],
                    arguments["prompt"],
                    int(arguments["every_seconds"]),
                    arguments.get("enabled", True),
                )
            if name == "cron_jobs_list":
                return self.cron_jobs_list()
            if name == "cron_job_set_enabled":
                return self.cron_job_set_enabled(arguments["name"], bool(arguments["enabled"]))
            if name == "shell_audit_recent":
                return self.shell_audit_recent()
            if name == "background_task_start":
                return self.background_task_start(arguments["prompt"], arguments["folder"])
            if name == "background_task_status":
                return self.background_task_status(arguments["job_id"])
            if name == "background_tasks_list":
                return self.background_tasks_list(int(arguments.get("limit", 10)), arguments.get("status"))
            if name == "background_agents_status":
                return self.background_agents_status(int(arguments.get("limit", 10)), arguments.get("status"))
            if name == "background_task_ask":
                return self.background_task_ask(arguments["job_id"], arguments["question"])
            if name == "background_task_cancel":
                return self.background_task_cancel(arguments["job_id"])
            if name == "background_task_pause":
                return self.background_task_pause(arguments["job_id"])
            if name == "background_task_message":
                return self.background_task_message(arguments["job_id"], arguments["message"])
            if name == "background_task_events":
                return self.background_task_events(arguments["job_id"], int(arguments.get("limit", 20)))
        except KeyError as exc:
            return ToolResult(ok=False, output=f"Missing required argument: {exc}")
        except Exception as exc:  # noqa: BLE001 - tool errors should return to the model.
            return ToolResult(ok=False, output=str(exc))

        return ToolResult(ok=False, output=f"Unknown tool: {name}")

    def ls(self, path: str = ".") -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.exists():
            return ToolResult(ok=False, output=f"No such path: {path}")
        if target.is_file():
            return ToolResult(ok=True, output=target.relative_to(self.root).as_posix())

        entries = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.relative_to(self.root).as_posix()}{suffix}")
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
                    f"Refusing to read likely binary file {target.relative_to(self.root)} into model context. "
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
        return ToolResult(ok=True, output=f"Wrote {len(content.encode('utf-8'))} bytes to {target.relative_to(self.root)}")

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
            return ToolResult(ok=False, output=f"Refusing to edit likely binary file {target.relative_to(self.root)}")
        content = data.decode("utf-8", errors="replace")
        count = content.count(old)
        if count == 0:
            return ToolResult(ok=False, output=f"old text not found in {target.relative_to(self.root)}")
        if count > 1 and not replace_all:
            return ToolResult(ok=False, output=f"old text occurs {count} times; set replace_all=true or provide a more specific old string")
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        target.write_text(updated, encoding="utf-8")
        replacements = count if replace_all else 1
        return ToolResult(ok=True, output=f"Edited {target.relative_to(self.root)} with {replacements} replacement(s)")

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
                        return ToolResult(ok=False, output=f"Refusing to patch likely binary file {target.relative_to(self.root)}")
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
            relative = base.relative_to(self.root).as_posix()
            matches = [relative] if fnmatch.fnmatch(base.name, pattern) or fnmatch.fnmatch(relative, pattern) else []
            return ToolResult(ok=True, output="\n".join(matches) or "(no matches)")
        matches = []
        for child in sorted(base.rglob("*")):
            if not child.is_file():
                continue
            relative_to_base = child.relative_to(base).as_posix()
            relative_to_root = child.relative_to(self.root).as_posix()
            if fnmatch.fnmatch(relative_to_base, pattern) or fnmatch.fnmatch(relative_to_root, pattern):
                matches.append(relative_to_root)
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

    def publish_static_site(self, source_path: str, name: str) -> ToolResult:
        try:
            source = self._resolve(source_path)
            slug = _normalize_public_slug(name)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not source.exists():
            return ToolResult(ok=False, output=f"No such path: {source_path}")
        if _contains_hidden_part(source.relative_to(self.root)):
            return ToolResult(ok=False, output="Refusing to publish hidden workspace paths")

        public_root = self.root / "public"
        destination = public_root / slug
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)

        if source.is_file():
            shutil.copy2(source, destination / source.name)
            entry = source.name
        else:
            for child in source.rglob("*"):
                relative = child.relative_to(source)
                if _contains_hidden_part(relative):
                    continue
                target = destination / relative
                if child.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif child.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child, target)
            entry = "index.html" if (destination / "index.html").is_file() else ""

        public_path = f"/public/{slug}/" + entry
        return ToolResult(ok=True, output=f"Published {source.relative_to(self.root)} to {public_path}")

    def public_sites_list(self) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(list_public_sites(self.root), sort_keys=True))

    def send_file(self, path: str) -> ToolResult:
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {path}")
        size = target.stat().st_size
        if size > self.max_send_file_bytes:
            return ToolResult(ok=False, output=f"File exceeds {self.max_send_file_bytes} bytes: {target.relative_to(self.root)}")
        if not self.file_sender:
            return ToolResult(ok=True, output=f"File ready at {target.relative_to(self.root)}; no file sender is configured")
        try:
            sent = self.file_sender(target)
        except Exception as exc:  # noqa: BLE001 - keep artifact path visible when Discord delivery is flaky.
            return ToolResult(
                ok=False,
                output=f"File send failed for {target.relative_to(self.root)}: {exc}",
            )
        return ToolResult(ok=True, output=sent or f"Sent {target.relative_to(self.root)} to the user")

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
        if self.shell_audit:
            self.shell_audit.record(command, completed.returncode == 0, "normal", "Allowed inside Docker container", completed.returncode, output)
        return ToolResult(ok=completed.returncode == 0, output=output or f"exit code {completed.returncode}")

    def process_start(self, name: str, command: str) -> ToolResult:
        try:
            shlex.split(command)[0]
        except (IndexError, ValueError) as exc:
            return ToolResult(ok=False, output=f"Invalid process command: {exc}")

        status = self.processes.start(name, command)
        if self.shell_audit:
            self.shell_audit.record(command, True, "normal", f"Started background process {name}", None, json.dumps(status, sort_keys=True))
        return ToolResult(ok=True, output=json.dumps(status, sort_keys=True))

    def processes_list(self) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(self.processes.list(), sort_keys=True))

    def process_status(self, name: str) -> ToolResult:
        return ToolResult(ok=True, output=json.dumps(self.processes.status(name), sort_keys=True))

    def process_logs(self, name: str, max_chars: int = 4000) -> ToolResult:
        return ToolResult(ok=True, output=self.processes.logs(name, max_chars))

    def process_stop(self, name: str) -> ToolResult:
        status = self.processes.stop(name)
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
            return ToolResult(ok=False, output=f"Image exceeds {self.max_inspect_image_bytes} bytes: {target.relative_to(self.root)}")
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

    def webhook_hook_save(self, name: str, prompt: str) -> ToolResult:
        if not self.self_improvement:
            return ToolResult(ok=False, output="Self-improvement store is not enabled")
        self.self_improvement.upsert_hook(name, prompt)
        self.self_improvement.record("webhook_hook", name, f"Webhook hook {name}", {})
        return ToolResult(ok=True, output=f"Saved webhook hook {name}; trigger with POST /webhooks/{name}")

    def self_improvements_list(self) -> ToolResult:
        if not self.self_improvement:
            return ToolResult(ok=False, output="Self-improvement store is not enabled")
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "improvements": self.self_improvement.list_records(),
                    "webhook_hooks": self.self_improvement.list_hooks(),
                    "webhook_events": self.self_improvement.list_webhook_events(),
                },
                sort_keys=True,
            ),
        )

    def webhook_events_list(self, limit: int = 20) -> ToolResult:
        if not self.self_improvement:
            return ToolResult(ok=False, output="Self-improvement store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.self_improvement.list_webhook_events(max(1, min(limit, 50))), sort_keys=True))

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

    def cron_jobs_list(self) -> ToolResult:
        if not self.cron:
            return ToolResult(ok=False, output="Cron store is not enabled")
        return ToolResult(ok=True, output=dumps_cron_state(self.cron))

    def cron_job_set_enabled(self, name: str, enabled: bool) -> ToolResult:
        if not self.cron:
            return ToolResult(ok=False, output="Cron store is not enabled")
        self.cron.set_enabled(name, enabled)
        return ToolResult(ok=True, output=f"Set cron job {name} enabled={enabled}")

    def shell_audit_recent(self) -> ToolResult:
        if not self.shell_audit:
            return ToolResult(ok=False, output="Shell audit store is not enabled")
        return ToolResult(ok=True, output=json.dumps(self.shell_audit.recent(), sort_keys=True))

    def background_task_start(self, prompt: str, folder: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.start(prompt, folder)

    def background_task_status(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.status_tool(job_id)

    def background_tasks_list(self, limit: int = 10, status: str | None = None) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.list_tool(limit, status)

    def background_agents_status(self, limit: int = 10, status: str | None = None) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.status_table_tool(limit, status)

    def background_task_ask(self, job_id: str, question: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.ask_tool(job_id, question)

    def background_task_cancel(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.cancel_tool(job_id)

    def background_task_pause(self, job_id: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.pause_tool(job_id)

    def background_task_message(self, job_id: str, message: str) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.message_tool(job_id, message)

    def background_task_events(self, job_id: str, limit: int = 20) -> ToolResult:
        if not self.background_tasks:
            return ToolResult(ok=False, output="Background task service is not enabled")
        return self.background_tasks.events_tool(job_id, limit)

    def _resolve(self, path: str) -> Path:
        raw = str(path or ".")
        if raw.startswith("/"):
            target = (self.root / raw.lstrip("/")).resolve()
        else:
            target = (self.cwd / raw).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"Path escapes workspace: {path}")
        return target


def _normalize_public_slug(name: str) -> str:
    slug = name.strip()
    if not slug or len(slug) > 64 or any(not (char.isalnum() or char in "-_") for char in slug):
        raise ValueError("public site name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens")
    return slug


def _contains_hidden_part(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


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
                "name": "background_task_start",
                    "description": "Start a write-capable background worker for long implementation, testing, research, or debugging work. The folder is required; leading / means /workspace and missing folders are created.",
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
                "name": "background_task_status",
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
                "name": "background_tasks_list",
                    "description": "List recent background worker jobs as structured JSON, optionally filtered by status. Use background_agents_status when you want the readable supervisor table.",
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
                "name": "background_agents_status",
                "description": (
                    "Show Pebble a readable supervisor table for background agents: elapsed time, model, status, "
                    "steps, token usage when available, recent activity summarized by the flash model, and warning flags."
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
                "name": "background_task_ask",
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
                "name": "background_task_cancel",
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
                "name": "background_task_pause",
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
                "name": "background_task_message",
                "description": "Send a new foreground instruction to a running, blocked, or needs-attention background worker. Paused workers resume with the same stored context and continue the original task.",
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
                "name": "background_task_events",
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
                    "path": {"type": "string", "description": "Workspace-relative file path to send."}
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
