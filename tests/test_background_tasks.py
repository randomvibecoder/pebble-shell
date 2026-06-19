from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from opencode_agent.agent import AgentResponse, CodingAgent
from opencode_agent.config import Settings
from opencode_agent.background_tasks import _render_status_yaml
from opencode_agent.tools import CURRENT_CHANNEL_ID, CURRENT_USER_ID


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

    user_token = CURRENT_USER_ID.set("user-1")
    channel_token = CURRENT_CHANNEL_ID.set("chan")
    try:
        started = [
            agent.tools.run("background_task_start", {"prompt": f"build site {index}", "title": f"site {index}"})
            for index in range(4)
        ]
        fifth = agent.tools.run("background_task_start", {"prompt": "build one more", "title": "overflow"})
    finally:
        CURRENT_CHANNEL_ID.reset(channel_token)
        CURRENT_USER_ID.reset(user_token)

    assert all(result.ok for result in started)
    assert not fifth.ok
    assert "Maximum active background tasks reached: 4" in fifth.output
    jobs = [json.loads(result.output) for result in started]
    assert len({job["id"] for job in jobs}) == 4
    assert all(job["folder"] == f"background_jobs/{job['id']}" for job in jobs)
    assert all("user_id" not in job and "channel_id" not in job for job in jobs)

    for job in jobs:
        agent.tools.run("background_task_cancel", {"job_id": job["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


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
            FakeResponse(ToolMessage("background_task_start", {"prompt": "monitor the test sites", "title": "real monitor"})),
            FakeResponse(FinalMessage("Worker started with the real tool result.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run(
        "Start a background worker with background_task_start. Title: real monitor. Task: monitor the test sites.",
        "user-1",
        "chan",
    )

    assert response.content == "Worker started with the real tool result."
    calls = agent.client.chat.completions.calls  # type: ignore[attr-defined]
    assert len(calls) == 3
    assert "You have not called `background_task_start`" in calls[1]["messages"][-1]["content"]
    jobs = agent.background_store.list_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "real monitor"
    assert jobs[0]["id"] != "bg_fake_123456"

    agent.tools.run("background_task_cancel", {"job_id": jobs[0]["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


@pytest.mark.asyncio
async def test_background_worker_stores_exact_tool_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "page", "user-1", "chan", "background_jobs/test")
    (agent.settings.agent_workspace / job.folder).mkdir(parents=True)
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("write_file", {"path": f"{job.folder}/index.html", "content": "<body>blue</body>"})),
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
    assert "background_task_start" not in tool_names
    system_text = "\n".join(str(message.get("content", "")) for message in first_call["messages"] if message.get("role") == "system")
    assert "Do not use process_start/processes as a way to hand off the assigned job" in system_text
    prompt_text = "\n".join(str(message.get("content", "")) for message in first_call["messages"])
    assert "Original Discord user" not in prompt_text
    assert "Original Discord channel" not in prompt_text
    assert "user-1" not in prompt_text


@pytest.mark.asyncio
async def test_background_job_self_check_complete_records_usage(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "page", "user-1", "chan", "background_jobs/test")
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
    job = agent.background_store.create_job("finish a hard task", "hard", "user-1", "chan", "background_jobs/test")
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
    assert saved.status == "needs_attention"
    assert saved.self_check_retries == 3
    assert "ran out of retries" in saved.attention_summary
    events = agent.background_store.list_events(job.id, limit=20)
    assert any(event["kind"] == "needs_attention" for event in events)


@pytest.mark.asyncio
async def test_background_job_needs_attention_survives_flash_summary_failure(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("finish a hard task", "hard", "user-1", "chan", "background_jobs/test")
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
    assert saved.status == "needs_attention"
    assert "needs foreground attention" in saved.attention_summary
    assert "flash" not in saved.attention_summary.lower()


@pytest.mark.asyncio
async def test_background_job_blocked_remains_messageable_and_resumes(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("do a blocked task", "blocked", "user-1", "chan", "background_jobs/test")
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
    assert resumed.status == "queued"
    assert resumed.self_check_retries == 0


@pytest.mark.asyncio
async def test_background_agents_status_table_uses_flash_activity(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "status page", "user-1", "chan", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write_file: ok")
    agent.background_store.record_model_usage(job.id, "claude-haiku-4-5-20251001", 100, 20, 120)
    agent.background_store.save_context(job.id, [{"role": "assistant", "content": "Wrote index.html."}])
    agent.client = FakeClient([FakeResponse(FinalMessage("Done: wrote index.html. Now: running browser check."))])  # type: ignore[assignment]

    status = await agent.background_tasks.status_table(limit=5)

    assert "markdown" in status
    assert "| job_id | model | status | elapsed | steps | tokens | recent_activity | flags |" in status["markdown"]
    row = status["jobs"][0]
    assert row["job_id"] == job.id
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["tokens"] == "120"
    assert row["tool_calls"] == 1
    assert "Done: wrote index.html" in row["recent_activity"]


def test_background_agents_status_yaml_is_human_readable() -> None:
    yaml = _render_status_yaml(
        [
            {
                "job_id": "bg_test",
                "title": "Test Worker",
                "model": "claude",
                "status": "running",
                "elapsed": "1m 2s",
                "steps": 3,
                "model_calls": 4,
                "tokens": "1234",
                "tool_calls": 2,
                "suspicious_completion": False,
                "flags": ["none"],
                "recent_activity": "Done: wrote file. Now: testing.",
            }
        ],
        limit=10,
        status="running",
    )

    assert "background_agents:" in yaml
    assert "status_filter: running" in yaml
    assert "- job_id: bg_test" in yaml
    assert "recent_activity: >-" in yaml


@pytest.mark.asyncio
async def test_background_agents_status_table_falls_back_when_flash_fails(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a status page", "status page", "user-1", "chan", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.add_event(job.id, "tool_call", "write_file: ok")
    async def fail_flash(**kwargs):
        raise RuntimeError("flash unavailable")

    agent._flash_completion = fail_flash  # type: ignore[method-assign]

    status = await agent.background_tasks.status_table(limit=5)

    row = status["jobs"][0]
    assert "tool_call: write_file: ok" in row["recent_activity"]
    assert "flash" not in row["recent_activity"].lower()


@pytest.mark.asyncio
async def test_background_task_message_reaches_running_worker_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "page", "user-1", "chan", "background_jobs/test")
    agent.background_store.start_job(job.id)
    agent.background_store.enqueue_message(job.id, "Change the background from light to dark.")
    agent.client = FakeClient([FakeResponse(FinalMessage("Changed to dark."))])  # type: ignore[assignment]

    response = await agent.run_background_task(job)

    assert response.content == "Changed to dark."
    context = agent.background_store.get_context(job.id)
    assert any("Change the background from light to dark" in str(message.get("content")) for message in context)


@pytest.mark.asyncio
async def test_background_task_ask_uses_no_tool_exact_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    job = agent.background_store.create_job("make a page", "page", "user-1", "chan", "background_jobs/test")
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
    job = agent.background_store.create_job("long job", "long", "user-1", "chan", "background_jobs/test")
    agent.background_store.start_job(job.id)

    restarted = _agent(tmp_path)

    assert restarted.background_store.get_job(job.id).status == "interrupted"  # type: ignore[union-attr]


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
                    "background_task_start",
                    {
                        "title": f"website {index + 1}",
                        "prompt": f"Build website {index + 1} in your assigned folder and make the background color color-{index + 1}.",
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

    assert (await agent.run("hello", "user-1", "chan")).content.startswith("Hi")
    assert (await agent.run("I'm Riley. I like chess, synths, and web design.", "user-1", "chan")).content.startswith("Nice")

    start_responses = []
    for index in range(4):
        response = await agent.run(f"Build complex website {index + 1} in the background.", "user-1", "chan")
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
                    "background_task_ask",
                    {"job_id": target, "question": "what color is the website background?"},
                )
            ),
            FakeResponse(FinalMessage("The website background is color-1.")),
            FakeResponse(FinalMessage("That worker says the background is color-1.")),
        ]
    )
    question_response = await agent.run("what color is the website background in that background job?", "user-1", "chan")
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
        agent.tools.run("background_task_cancel", {"job_id": job["id"]})
    await _wait_until(lambda: agent.background_store.count_active() == 0)


def _agent(tmp_path: Path) -> CodingAgent:
    return CodingAgent(
        Settings(
            openai_api_key="test-key",
            agent_workspace=tmp_path / "workspace",
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            self_improvement_db_path=tmp_path / "self.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            exec_audit_db_path=tmp_path / "exec.sqlite3",
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
