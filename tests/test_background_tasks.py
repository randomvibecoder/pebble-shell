from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from pebble_shell.agent import AgentResponse, CodingAgent
from pebble_shell.config import Settings
from pebble_shell.background_tasks import _normalize_workspace_folder, _render_status_yaml


def test_background_folder_allows_parent_traversal() -> None:
    assert _normalize_workspace_folder("../tmp/worker") == "../tmp/worker"
    assert _normalize_workspace_folder("/../tmp/worker") == "../tmp/worker"
    assert _normalize_workspace_folder("/site") == "site"


class FakeChoice:
    def __init__(self, message: object) -> None:
        self.message = message


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeResponse:
    def __init__(self, message: object, usage: FakeUsage | None = None) -> None:
        self.choices = [FakeChoice(message)]
        self.usage = usage


class FinalMessage:
    tool_calls = []

    def __init__(self, content: str) -> None:
        self.content = content

    def model_dump(self, exclude_none: bool = True) -> dict[str, object]:
        return {"role": "assistant", "content": self.content}


class ToolMessage:
    content = None

    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        class Function:
            pass

        class ToolCall:
            pass

        function = Function()
        function.name = name
        function.arguments = json.dumps(arguments)
        call = ToolCall()
        call.id = "call-1"
        call.function = function
        self.tool_calls = [call]

    def model_dump(self, exclude_none: bool = True) -> dict[str, object]:
        call = self.tool_calls[0]
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
            ],
        }


class SequencedCompletions:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("No fake completion response queued")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.chat = type("Chat", (), {"completions": SequencedCompletions(responses)})()


@pytest.mark.asyncio
async def test_background_task_tools_limit_active_workers_to_four(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.bind_background_loop()

    async def hold_until_cancelled(job) -> AgentResponse:
        while not agent.background_store.should_cancel(job.id):
            await asyncio.sleep(0.01)
        return AgentResponse(content="cancelled", steps=1)

    agent.run_background_task = hold_until_cancelled  # type: ignore[method-assign]

    started = [
        agent.tools.run("subagent_start", {"prompt": f"build site {index}", "folder": f"/site-{index}"})
        for index in range(4)
    ]
    fifth = agent.tools.run("subagent_start", {"prompt": "build one more", "folder": "/overflow"})

    assert all(result.ok for result in started)
    assert not fifth.ok
    assert "Maximum active background tasks reached: 4" in fifth.output
    jobs = [json.loads(result.output) for result in started]
    assert len({job["id"] for job in jobs}) == 4
    assert {job["folder"] for job in jobs} == {f"site-{index}" for index in range(4)}
    assert all("user_id" not in job and "channel_id" not in job for job in jobs)

    for job in jobs:
        agent.tools.run("subagent_cancel", {"job_id": job["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


@pytest.mark.asyncio
async def test_subagent_start_sends_debug_notice_and_creates_folder(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.bind_background_loop()
    delivered: list[str] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    async def hold_until_cancelled(job) -> AgentResponse:
        while not agent.background_store.should_cancel(job.id):
            await asyncio.sleep(0.01)
        return AgentResponse(content="canceled", steps=1)

    agent.set_deliver(deliver)
    agent.run_background_task = hold_until_cancelled  # type: ignore[method-assign]

    result = agent.tools.run("subagent_start", {"prompt": "build a dashboard with a lot of detail", "folder": "/dashboards/main"})

    assert result.ok
    job = json.loads(result.output)
    assert job["folder"] == "dashboards/main"
    assert (agent.settings.agent_workspace / "dashboards/main").is_dir()
    await _wait_until(lambda: bool(delivered))
    assert delivered[0].startswith(f"[subagent created] id={job['id']} folder=/dashboards/main prompt=build a dashboard")
    agent.tools.run("subagent_cancel", {"job_id": job["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


@pytest.mark.asyncio
async def test_subagent_pause_stops_after_current_step_and_message_resumes(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("pause me", "background_jobs/test")

    async def one_step(job) -> AgentResponse:
        return AgentResponse(content="step finished", steps=1)

    agent.run_background_task = one_step  # type: ignore[method-assign]
    agent.background_store.pause_job(job.id)

    await agent.background_tasks._run_job(job.id)

    paused = agent.background_store.get_job(job.id)
    assert paused is not None
    assert paused.status == "paused"

    result = agent.background_tasks.message_tool(job.id, "keep going")

    assert result.ok
    resumed = agent.background_store.get_job(job.id)
    assert resumed is not None
    assert resumed.status == "running"


@pytest.mark.asyncio
async def test_background_worker_send_msg_records_progress_and_wakes_foreground(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    delivered: list[str] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    agent.set_deliver(deliver)
    job = agent.background_store.create_job("make progress", "background_jobs/test")
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("send_msg", {"msg": "Halfway through the build."})),
            FakeResponse(FinalMessage("Progress noted.")),
            FakeResponse(FinalMessage("Continuing after progress update.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run_background_task(job)

    assert response.content == "Continuing after progress update."
    events = agent.background_store.list_events(job.id, limit=10)
    assert any(event["kind"] == "progress" and "Halfway through" in event["message"] for event in events)
    assert any(event["kind"] == "foreground_wakeup" and "Progress noted" in event["message"] for event in events)
    assert delivered == ["Progress noted."]


@pytest.mark.asyncio
async def test_foreground_cannot_fake_background_start_without_tool_call(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.bind_background_loop()

    async def hold_until_cancelled(job) -> AgentResponse:
        while not agent.background_store.should_cancel(job.id):
            await asyncio.sleep(0.01)
        return AgentResponse(content="cancelled", steps=1)

    agent.run_background_task = hold_until_cancelled  # type: ignore[method-assign]
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("Worker queued. Job ID: `bg_fake_123456`")),
            FakeResponse(ToolMessage("subagent_start", {"prompt": "monitor the test sites", "folder": "/real-monitor"})),
            FakeResponse(FinalMessage("Worker started with the real tool result.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run_user_message(
        "Start a background worker with subagent_start. Folder: real-monitor. Task: monitor the test sites.",
    )

    assert response.content == "Worker started with the real tool result."
    calls = agent.client.chat.completions.calls  # type: ignore[attr-defined]
    assert len(calls) == 3
    assert "You have not called `subagent_start`" in calls[1]["messages"][-1]["content"]
    jobs = agent.background_store.list_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0]["folder"] == "real-monitor"
    assert jobs[0]["id"] != "bg_fake_123456"

    agent.tools.run("subagent_cancel", {"job_id": jobs[0]["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


@pytest.mark.asyncio
async def test_background_worker_stores_exact_tool_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "background_jobs/test")
    (agent.settings.agent_workspace / job.folder).mkdir(parents=True)
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("write", {"path": "index.html", "content": "<body>blue</body>"})),
            FakeResponse(FinalMessage("Wrote the page.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run_background_task(job)

    assert response.content == "Wrote the page."
    assert (agent.settings.agent_workspace / job.folder / "index.html").read_text(encoding="utf-8") == "<body>blue</body>"
    context = agent.background_store.get_context(job.id)
    assert any(message.get("role") == "tool" and "Wrote" in str(message.get("content")) for message in context)
    first_call = agent.client.chat.completions.calls[0]  # type: ignore[attr-defined]
    tool_names = {tool["function"]["name"] for tool in first_call["tools"]}
    assert "subagent_start" not in tool_names
    assert "hook_set" not in tool_names
    assert "cron_job_save" not in tool_names
    assert "heartbeat_set" not in tool_names
    assert "send_file" not in tool_names
    assert "send_msg" in tool_names
    system_text = "\n".join(str(message.get("content", "")) for message in first_call["messages"] if message.get("role") == "system")
    assert "Use send_msg often enough to keep foreground Pebble informed" in system_text
    assert "Summarize what changed or what you verified" in system_text
    assert "no cron tools, no webhook tools, no subagent tools" in system_text
    assert "context/WORKER_TOOLS.md" in system_text
    assert "context/TOOLS.md" not in system_text
    assert "full shell control in the configured runtime environment" in system_text
    assert "may install packages, CLIs, browsers, dependencies" in system_text
    assert "Do not use exec_command as a way to hand off the assigned job" in system_text
    assert "Docker" not in system_text
    assert "container" not in system_text
    prompt_text = "\n".join(str(message.get("content", "")) for message in first_call["messages"])
    assert "Original Discord user" not in prompt_text
    assert "Original Discord channel" not in prompt_text


@pytest.mark.asyncio
async def test_background_job_self_check_complete_records_usage(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "background_jobs/test")
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("Wrote and verified the page."), FakeUsage(10, 4, 14)),
            FakeResponse(FinalMessage("COMPLETE"), FakeUsage(5, 1, 6)),
        ]
    )  # type: ignore[assignment]

    await agent.background_tasks._run_job(job.id)

    saved = agent.background_store.get_job(job.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.model_calls == 2
    assert saved.prompt_tokens == 15
    assert saved.completion_tokens == 5
    assert saved.total_tokens == 20
    assert saved.last_model == agent.current_model
    context = agent.background_store.get_context(job.id)
    assert any(message.get("role") == "user" and "Reply with exactly one of" in str(message.get("content")) for message in context)
    assert any(message.get("role") == "assistant" and message.get("content") == "COMPLETE" for message in context)


@pytest.mark.asyncio
async def test_background_job_needs_attention_after_three_self_check_retries(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("finish a hard task", "background_jobs/test")
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("I am not done yet.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
            FakeResponse(FinalMessage("Still not done.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
            FakeResponse(FinalMessage("Still needs work.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
            FakeResponse(FinalMessage("Summary: the worker ran out of retries after repeated incomplete checks.")),
        ]
    )  # type: ignore[assignment]

    await agent.background_tasks._run_job(job.id)

    saved = agent.background_store.get_job(job.id)
    assert saved is not None
    assert saved.status == "blocked"
    assert saved.self_check_retries == 3
    assert "ran out of retries" in saved.attention_summary
    events = agent.background_store.list_events(job.id, limit=20)
    assert any(event["kind"] == "blocked" for event in events)


@pytest.mark.asyncio
async def test_background_job_needs_attention_survives_flash_summary_failure(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("finish a hard task", "background_jobs/test")
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("I am not done yet.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
            FakeResponse(FinalMessage("Still not done.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
            FakeResponse(FinalMessage("Still needs work.")),
            FakeResponse(FinalMessage("NEEDS_MORE_WORK")),
        ]
    )  # type: ignore[assignment]

    async def fail_flash(**kwargs):
        raise RuntimeError("flash unavailable")

    agent._flash_completion = fail_flash  # type: ignore[method-assign]

    await agent.background_tasks._run_job(job.id)

    saved = agent.background_store.get_job(job.id)
    assert saved is not None
    assert saved.status == "blocked"
    assert "needs foreground attention" in saved.attention_summary
    assert "flash" not in saved.attention_summary.lower()


@pytest.mark.asyncio
async def test_background_job_blocked_remains_messageable_and_resumes(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("do a blocked task", "background_jobs/test")
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("Blocked on missing input.")),
            FakeResponse(FinalMessage("BLOCKED")),
            FakeResponse(FinalMessage("Summary: missing input blocked the job.")),
        ]
    )  # type: ignore[assignment]
    await agent.background_tasks._run_job(job.id)
    saved = agent.background_store.get_job(job.id)
    assert saved is not None
    assert saved.status == "blocked"

    result = agent.background_tasks.message_tool(job.id, "Here is the missing input. Continue.")

    assert result.ok
    resumed = agent.background_store.get_job(job.id)
    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.self_check_retries == 0


@pytest.mark.asyncio
async def test_background_job_completed_can_be_messaged_and_completed_again(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "background_jobs/test")
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "Original page is done."}])
    agent.background_store.complete_job(job.id, "Original page complete.", 2)
    completed = agent.background_store.get_job(job.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.finished_at is not None
    old_finished_at = completed.finished_at

    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("Applied the follow-up fix.")),
            FakeResponse(FinalMessage("COMPLETE")),
        ]
    )  # type: ignore[assignment]

    result = agent.background_tasks.message_tool(job.id, "Fix the typo the user just found.")

    assert result.ok
    reopened = agent.background_store.get_job(job.id)
    assert reopened is not None
    assert reopened.id == job.id
    assert reopened.folder == job.folder
    assert reopened.status == "running"
    assert reopened.finished_at is None
    assert reopened.result == "Original page complete."
    assert old_finished_at

    await agent.background_tasks._run_job(job.id)

    rerun = agent.background_store.get_job(job.id)
    assert rerun is not None
    assert rerun.status == "completed"
    assert rerun.result == "Applied the follow-up fix."
    context = agent.background_store.get_context(job.id)
    assert any("Original page is done" in str(message.get("content")) for message in context)
    assert any("Fix the typo" in str(message.get("content")) for message in context)
    events = agent.background_store.list_events(job.id, limit=20)
    assert any(event["kind"] == "reopened" for event in events)
    assert any(event["kind"] == "message_delivered" and "Fix the typo" in event["message"] for event in events)


def test_background_job_canceled_rejects_messages(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("cancel me", "background_jobs/test")
    agent.background_store.mark_cancelled(job.id, 0)

    result = agent.tools.run("subagent_send", {"job_id": job.id, "message": "Resume this canceled worker."})

    assert not result.ok
    assert "not messageable" in result.output
    canceled = agent.background_store.get_job(job.id)
    assert canceled is not None
    assert canceled.status == "canceled"


def test_subagent_delete_deletes_inactive_job_records_and_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make cleanup data", "background_jobs/test")
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "stored context"}])
    agent.background_store.enqueue_message(job.id, "queued instruction")
    agent.background_store.complete_job(job.id, "done", 1)

    result = agent.tools.run("subagent_delete", {"job_id": job.id})

    assert result.ok
    assert "deleted job records and stored context" in result.output
    assert agent.background_store.get_job(job.id) is None
    assert agent.background_store.get_context(job.id) == []
    assert agent.background_store.list_events(job.id, limit=10) == []
    assert agent.background_store.drain_messages(job.id) == []


def test_subagent_delete_rejects_active_worker(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("still running", "background_jobs/test")

    result = agent.tools.run("subagent_delete", {"job_id": job.id})

    assert not result.ok
    assert "pause or cancel" in result.output
    assert agent.background_store.get_job(job.id) is not None


def test_background_tool_schema_includes_completed_resume_and_finish(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    definitions = {definition["function"]["name"]: definition["function"]["description"] for definition in agent.tools.definitions()}

    assert "subagent_start" in definitions
    assert "subagent_delete" in definitions
    assert "subagent_summary" in definitions
    assert "single job" in definitions["subagent_summary"]
    assert "completed" in definitions["subagent_send"]
    assert "same job id, folder, and stored context" in definitions["subagent_send"]
    assert "Destructively delete" in definitions["subagent_delete"]
    assert "stored context" in definitions["subagent_delete"]


def test_old_background_tool_names_are_not_available(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    definitions = {definition["function"]["name"] for definition in agent.tools.definitions()}
    old_names = {
        "background_task_start",
        "background_task_status",
        "background_tasks_list",
        "background_agents_status",
        "background_task_recent_status",
        "background_task_ask",
        "background_task_cancel",
        "background_task_pause",
        "background_task_message",
        "background_task_finish",
        "background_task_events",
    }

    assert not old_names & definitions
    for name in old_names:
        assert not agent.tools.run(name, {}).ok


@pytest.mark.asyncio
async def test_subagent_dashboard_uses_stored_activity_without_flash(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write: ok")
    agent.background_store.record_model_usage(job.id, "claude-haiku-4-5-20251001", 100, 20, 120)
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "Wrote index.html."}])
    agent.client = FakeClient([FakeResponse(FinalMessage("Done: wrote index.html. Now: running browser check."))])  # type: ignore[assignment]

    status = await agent.background_tasks.status_table(limit=5)

    assert "markdown" not in status
    row = status["jobs"][0]
    assert row["job_id"] == job.id
    assert "title" not in row
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["tokens"] == {"prompt": 100, "completion": 20, "total": 120}
    assert row["tool_calls"] == 1
    assert isinstance(row["recent_activity"], list)
    assert row["recent_activity"][0]["time"].endswith(" UTC")
    assert row["recent_activity"][0]["message"] == "write: ok"
    assert "kind" not in row["recent_activity"][0]
    assert agent.client.chat.completions.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_subagent_summary_uses_flash_for_one_worker(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write: ok")
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "Wrote index.html."}])
    agent.client = FakeClient([FakeResponse(FinalMessage("Wrote index.html. " + ("x" * 1200)))])  # type: ignore[assignment]

    status = await agent.background_tasks.recent_status(job.id)

    assert status["job_id"] == job.id
    assert status["summary_source"] == "flash"
    assert status["recent_activity"].startswith("Wrote index.html.")
    assert len(status["recent_activity"]) == 1000
    assert len(agent.client.chat.completions.calls) == 1  # type: ignore[attr-defined]
    system_prompt = agent.client.chat.completions.calls[0]["messages"][0]["content"]  # type: ignore[attr-defined]
    assert "one concise paragraph" in system_prompt
    assert "Keep under 1000 characters" in system_prompt


@pytest.mark.asyncio
async def test_subagent_summary_reuses_cache_until_events_or_context_change(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write: ok")
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "Wrote index.html."}])
    agent.client = FakeClient(
        [
            FakeResponse(FinalMessage("First cached summary.")),
            FakeResponse(FinalMessage("Second summary after change.")),
            FakeResponse(FinalMessage("Third summary after context change.")),
        ]
    )  # type: ignore[assignment]

    first = await agent.background_tasks.recent_status(job.id)
    second = await agent.background_tasks.recent_status(job.id)

    assert first["recent_activity"] == "First cached summary."
    assert second["recent_activity"] == "First cached summary."
    assert len(agent.client.chat.completions.calls) == 1  # type: ignore[attr-defined]

    agent.background_store.add_event(job.id, "tool_call", "read: ok")
    third = await agent.background_tasks.recent_status(job.id)

    assert third["recent_activity"] == "Second summary after change."
    assert len(agent.client.chat.completions.calls) == 2  # type: ignore[attr-defined]

    agent.background_store.save_context(
        job.id,
        [
            {"role": "assistant", "content": "Wrote index.html."},
            {"role": "tool", "content": "Read it back."},
        ],
    )
    fourth = await agent.background_tasks.recent_status(job.id)

    assert fourth["recent_activity"] == "Third summary after context change."
    assert len(agent.client.chat.completions.calls) == 3  # type: ignore[attr-defined]


def test_subagent_dashboard_yaml_is_human_readable() -> None:
    yaml = _render_status_yaml(
        [
            {
                "job_id": "bg_test",
                "model": "claude",
                "status": "running",
                "elapsed": "1m 2s",
                "steps": 3,
                "model_calls": 4,
                "tokens": {"prompt": 1000, "completion": 234, "total": 1234},
                "tool_calls": 2,
                "suspicious_completion": False,
                "flags": ["none"],
                "recent_activity": [
                    {
                        "time": "2026-06-24 03:12:04 UTC",
                        "message": "Wrote file. Now testing.",
                    }
                ],
            }
        ],
        limit=10,
        status="running",
    )

    assert "background_agents:" in yaml
    assert "status_filter: running" in yaml
    assert "- job_id: bg_test" in yaml
    assert "prompt: 1000" in yaml
    assert "completion: 234" in yaml
    assert "total: 1234" in yaml
    assert "recent_activity:" in yaml
    assert '- time: "2026-06-24 03:12:04 UTC"' in yaml
    assert "message: Wrote file. Now testing." in yaml


@pytest.mark.asyncio
async def test_subagent_summary_falls_back_when_flash_fails(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write: ok")

    async def fail_flash(**kwargs):
        raise RuntimeError("flash unavailable")

    agent._flash_completion = fail_flash  # type: ignore[method-assign]

    status = await agent.background_tasks.recent_status(job.id)

    assert status["summary_source"] == "fallback"
    assert "tool_call: write: ok" in status["recent_activity"]


@pytest.mark.asyncio
async def test_subagent_send_reaches_running_worker_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.enqueue_message(job.id, "Change the background from light to dark.")
    agent.client = FakeClient([FakeResponse(FinalMessage("Changed to dark."))])  # type: ignore[assignment]

    response = await agent.run_background_task(job)

    assert response.content == "Changed to dark."
    context = agent.background_store.get_context(job.id)
    assert any("Change the background from light to dark" in str(message.get("content")) for message in context)


@pytest.mark.asyncio
async def test_subagent_ask_uses_no_tool_exact_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "background_jobs/test")
    agent.background_store.save_context(
        job.id,
        [
            {"role": "assistant", "content": "I wrote index.html with background #07111f."},
            {"role": "tool", "content": "Wrote 120 bytes to background_jobs/test/index.html"},
        ],
    )
    agent.client = FakeClient([FakeResponse(FinalMessage("The background is #07111f."))])  # type: ignore[assignment]

    answer = await agent.background_tasks.ask(job.id, "what color is the website background?")

    assert answer == "The background is #07111f."
    call = agent.client.chat.completions.calls[0]  # type: ignore[attr-defined]
    assert call["tool_choice"] == "none"
    assert "background #07111f" in call["messages"][1]["content"]


def test_background_store_marks_active_jobs_interrupted_on_restart(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("long job", "background_jobs/test")
    agent.background_store.start_job(job.id)

    restarted = _agent(tmp_path)

    assert restarted.background_store.get_job(job.id).status == "blocked"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_end_to_end_onboarding_four_parallel_websites_and_midrun_question(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.bind_background_loop()
    completions = []
    completions.append(FakeResponse(FinalMessage("Hi, I'm Pebble Shell. What's your name?")))
    completions.append(FakeResponse(FinalMessage("Nice to meet you, Riley. I recorded your hobbies.")))
    for index in range(4):
        completions.append(
            FakeResponse(
                ToolMessage(
                    "subagent_start",
                        {
                            "prompt": f"Build website {index + 1} in your assigned folder and make the background color color-{index + 1}.",
                            "folder": f"/website-{index + 1}",
                        },
                )
            )
        )
        completions.append(FakeResponse(FinalMessage(f"Started website {index + 1} in the background.")))
    agent.client = FakeClient(completions)  # type: ignore[assignment]

    async def long_website_job(job) -> AgentResponse:
        agent.background_store.save_context(
            job.id,
            [
                {"role": "user", "content": job.prompt},
                {"role": "assistant", "content": f"The website background is {job.prompt.rsplit(' ', 1)[-1].rstrip('.')}."},
            ],
        )
        while not agent.background_store.should_cancel(job.id):
            await asyncio.sleep(0.01)
        return AgentResponse(content=f"{job.id} cancelled after test", steps=1)

    agent.run_background_task = long_website_job  # type: ignore[method-assign]

    assert (await agent.run_user_message("hello")).content.startswith("Hi")
    assert (await agent.run_user_message("I'm Riley. I like chess, synths, and web design.")).content.startswith("Nice")

    start_responses = []
    for index in range(4):
        response = await agent.run_user_message(f"Build complex website {index + 1} in the background.")
        start_responses.append(response.content)
    assert start_responses == [
        "Started website 1 in the background.",
        "Started website 2 in the background.",
        "Started website 3 in the background.",
        "Started website 4 in the background.",
    ]
    await _wait_until(lambda: agent.background_store.count_active() == 4)
    jobs = agent.background_store.list_jobs(limit=4, status="running")
    assert len(jobs) == 4
    assert len({job["folder"] for job in jobs}) == 4

    target = jobs[-1]["id"]
    agent.client.chat.completions.responses.extend(  # type: ignore[attr-defined]
        [
            FakeResponse(
                ToolMessage(
                    "subagent_ask",
                    {"job_id": target, "question": "what color is the website background?"},
                )
            ),
            FakeResponse(FinalMessage("The website background is color-1.")),
            FakeResponse(FinalMessage("That worker says the background is color-1.")),
        ]
    )
    question_response = await agent.run_user_message("what color is the website background in that background job?")
    assert question_response.content == "That worker says the background is color-1."
    ask_calls = [
        call
        for call in agent.client.chat.completions.calls  # type: ignore[attr-defined]
        if call.get("tool_choice") == "none"
    ]
    assert ask_calls
    assert str(target) in ask_calls[-1]["messages"][1]["content"]
    assert "The website background is color-" in ask_calls[-1]["messages"][1]["content"]

    agent.client.chat.completions.responses.append(  # type: ignore[attr-defined]
        FakeResponse(FinalMessage("The website background is color-1.")),
    )
    answer = await agent.background_tasks.ask(target, "what color is the website background?")
    assert "color-" in answer

    for job in jobs:
        agent.tools.run("subagent_cancel", {"job_id": job["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


def _agent(tmp_path: Path) -> CodingAgent:
    return CodingAgent(
        Settings(
            openai_api_key="test-key",
            agent_workspace=tmp_path / "workspace",
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            event_hooks_db_path=tmp_path / "hooks.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            shell_audit_db_path=tmp_path / "exec.sqlite3",
            background_tasks_db_path=tmp_path / "background.sqlite3",
            max_agent_steps=3,
        )
    )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
