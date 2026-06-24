from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from pebble_shell.agent import ImageInput
from pebble_shell.agent import SYSTEM_PROMPT, CodingAgent
from pebble_shell.config import Settings


class FakeMessage:
    content = "ok"
    tool_calls = []

    def model_dump(self, exclude_none: bool = True):
        return {"role": "assistant", "content": self.content}


class FakeChoice:
    message = FakeMessage()


class FakeResponse:
    choices = [FakeChoice()]


class CapturingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return FakeResponse()


class FakeChat:
    def __init__(self) -> None:
        self.completions = CapturingCompletions()


class FakeClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


class SequencedCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [_tool_call_response(), _final_response("done")]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SequencedClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": SequencedCompletions()})()


class MaxStepsCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [_tool_call_response(), _tool_call_response(), _final_response("tool budget exhausted summary")]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


class MaxStepsClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": MaxStepsCompletions()})()


class EmptyThenRecoveryCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [_final_response(""), _final_response("Recovered useful reply")]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


class EmptyThenRecoveryClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": EmptyThenRecoveryCompletions()})()


class EmptyThenEmptyCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [_final_response(""), _final_response("   ")]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


class EmptyThenEmptyClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": EmptyThenEmptyCompletions()})()


@pytest.mark.asyncio
async def test_initial_onboarding_instruction_comes_from_user_context_file(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("hello")

    system_messages = [message["content"] for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "system"]
    assert not any("First-contact onboarding:" in str(message) for message in system_messages)
    assert any(str(message).startswith("context/USER.md:") and "Initial onboarding:" in str(message) for message in system_messages)
    assert any("hobbies" in str(message) for message in system_messages)


@pytest.mark.asyncio
async def test_core_system_prompt_describes_foreground_background_runtime(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("hello")

    core_prompt = fake_client.chat.completions.calls[0]["messages"][0]["content"]
    assert "foreground orchestrator and have up to four long-running background workers" in core_prompt
    assert "subagent_start" in core_prompt
    assert "The user does not need to explicitly ask for a subagent" in core_prompt
    assert "Workers may install packages, CLIs, browsers, dependencies" in core_prompt
    assert "After starting a subagent, write its job id, folder, and task to context/MEMORY.md" in core_prompt
    assert "running/paused/blocked/completed worker" in core_prompt
    assert "Use subagent_delete only for destructive cleanup" in core_prompt
    assert "exec_command for shell commands" in core_prompt
    assert "write_stdin(session_id" in core_prompt
    assert "context/MEMORY.md" in core_prompt
    assert "record_memory" not in core_prompt
    assert "heartbeat_set" in core_prompt
    assert "set_runtime_config" not in core_prompt
    assert "context/USER.md" in core_prompt
    assert "context/SOUL.md" in core_prompt
    assert "context/HEARTBEAT.md" in core_prompt
    assert "Docker" not in core_prompt
    assert "container" not in core_prompt
    assert "V0.0.1" not in core_prompt


@pytest.mark.asyncio
async def test_generated_onboarding_prompt_is_not_injected_when_memory_exists(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.memory.add_message("user", "previous hello")
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("hello again")

    system_messages = [message["content"] for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "system"]
    assert not any("First-contact onboarding:" in str(message) for message in system_messages)


@pytest.mark.asyncio
async def test_recent_memory_is_sent_as_native_roles_not_system_transcript(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.memory.add_message("assistant", "what do you need?\nuser: install ffmpeg")
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("why did you complete my turn?")

    messages = fake_client.chat.completions.calls[0]["messages"]
    assert {"role": "assistant", "content": "what do you need?\nuser: install ffmpeg"} in messages
    system_text = "\n".join(str(message["content"]) for message in messages if message["role"] == "system")
    assert "Recent exact messages" not in system_text
    assert "\nassistant: what do you need?" not in system_text


def test_dump_next_heartbeat_context_writes_messages_jsonl(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    (agent.settings.agent_workspace / "context" / "HEARTBEAT.md").write_text("SECRET HEARTBEAT BODY", encoding="utf-8")
    agent.memory.add_message("user", "hello")
    agent.memory.add_message("assistant", "hi")

    path = agent.dump_next_heartbeat_context()

    assert path.name.startswith("heartbeat_")
    messages = [__import__("json").loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert messages
    assert all(set(message) <= {"role", "content", "tool_calls", "tool_call_id", "name"} for message in messages)
    assert {"role": "user", "content": "hello"} in messages
    assert {"role": "assistant", "content": "hi"} in messages
    assert messages[-1]["role"] == "user"
    assert "read" in messages[-1]["content"]
    assert "context/HEARTBEAT.md" in messages[-1]["content"]
    assert "SECRET HEARTBEAT BODY" not in str(messages)


def test_heartbeat_prompt_includes_current_utc_time(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    prompt = agent._heartbeat_prompt()

    assert re.match(
        r"^This is a heartbeat turn\. The time is \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\. "
        r"First call read with path context/HEARTBEAT\.md\.",
        prompt,
    )


def test_system_prompt_defines_heartbeat() -> None:
    assert "A heartbeat is an automatic periodic internal turn started by the harness" in SYSTEM_PROMPT
    assert "not a direct user message" in SYSTEM_PROMPT
    assert "The time is YYYY-MM-DD HH:MM:SS UTC" in SYSTEM_PROMPT
    assert "first call read with path context/HEARTBEAT.md" in SYSTEM_PROMPT
    assert "HEARTBEAT_OK means there is no user-visible update" in SYSTEM_PROMPT
    assert "The harness suppresses HEARTBEAT_OK" in SYSTEM_PROMPT


def test_system_prompt_documents_webhook_token_file() -> None:
    assert "/workspace/.pebble_shell/secrets/api_auth_token" in SYSTEM_PROMPT
    assert "read the bearer token at runtime" in SYSTEM_PROMPT
    assert "Do not copy the token into source code" in SYSTEM_PROMPT


def test_system_prompt_documents_webhook_context_and_send_msg() -> None:
    assert "Webhooks are internal localhost event ingress, not chat/completion APIs" in SYSTEM_PROMPT
    assert "the caller must not expect your final model answer in the HTTP response" in SYSTEM_PROMPT
    assert "A webhook does not create a separate conversation" in SYSTEM_PROMPT
    assert "build an adapter you control" in SYSTEM_PROMPT


def test_api_auth_token_file_is_seeded_from_settings(tmp_path: Path) -> None:
    agent = CodingAgent(
        Settings(
            openai_api_key="test-key",
            api_auth_token="secret-token",
            agent_workspace=tmp_path / "workspace",
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            event_hooks_db_path=tmp_path / "hooks.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            shell_audit_db_path=tmp_path / "exec.sqlite3",
            background_tasks_db_path=tmp_path / "background.sqlite3",
        )
    )

    token_path = agent.settings.agent_workspace / ".pebble_shell" / "secrets" / "api_auth_token"
    assert token_path.read_text(encoding="utf-8") == "secret-token\n"
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_api_auth_token_file_is_removed_when_auth_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    token_path = workspace / ".pebble_shell" / "secrets" / "api_auth_token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("old-token\n", encoding="utf-8")

    CodingAgent(
        Settings(
            openai_api_key="test-key",
            api_auth_token="",
            agent_workspace=workspace,
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            event_hooks_db_path=tmp_path / "hooks.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            shell_audit_db_path=tmp_path / "exec.sqlite3",
            background_tasks_db_path=tmp_path / "background.sqlite3",
        )
    )

    assert not token_path.exists()


@pytest.mark.asyncio
async def test_memory_md_is_cached_until_compaction_or_restart(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    memory_path = agent.settings.agent_workspace / "context" / "MEMORY.md"
    memory_path.write_text("initial memory", encoding="utf-8")
    restarted = _agent(tmp_path)
    fake_client = FakeClient()
    restarted.client = fake_client  # type: ignore[assignment]

    await restarted.run_user_message("hello")
    memory_path.write_text("changed memory", encoding="utf-8")
    await restarted.run_user_message("hello again")

    first_system_text = "\n".join(str(message["content"]) for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "system")
    second_system_text = "\n".join(str(message["content"]) for message in fake_client.chat.completions.calls[1]["messages"] if message["role"] == "system")
    assert "Cached context/MEMORY.md snapshot:\ninitial memory" in first_system_text
    assert "Cached context/MEMORY.md snapshot:\ninitial memory" in second_system_text
    assert "changed memory" not in second_system_text


@pytest.mark.asyncio
async def test_context_files_are_cached_until_compaction_or_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "context").mkdir(parents=True)
    tools_path = workspace / "context" / "TOOLS.md"
    tools_path.write_text("initial tools", encoding="utf-8")
    agent = _agent(tmp_path)
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("hello")
    tools_path.write_text("changed tools", encoding="utf-8")
    await agent.run_user_message("hello again")

    first_system_text = "\n".join(str(message["content"]) for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "system")
    second_system_text = "\n".join(str(message["content"]) for message in fake_client.chat.completions.calls[1]["messages"] if message["role"] == "system")
    assert "context/TOOLS.md:\ninitial tools" in first_system_text
    assert "context/TOOLS.md:\ninitial tools" in second_system_text
    assert "changed tools" not in second_system_text


@pytest.mark.asyncio
async def test_memory_md_refreshes_after_context_compaction(tmp_path: Path) -> None:
    from openai import BadRequestError
    import httpx

    agent = _agent(tmp_path)
    memory_path = agent.settings.agent_workspace / "context" / "MEMORY.md"
    memory_path.write_text("initial memory", encoding="utf-8")
    restarted = _agent(tmp_path)
    restarted.memory.add_message("user", "old one")
    restarted.memory.add_message("assistant", "old two")
    restarted.memory.add_message("user", "old three")
    memory_path.write_text("changed memory", encoding="utf-8")
    error = BadRequestError("context length exceeded", response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")), body={})
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        error,
        _final_response("Detailed summary of older context."),
        _final_response("done after compaction"),
    ]
    restarted.client = fake_client  # type: ignore[assignment]

    response = await restarted.run_user_message("force compaction")

    assert response.content == "done after compaction"
    retry_messages = fake_client.chat.completions.calls[-1]["messages"]
    system_text = "\n".join(str(message["content"]) for message in retry_messages if message["role"] == "system")
    assert "Cached context/MEMORY.md snapshot:\nchanged memory" in system_text
    assert "initial memory" not in system_text


@pytest.mark.asyncio
async def test_context_files_refresh_after_context_compaction(tmp_path: Path) -> None:
    from openai import BadRequestError
    import httpx

    workspace = tmp_path / "workspace"
    (workspace / "context").mkdir(parents=True)
    tools_path = workspace / "context" / "TOOLS.md"
    tools_path.write_text("initial tools", encoding="utf-8")
    agent = _agent(tmp_path)
    agent.memory.add_message("user", "old one")
    agent.memory.add_message("assistant", "old two")
    agent.memory.add_message("user", "old three")
    tools_path.write_text("changed tools", encoding="utf-8")
    error = BadRequestError("context length exceeded", response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")), body={})
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        error,
        _final_response("Detailed summary of older context."),
        _final_response("done after compaction"),
    ]
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_user_message("force compaction")

    assert response.content == "done after compaction"
    retry_messages = fake_client.chat.completions.calls[-1]["messages"]
    system_text = "\n".join(str(message["content"]) for message in retry_messages if message["role"] == "system")
    assert "context/TOOLS.md:\nchanged tools" in system_text
    assert "context/TOOLS.md:\ninitial tools" not in system_text


@pytest.mark.asyncio
async def test_image_inputs_are_sent_as_multimodal_content_and_stored_as_references(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message(
        "what is in this image?",
        images=[ImageInput(url="https://cdn.discordapp.com/attachments/1/cat.png", content_type="image/png", filename="cat.png")],
    )

    user_message = next(message for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "user")
    assert fake_client.chat.completions.calls[0]["model"] == "xiaomi/mimo-v2.5:thinking"
    assert user_message["role"] == "user"
    assert user_message["content"][0]["type"] == "text"
    assert user_message["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn.discordapp.com/attachments/1/cat.png"},
    }
    context = agent.memory.get_context("image", recent_limit=5)
    assert "Attached images:" in context.recent_messages[0][1]
    assert "https://cdn.discordapp.com/attachments/1/cat.png" in context.recent_messages[0][1]
    assert context.recent_raw_messages[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn.discordapp.com/attachments/1/cat.png"},
    }


def test_user_prompt_does_not_include_discord_identity_wrapper(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    payload = agent.build_chat_completion_payload("hello")
    user_content = payload["messages"][-1]["content"]
    system_content = payload["messages"][0]["content"]

    assert user_content == "hello"
    assert "111111111111111111" not in str(payload["messages"])
    assert "Primary Discord user says" not in str(payload["messages"])
    assert "Transport routing is handled by the harness" in str(system_content)


@pytest.mark.asyncio
async def test_image_url_is_preserved_across_tool_iterations_and_future_turns(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        _tool_call_response(),
        _final_response("done inspecting"),
        _final_response("prior image is still visible"),
    ]
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message(
        "inspect this image then list files",
        images=[ImageInput(url="https://cdn.discordapp.com/attachments/1/cat.png", content_type="image/png", filename="cat.png")],
    )

    first_user = next(message for message in fake_client.chat.completions.calls[0]["messages"] if message["role"] == "user")
    second_user = next(message for message in fake_client.chat.completions.calls[1]["messages"] if message["role"] == "user")
    assert isinstance(first_user["content"], list)
    assert first_user["content"][1]["type"] == "image_url"
    assert isinstance(second_user["content"], list)
    assert second_user["content"][1]["type"] == "image_url"

    response = await agent.run_user_message("can you still see the previous image?")

    assert response.content == "prior image is still visible"
    third_call_messages = fake_client.chat.completions.calls[2]["messages"]
    prior_image_message = next(
        message
        for message in third_call_messages
        if message["role"] == "user"
        and isinstance(message["content"], list)
        and message["content"][0]["type"] == "text"
        and "inspect this image" in message["content"][0]["text"]
    )
    assert prior_image_message["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_max_tool_steps_gets_final_no_tool_turn(tmp_path: Path) -> None:
    agent = CodingAgent(
        Settings(
            openai_api_key="test-key",
            agent_workspace=tmp_path / "workspace",
            memory_db_path=tmp_path / "memory.sqlite3",
            runtime_config_db_path=tmp_path / "runtime.sqlite3",
            event_hooks_db_path=tmp_path / "hooks.sqlite3",
            cron_db_path=tmp_path / "cron.sqlite3",
            shell_audit_db_path=tmp_path / "exec.sqlite3",
            background_tasks_db_path=tmp_path / "background.sqlite3",
            max_agent_steps=2,
        )
    )
    fake_client = MaxStepsClient()
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_user_message("keep checking files")

    assert response.content == "tool budget exhausted summary"
    assert response.steps == 3
    calls = fake_client.chat.completions.calls
    assert len(calls) == 3
    assert calls[-1]["tool_choice"] == "none"
    final_system_text = "\n".join(str(message["content"]) for message in calls[-1]["messages"] if message["role"] == "system")
    assert "maximum tool-turn limit" in final_system_text


@pytest.mark.asyncio
async def test_empty_final_gets_no_tool_recovery_turn(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = EmptyThenRecoveryClient()
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_user_message("hello?")

    assert response.content == "Recovered useful reply"
    calls = fake_client.chat.completions.calls
    assert len(calls) == 2
    assert calls[-1]["tool_choice"] == "none"
    final_system_text = "\n".join(str(message["content"]) for message in calls[-1]["messages"] if message["role"] == "system")
    assert "previous assistant response was empty" in final_system_text
    context = agent.memory.get_context("hello", recent_limit=5)
    assert ("assistant", "Recovered useful reply") in context.recent_messages
    assert ("assistant", "") not in context.recent_messages


@pytest.mark.asyncio
async def test_empty_final_recovery_falls_back_to_visible_message(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fake_client = EmptyThenEmptyClient()
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_user_message("hello?")

    assert response.content.startswith("I got an empty model response")
    assert len(fake_client.chat.completions.calls) == 2
    context = agent.memory.get_context("hello", recent_limit=5)
    assert any(role == "assistant" and content.startswith("I got an empty model response") for role, content in context.recent_messages)


@pytest.mark.asyncio
async def test_heartbeat_forces_read_before_final_answer(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    (agent.settings.agent_workspace / "context" / "HEARTBEAT.md").write_text("Check the dashboard.", encoding="utf-8")
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        _final_response("HEARTBEAT_OK"),
        _tool_call_response_named("read", {"path": "context/HEARTBEAT.md"}),
        _final_response("HEARTBEAT_OK"),
    ]
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_heartbeat()

    assert response.content == "HEARTBEAT_OK"
    assert response.should_notify is False
    calls = fake_client.chat.completions.calls
    assert len(calls) == 3
    assert any(
        message["role"] == "system" and "must inspect context/HEARTBEAT.md through the read tool" in message["content"]
        for message in calls[1]["messages"]
    )
    assert any(
        message["role"] == "tool" and "Check the dashboard." in message["content"]
        for message in calls[2]["messages"]
    )


@pytest.mark.asyncio
async def test_send_msg_sends_progress_and_final_response_still_uses_normal_reply(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    delivered: list[str] = []
    agent.tools.text_sender = lambda text: delivered.append(text) or "sent progress"
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        _tool_call_response_named("send_msg", {"msg": "I started the verification run."}),
        _final_response("Final result is ready."),
    ]
    agent.client = fake_client  # type: ignore[assignment]

    response = await agent.run_user_message("do a long task")

    assert delivered == ["I started the verification run."]
    assert response.content == "Final result is ready."
    context = agent.memory.get_context("long task", recent_limit=5)
    assert ("assistant", "Final result is ready.") in context.recent_messages
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in context.recent_raw_messages)
    assert any(
        message.get("role") == "tool" and "sent progress" in str(message.get("content"))
        for message in context.recent_raw_messages
    )


@pytest.mark.asyncio
async def test_foreground_tool_calls_are_preserved_in_next_turn_context(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.tools.text_sender = lambda text: "sent progress"
    fake_client = SequencedClient()
    fake_client.chat.completions.responses = [
        _tool_call_response_named("send_msg", {"msg": "I started the verification run."}),
        _final_response("Final result is ready."),
        _final_response("The prior tool result is visible."),
    ]
    agent.client = fake_client  # type: ignore[assignment]

    await agent.run_user_message("do a long task")
    response = await agent.run_user_message("what was the prior tool result?")

    assert response.content == "The prior tool result is visible."
    second_turn_messages = fake_client.chat.completions.calls[2]["messages"]
    roles = [message["role"] for message in second_turn_messages]
    assert "tool" in roles
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in second_turn_messages)
    assert any(message.get("role") == "tool" and "sent progress" in str(message.get("content")) for message in second_turn_messages)


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
    return _tool_call_response_named("list_files", {})


def _tool_call_response_named(name: str, arguments: dict[str, object]):
    class Function:
        pass

    class ToolCall:
        id = "call-1"

    function = Function()
    function.name = name
    function.arguments = __import__("json").dumps(arguments)
    call = ToolCall()
    call.function = function

    class Message:
        content = None
        tool_calls = [call]

        def model_dump(self, exclude_none: bool = True):
            return {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": function.arguments}}],
            }

    return type("Response", (), {"choices": [type("Choice", (), {"message": Message()})()]})()
