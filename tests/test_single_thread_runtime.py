from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path

import pytest

from pebble_shell.agent import CodingAgent
from pebble_shell.config import Settings
from pebble_shell.tools import ToolResult


class FakeMessage:
    content = "ok"
    tool_calls = []

    def model_dump(self, exclude_none: bool = True):
        return {"role": "assistant", "content": self.content}


class FakeChoice:
    message = FakeMessage()


class FakeResponse:
    choices = [FakeChoice()]


class SerializingCompletions:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return FakeResponse()


class FakeChat:
    def __init__(self) -> None:
        self.completions = SerializingCompletions()


class FakeClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


class SequencedCompletions:
    def __init__(self) -> None:
        self.first_call_seen = asyncio.Event()
        self.calls: list[dict[str, object]] = []
        self.responses = [_tool_call_response(), _final_response("combined")]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            self.first_call_seen.set()
        return self.responses.pop(0)


class SequencedChat:
    def __init__(self) -> None:
        self.completions = SequencedCompletions()


class SequencedClient:
    def __init__(self) -> None:
        self.chat = SequencedChat()


@pytest.mark.asyncio
async def test_agent_runs_are_serialized(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    responses = await asyncio.gather(
        agent.run_user_message("first"),
        agent.run_user_message("second"),
    )

    assert [response.content for response in responses] == ["ok", "ok"]
    assert fake_client.chat.completions.calls == 2
    assert fake_client.chat.completions.max_active == 1


@pytest.mark.asyncio
async def test_separate_agent_instances_share_run_lock(tmp_path: Path) -> None:
    first_agent = _agent(tmp_path / "first")
    second_agent = _agent(tmp_path / "second")
    fake_client = FakeClient()
    first_agent.client = fake_client  # type: ignore[assignment]
    second_agent.client = fake_client  # type: ignore[assignment]

    responses = await asyncio.gather(
        first_agent.run_user_message("first"),
        second_agent.run_user_message("second"),
    )

    assert [response.content for response in responses] == ["ok", "ok"]
    assert fake_client.chat.completions.calls == 2
    assert fake_client.chat.completions.max_active == 1


@pytest.mark.asyncio
async def test_same_channel_message_is_injected_before_next_model_step(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = SequencedClient()
    agent.client = fake_client  # type: ignore[assignment]

    def slow_tool(name: str, raw_arguments: object) -> ToolResult:
        time.sleep(0.2)
        return ToolResult(ok=True, output="tool done")

    agent.tools.run = slow_tool  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run_user_message("first task"))
    await fake_client.chat.completions.first_call_seen.wait()

    queued = await agent.enqueue_user_message("second message")
    response = await run_task

    assert queued is True
    assert response.content == "combined"
    assert len(fake_client.chat.completions.calls) == 2
    second_call_messages = fake_client.chat.completions.calls[1]["messages"]
    user_messages = [message["content"] for message in second_call_messages if message["role"] == "user"]
    assert any("first task" in str(content) for content in user_messages)
    assert any("second message" in str(content) for content in user_messages)
    context = agent.memory.get_context("second", recent_limit=10)
    assert [role for role, _ in context.recent_messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in context.recent_raw_messages)
    assert any(message.get("role") == "tool" and "tool done" in str(message.get("content")) for message in context.recent_raw_messages)


def _agent(tmp_path: Path) -> CodingAgent:
    return CodingAgent(
        Settings(
            openai_api_key="test-key",
            agent_workspace=tmp_path / "workspace",
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            self_improvement_db_path=tmp_path / "self.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            shell_audit_db_path=tmp_path / "exec.sqlite3",
            background_tasks_db_path=tmp_path / "background.sqlite3",
        )
    )


def _final_response(content: str):
    class Message:
        tool_calls = []

        def __init__(self, content: str) -> None:
            self.content = content

        def model_dump(self, exclude_none: bool = True):
            return {"role": "assistant", "content": self.content}

    return type("Response", (), {"choices": [type("Choice", (), {"message": Message(content)})()]})()


def _tool_call_response():
    class Function:
        name = "list_files"
        arguments = "{}"

    class ToolCall:
        id = "call-1"
        function = Function()

    class Message:
        content = None
        tool_calls = [ToolCall()]

        def model_dump(self, exclude_none: bool = True):
            return {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}],
            }

    return type("Response", (), {"choices": [type("Choice", (), {"message": Message()})()]})()
