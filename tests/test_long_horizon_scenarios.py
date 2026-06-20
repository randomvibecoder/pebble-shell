from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from pebble_shell.agent import AgentResponse, HeartbeatResponse
from pebble_shell.cron import CronRunner, CronStore
from pebble_shell.shell_audit import ShellAuditStore
from pebble_shell.heartbeat import HeartbeatRunner
from pebble_shell.memory import MemoryStore
from pebble_shell.runtime_config import RuntimeConfigStore
from pebble_shell.self_improvement import SelfImprovementStore
from pebble_shell.tools import WorkspaceTools


@dataclass
class FakeSettings:
    heartbeat_every_seconds: int = 7200


class FakeAgent:
    def __init__(self, memory: MemoryStore, runtime_config: RuntimeConfigStore) -> None:
        self.memory = memory
        self.runtime_config = runtime_config
        self.runs: list[tuple[str, str]] = []
        self._deliver = None

    async def run_internal_event(self, content: str, source: str) -> AgentResponse:
        self.runs.append((content, source))
        self.memory.add_message("user", content)
        self.memory.add_message("assistant", f"handled {source}")
        return AgentResponse(content=f"handled {source}", steps=2)

    async def run_heartbeat(self) -> HeartbeatResponse:
        self.runs.append(("heartbeat", "heartbeat"))
        self.memory.record_heartbeat("Need attention", True)
        return HeartbeatResponse(content="Need attention", should_notify=True, steps=1)


@pytest.mark.asyncio
async def test_multi_day_self_modification_heartbeat_and_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    self_improvement = SelfImprovementStore(tmp_path / "self.sqlite3")
    cron = CronStore(tmp_path / "cron.sqlite3")
    shell_audit = ShellAuditStore(tmp_path / "shell.sqlite3")
    tools = WorkspaceTools(
        workspace,
        shell_timeout_seconds=1,
        runtime_config=runtime,
        self_improvement=self_improvement,
        cron=cron,
        shell_audit=shell_audit,
        memory=memory,
    )

    assert tools.set_runtime_config("heartbeat_every_seconds", "3600").ok
    assert tools.webhook_hook_save("email-alert", "Triage the incoming email payload.").ok
    assert tools.cron_job_save("daily-check", "Review outstanding state.", 86_400).ok
    assert tools.write_file("context/MEMORY.md", "The recovery owner is platform-oncall.").ok
    assert "platform-oncall" in tools.read_file("context/MEMORY.md").output

    with sqlite3.connect(tmp_path / "cron.sqlite3") as conn:
        conn.execute("update cron_jobs set next_run_at = ?", (0,))

    delivered: list[str] = []

    async def deliver(content: str) -> None:
        delivered.append(content)

    agent = FakeAgent(memory, runtime)
    agent._deliver = deliver
    cron_outputs = await CronRunner(agent, cron).tick()
    heartbeat_output = await HeartbeatRunner(agent, FakeSettings()).tick()

    reloaded_runtime = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    reloaded_memory = MemoryStore(tmp_path / "memory.sqlite3")
    reloaded_self_improvement = SelfImprovementStore(tmp_path / "self.sqlite3")
    reloaded_cron = CronStore(tmp_path / "cron.sqlite3")

    assert reloaded_runtime.get("heartbeat_every_seconds") == "3600"
    assert reloaded_self_improvement.get_hook("email-alert") is not None
    assert reloaded_cron.list_runs("daily-check")[0]["ok"]
    assert reloaded_memory.get_context("", 10).recent_messages
    assert (workspace / "context" / "MEMORY.md").read_text(encoding="utf-8") == "The recovery owner is platform-oncall."
    assert cron_outputs == ["handled cron:daily-check"]
    assert heartbeat_output == "Need attention"
    assert "handled cron:daily-check" in delivered
    assert "Need attention" in delivered
