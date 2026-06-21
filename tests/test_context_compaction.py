from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from pebble_shell.agent import CodingAgent
from pebble_shell.agent import _split_history_indexes_by_token_weight
from pebble_shell.config import Settings


class FakeChoice:
    def __init__(self, message: object) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, message: object, prompt_tokens: int | None = None, completion_tokens: int | None = None) -> None:
        self.choices = [FakeChoice(message)]
        if prompt_tokens is not None or completion_tokens is not None:
            self.usage = type(
                "Usage",
                (),
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                },
            )()


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
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("No fake completion response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.chat = type("Chat", (), {"completions": SequencedCompletions(responses)})()


@pytest.mark.asyncio
async def test_foreground_summarizes_only_after_context_length_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    delivered: list[str] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    agent.set_deliver(deliver)
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("list_files", {"path": "."})),
            RuntimeError("context_length_exceeded: prompt is too long"),
            FakeResponse(FinalMessage("Detailed summary of the compacted foreground conversation and tool result.")),
            FakeResponse(FinalMessage("Done after compaction.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run_user_message("inspect the workspace")

    assert response.content == "Done after compaction."
    assert len(delivered) == 1
    _assert_summary_notice(delivered[0])
    calls = agent.client.chat.completions.calls  # type: ignore[attr-defined]
    assert calls[2]["tool_choice"] == "none"
    summary_messages = calls[2]["messages"]
    assert "You are Pebble Shell" in summary_messages[0]["content"]
    assert any(str(message.get("content", "")).startswith("context/USER.md:") for message in summary_messages)
    assert any(message.get("role") == "user" and message.get("content") == "inspect the workspace" for message in summary_messages)
    assert "Return only the updated summary" in summary_messages[-1]["content"]
    retry_messages = calls[3]["messages"]
    assert any("Active foreground compacted summary" in str(message.get("content")) for message in retry_messages)
    assert any(message.get("role") == "tool" for message in retry_messages)
    assert agent.memory.get_context("", recent_limit=10).summary == "Detailed summary of the compacted foreground conversation and tool result."


@pytest.mark.asyncio
async def test_background_worker_persists_summary_and_notifies_after_context_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    delivered: list[str] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    agent.set_deliver(deliver)
    job = agent.background_store.create_job("make a page", "page", "background_jobs/test")
    (agent.settings.agent_workspace / job.folder).mkdir(parents=True)
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("write", {"path": "index.html", "content": "<body>blue</body>"})),
            RuntimeError("maximum context length exceeded"),
            FakeResponse(FinalMessage("Detailed summary of the compacted background job, files, commands, and tool result.")),
            FakeResponse(FinalMessage("Background job done after compaction.")),
        ]
    )  # type: ignore[assignment]

    response = await agent.run_background_task(job)

    assert response.content == "Background job done after compaction."
    assert len(delivered) == 1
    _assert_summary_notice(delivered[0])
    assert "compacted background job" in agent.background_store.get_summary(job.id)
    context = agent.background_store.get_context(job.id)
    assert any("Background job compacted summary" in str(message.get("content")) for message in context)
    assert any(event["kind"] == "summary" for event in agent.background_store.list_events(job.id, limit=10))


@pytest.mark.asyncio
async def test_compaction_notice_uses_provider_summary_usage_when_available(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    delivered: list[str] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    agent.set_deliver(deliver)
    agent.client = FakeClient(
        [
            FakeResponse(ToolMessage("list_files", {"path": "."})),
            RuntimeError("context_length_exceeded: prompt is too long"),
            FakeResponse(
                FinalMessage("Detailed summary of the compacted foreground conversation and tool result."),
                prompt_tokens=1234,
                completion_tokens=234,
            ),
            FakeResponse(FinalMessage("Done after compaction.")),
        ]
    )  # type: ignore[assignment]

    await agent.run_user_message("inspect the workspace")

    assert delivered == ["[compacted 1234 tokens to 234 tokens]"]


def test_persisted_foreground_summary_is_included_in_future_payloads(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.memory.add_message("user", "older user detail")
    agent.memory.add_message("assistant", "older assistant detail")
    agent.memory.add_message("user", "recent user detail")
    agent.memory.upsert_summary("Durable compacted facts about older user work.", 2)

    payload = agent.build_chat_completion_payload("what do you remember?")
    messages = payload["messages"]

    assert any(
        message.get("role") == "system"
        and message.get("content") == "Active foreground compacted summary:\nDurable compacted facts about older user work."
        for message in messages
    )
    assert not any(message.get("content") == "older user detail" for message in messages)
    assert not any(message.get("content") == "older assistant detail" for message in messages)
    assert any(message.get("role") == "user" and message.get("content") == "recent user detail" for message in messages)


def test_dump_next_heartbeat_context_includes_persisted_summary(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.memory.add_message("user", "older user detail")
    agent.memory.add_message("assistant", "older assistant detail")
    agent.memory.add_message("user", "recent user detail")
    agent.memory.upsert_summary("Durable compacted facts for heartbeat dump.", 2)

    path = agent.dump_next_heartbeat_context()
    dumped = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert any(
        message.get("role") == "system"
        and message.get("content") == "Active foreground compacted summary:\nDurable compacted facts for heartbeat dump."
        for message in dumped
    )
    assert dumped[-1]["role"] == "user"
    assert "This is a heartbeat turn." in str(dumped[-1]["content"])


def test_compaction_split_is_weighted_by_message_tokens() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "tiny"},
        {"role": "assistant", "content": "x" * 2000},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent"},
        {"role": "tool", "tool_call_id": "call-1", "content": "recent"},
    ]

    summarized, recent = _split_history_indexes_by_token_weight(list(range(1, len(messages))), messages, min_recent=2)

    assert summarized == [1, 2]
    assert recent == [3, 4, 5, 6, 7]


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
            max_agent_steps=3,
        )
    )


def _assert_summary_notice(text: str) -> None:
    assert text == "[compacted]"
