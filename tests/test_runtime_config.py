from pathlib import Path

import pytest

from pebble_shell.runtime_config import RuntimeConfigStore
from pebble_shell.tools import WorkspaceTools


def test_runtime_config_store_persists_allowed_keys(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    store.set("openai_model", "claude-haiku-4-5-20251001")
    store.set("heartbeat_every_seconds", "3600")

    reopened = RuntimeConfigStore(tmp_path / "runtime.sqlite3")

    assert reopened.get("openai_model") == "claude-haiku-4-5-20251001"
    assert reopened.get("heartbeat_every_seconds") == "3600"


def test_runtime_config_rejects_unknown_keys(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime.sqlite3")

    with pytest.raises(ValueError):
        store.set("api_key", "secret")


def test_runtime_config_tool_updates_model(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, runtime_config=store)

    result = tools.set_runtime_config("openai_model", "gpt-6.7-agi")

    assert result.ok
    assert store.get("openai_model") == "gpt-6.7-agi"


def test_runtime_config_rejects_unsupported_keys(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime.sqlite3")

    with pytest.raises(ValueError):
        store.set("unsupported_key", "strict")


def test_runtime_config_all_filters_obsolete_keys(tmp_path: Path) -> None:
    store = RuntimeConfigStore(tmp_path / "runtime.sqlite3")
    with store._connect() as conn:
        conn.execute("insert into runtime_config(key, value) values ('legacy_key', 'true')")
    store.set("openai_model", "claude-haiku-4-5-20251001")

    assert store.all() == {"openai_model": "claude-haiku-4-5-20251001"}
