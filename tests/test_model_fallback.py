from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pebble_shell.agent import CodingAgent
from pebble_shell.config import Settings


class ProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeUsage:
    prompt_tokens = 11
    completion_tokens = 3
    total_tokens = 14
    reasoning_tokens = 2
    prompt_tokens_details = {"cached_tokens": 5, "image_tokens": 7}
    completion_tokens_details = {"reasoning_tokens": 2}


class FakeResponse:
    usage = FakeUsage()

    def __init__(self, model: str) -> None:
        self.model = model


class ScriptedCompletions:
    def __init__(self, script: dict[str, list[object]] | None = None) -> None:
        self.script = script or {}
        self.models: list[str] = []

    async def create(self, model: str, **_kwargs: Any):
        self.models.append(model)
        responses = self.script.get(model, [])
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return FakeResponse(model)


class FakeClient:
    def __init__(self, script: dict[str, list[object]] | None = None) -> None:
        self.chat = type("Chat", (), {"completions": ScriptedCompletions(script)})()


def test_candidate_models_deduplicate_runtime_and_fallbacks(tmp_path: Path) -> None:
    agent = _agent(tmp_path, openai_model="primary", openai_fallback_models="secondary, primary, tertiary")

    assert agent.candidate_models() == ["primary", "secondary", "tertiary"]


@pytest.mark.asyncio
async def test_chat_completion_retries_primary_then_uses_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(tmp_path, openai_model="broken-model", openai_fallback_models="working-model")
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("pebble_shell.agent.asyncio.sleep", fake_sleep)
    fake_client = FakeClient(
        {
            "broken-model": [
                ProviderError("temporarily unavailable", 503),
                ProviderError("temporarily unavailable", 503),
                ProviderError("temporarily unavailable", 503),
            ],
            "working-model": [FakeResponse("working-model")],
        }
    )
    agent.client = fake_client  # type: ignore[assignment]

    result = await agent._chat_completion(messages=[{"role": "user", "content": "hi"}], source="user")

    assert result.model == "working-model"
    assert fake_client.chat.completions.models == ["broken-model", "broken-model", "broken-model", "working-model"]
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_chat_completion_skips_same_model_retries_for_permanent_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path, openai_model="bad-request-model", openai_fallback_models="working-model")
    fake_client = FakeClient({"bad-request-model": [ProviderError("image_input_not_supported", 400)]})
    agent.client = fake_client  # type: ignore[assignment]

    result = await agent._chat_completion(messages=[{"role": "user", "content": "hi"}], source="user")

    assert result.model == "working-model"
    assert fake_client.chat.completions.models == ["bad-request-model", "working-model"]


@pytest.mark.asyncio
async def test_chat_completion_records_usage_and_errors(tmp_path: Path) -> None:
    agent = _agent(tmp_path, openai_model="bad-request-model", openai_fallback_models="working-model")
    fake_client = FakeClient({"bad-request-model": [ProviderError("image_input_not_supported", 400)]})
    agent.client = fake_client  # type: ignore[assignment]

    await agent._chat_completion(messages=[{"role": "user", "content": "hi"}], source="user")

    with sqlite3.connect(tmp_path / "memory.sqlite3") as conn:
        rows = conn.execute(
            "select source, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens, image_tokens, error from model_calls order by id"
        ).fetchall()
    assert rows[0][0] == "user"
    assert rows[0][1] == "bad-request-model"
    assert "image_input_not_supported" in rows[0][8]
    assert rows[1] == ("user", "working-model", 11, 3, 14, 5, 2, 7, "")


@pytest.mark.asyncio
async def test_configured_input_cap_raises_context_length_before_provider_call(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        openai_model="small-cap",
        openai_fallback_models="",
        openai_model_input_token_limits="small-cap=5",
    )
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="context_length_exceeded"):
        await agent._chat_completion(messages=[{"role": "user", "content": "x" * 200}], source="user")

    assert fake_client.chat.completions.models == []


def _agent(tmp_path: Path, **overrides: object) -> CodingAgent:
    settings = Settings(
        openai_api_key="test-key",
        agent_workspace=tmp_path / "workspace",
        memory_db_path=tmp_path / "memory.sqlite3",
        runtime_config_db_path=tmp_path / "runtime.sqlite3",
        event_hooks_db_path=tmp_path / "hooks.sqlite3",
        cron_db_path=tmp_path / "cron.sqlite3",
        shell_audit_db_path=tmp_path / "exec.sqlite3",
        background_tasks_db_path=tmp_path / "background.sqlite3",
        **overrides,
    )
    return CodingAgent(settings)
