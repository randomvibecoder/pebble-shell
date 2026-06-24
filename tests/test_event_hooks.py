from pathlib import Path

import json
import pytest

from pebble_shell.event_hooks import EventHookStore
from pebble_shell.tools import WorkspaceTools


def test_hook_set_registers_hook(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, event_hooks=store)

    result = tools.hook_set("email", "Summarize inbound email payloads.")

    assert result.ok
    hook = store.get_hook("email")
    assert hook is not None
    assert hook["prompt"] == "Summarize inbound email payloads."
    assert "channel_id" not in hook


def test_hook_events_are_visible_to_agent_tools(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, event_hooks=store)
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)
    store.mark_webhook_event_completed(event_id, "Summarized dark mode request.")

    dedicated = tools.run("hook_events", {"limit": 5})

    assert dedicated.ok
    events = json.loads(dedicated.output)
    assert len(events) == 1
    assert events[0]["name"] == "suggestion-box"
    assert events[0]["payload"]["suggestion"] == "Add dark mode."
    assert events[0]["status"] == "completed"
    assert "channel_id" not in dedicated.output


def test_hook_management_tools_enable_disable_remove_and_show(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, event_hooks=store)

    assert tools.run("hook_set", {"name": "suggestion-box", "prompt": "Summarize suggestions."}).ok
    assert json.loads(tools.run("hook_show", {"name": "suggestion-box"}).output)["enabled"] is True
    assert json.loads(tools.run("hook_list", {}).output)[0]["name"] == "suggestion-box"
    assert tools.run("hook_disable", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box")["enabled"] is False
    assert tools.run("hook_enable", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box")["enabled"] is True
    assert tools.run("hook_remove", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box") is None


def test_hook_list_limits_results(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, event_hooks=store)
    for index in range(3):
        store.upsert_hook(f"hook-{index}", "Handle event.")

    result = tools.run("hook_list", {"limit": 2})

    assert result.ok
    assert len(json.loads(result.output)) == 2


def test_hook_event_replay_schedules_existing_event(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")
    calls: list[int] = []
    tools = WorkspaceTools(
        tmp_path / "workspace",
        shell_timeout_seconds=1,
        event_hooks=store,
        webhook_replayer=lambda event_id: calls.append(event_id) or f"queued {event_id}",
    )
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)

    result = tools.run("hook_event_replay", {"event_id": event_id})

    assert result.ok
    assert result.output == f"queued {event_id}"
    assert calls == [event_id]


def test_webhook_hook_rejects_unsafe_names(tmp_path: Path) -> None:
    store = EventHookStore(tmp_path / "hooks.sqlite3")

    with pytest.raises(ValueError):
        store.upsert_hook("../email", "prompt")
