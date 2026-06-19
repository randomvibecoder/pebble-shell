from __future__ import annotations

from pathlib import Path

import pytest

from pebble_shell.agent import CodingAgent
from pebble_shell.config import Settings


class FakeCompletions:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def create(self, model: str, **_kwargs):
        self.models.append(model)
        if model == "broken-model":
            raise RuntimeError("model unavailable")
        return {"model": model}


class FakeChat:
    def __init__(self) -> None:
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


def test_candidate_models_deduplicate_runtime_and_fallbacks(tmp_path: Path) -> None:
    agent = _agent(tmp_path, openai_model="primary", openai_fallback_models="secondary, primary, tertiary")

    assert agent.candidate_models() == ["primary", "secondary", "tertiary"]


@pytest.mark.asyncio
async def test_chat_completion_uses_fallback_model(tmp_path: Path) -> None:
    agent = _agent(tmp_path, openai_model="broken-model", openai_fallback_models="working-model")
    fake_client = FakeClient()
    agent.client = fake_client  # type: ignore[assignment]

    result = await agent._chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result == {"model": "working-model"}
    assert fake_client.chat.completions.models == ["broken-model", "working-model"]


def _agent(tmp_path: Path, **overrides) -> CodingAgent:
    settings = Settings(
        openai_api_key="test-key",
        agent_workspace=tmp_path / "workspace",
        memory_db_path=tmp_path / "memory.sqlite3",
        runtime_config_db_path=tmp_path / "runtime.sqlite3",
        self_improvement_db_path=tmp_path / "self.sqlite3",
        cron_db_path=tmp_path / "cron.sqlite3",
        shell_audit_db_path=tmp_path / "exec.sqlite3",
        background_tasks_db_path=tmp_path / "background.sqlite3",
        **overrides,
    )
    return CodingAgent(settings)
