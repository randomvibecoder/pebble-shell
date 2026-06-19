from pathlib import Path

import pytest

import pebble_shell.__main__ as main_module
from pebble_shell.__main__ import _send_initial_dm_once
from pebble_shell.agent import CodingAgent
from pebble_shell.config import Settings


@pytest.mark.asyncio
async def test_initial_dm_sends_once_and_records_context(tmp_path: Path, monkeypatch) -> None:
    sent = []

    def fake_send_dm(bot_token: str, user_id: str, content: str):
        sent.append((bot_token, user_id, content))
        return [{"channel_id": "dm-channel"}]

    monkeypatch.setattr(main_module, "send_dm", fake_send_dm)
    settings = _settings(tmp_path, initial_dm_user_id="111111111111111111")
    agent = CodingAgent(settings)

    await _send_initial_dm_once(settings, agent)
    await _send_initial_dm_once(settings, agent)

    assert sent == [("bot-token", "111111111111111111", "Hi, I'm Pebble Shell. What's your name?")]
    context = agent.memory.get_context("name", recent_limit=5)
    assert context.recent_messages == [("assistant", "Hi, I'm Pebble Shell. What's your name?")]
    assert agent.memory.get_contact("initial_dm_sent:111111111111111111:Hi, I'm Pebble Shell. What's your name?") == "sent"


@pytest.mark.asyncio
async def test_initial_dm_skips_without_target(tmp_path: Path, monkeypatch) -> None:
    called = False

    def fake_send_dm(bot_token: str, user_id: str, content: str):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main_module, "send_dm", fake_send_dm)
    settings = _settings(tmp_path)
    agent = CodingAgent(settings)

    await _send_initial_dm_once(settings, agent)

    assert called is False


def _settings(tmp_path: Path, initial_dm_user_id: str = "") -> Settings:
    return Settings(
        openai_api_key="test-key",
        discord_bot_token="bot-token",
        initial_dm_user_id=initial_dm_user_id,
        agent_workspace=tmp_path / "workspace",
        memory_db_path=tmp_path / "memory.sqlite3",
        runtime_config_db_path=tmp_path / "runtime.sqlite3",
        self_improvement_db_path=tmp_path / "self.sqlite3",
        cron_db_path=tmp_path / "cron.sqlite3",
        shell_audit_db_path=tmp_path / "exec.sqlite3",
        background_tasks_db_path=tmp_path / "background.sqlite3",
    )
