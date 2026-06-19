from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencode_agent.agent import AgentResponse, HeartbeatResponse
from opencode_agent.cron import CronRunner, CronStore
from opencode_agent.exec_policy import ExecAuditStore
from opencode_agent.heartbeat import HeartbeatRunner
from opencode_agent.memory import MemoryStore
from opencode_agent.runtime_config import RuntimeConfigStore
from opencode_agent.self_improvement import SelfImprovementStore
from opencode_agent.skills import SkillLoader
from opencode_agent.tools import WorkspaceTools


@dataclass
class FakeSettings:
    heartbeat_every_seconds: int = 7200


class FakeAgent:
    def __init__(self, memory: MemoryStore, runtime_config: RuntimeConfigStore) -> None:
        self.memory = memory
        self.runtime_config = runtime_config
        self.runs: list[tuple[str, str, str]] = []

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.runs.append((content, user_id, channel_id))
        self.memory.add_message(channel_id, "user", content)
        self.memory.add_message(channel_id, "assistant", f"handled {user_id}")
        return AgentResponse(content=f"handled {user_id}", steps=2)

    async def run_heartbeat(self, channel_id: str) -> HeartbeatResponse:
        self.runs.append(("heartbeat", "heartbeat", channel_id))
        self.memory.record_heartbeat(channel_id, "Need attention", True)
        return HeartbeatResponse(content="Need attention", should_notify=True, steps=1)


@pytest.mark.asyncio
async def test_multi_day_self_modification_heartbeat_and_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    self_improvement = SelfImprovementStore(tmp_path / "self.sqlite3")
    cron = CronStore(tmp_path / "cron.sqlite3")
    exec_audit = ExecAuditStore(tmp_path / "exec.sqlite3")
    skills = SkillLoader(workspace, tmp_path)
    tools = WorkspaceTools(
        workspace,
        shell_timeout_seconds=1,
        runtime_config=runtime,
        skills=skills,
        self_improvement=self_improvement,
        cron=cron,
        exec_audit=exec_audit,
        memory=memory,
    )

    assert tools.set_runtime_config("heartbeat_every_seconds", "3600").ok
    assert tools.skill_save("incident-review", "Always check incident.md before proposing recovery.", "incident workflow").ok
    assert tools.webhook_hook_save("email-alert", "Triage the incoming email payload.", "ops").ok
    assert tools.cron_job_save("daily-check", "Review outstanding state.", "ops", 86_400).ok
    assert tools.write_file("MEMORY.md", "The recovery owner is platform-oncall.").ok
    assert "platform-oncall" in tools.read_file("MEMORY.md").output

    with sqlite3.connect(tmp_path / "cron.sqlite3") as conn:
        conn.execute("update cron_jobs set next_run_at = ?", (0,))

    delivered: list[tuple[str, str]] = []

    async def deliver(channel_id: str, content: str) -> None:
        delivered.append((channel_id, content))

    agent = FakeAgent(memory, runtime)
    cron_outputs = await CronRunner(agent, cron, deliver=deliver).tick()
    memory.set_last_contact("ops")
    heartbeat_output = await HeartbeatRunner(agent, FakeSettings(), deliver=deliver).tick()

    reloaded_runtime = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    reloaded_memory = MemoryStore(tmp_path / "memory.sqlite3")
    reloaded_self_improvement = SelfImprovementStore(tmp_path / "self.sqlite3")
    reloaded_cron = CronStore(tmp_path / "cron.sqlite3")

    assert reloaded_runtime.get("heartbeat_every_seconds") == "3600"
    assert reloaded_self_improvement.get_hook("email-alert") is not None
    assert reloaded_cron.list_runs("daily-check")[0]["ok"]
    assert reloaded_memory.get_last_contact() == "ops"
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == "The recovery owner is platform-oncall."
    assert cron_outputs == ["handled cron:daily-check"]
    assert heartbeat_output == "Need attention"
    assert ("ops", "handled cron:daily-check") in delivered
    assert ("ops", "Need attention") in delivered
