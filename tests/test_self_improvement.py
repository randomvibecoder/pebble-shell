from pathlib import Path

import json
import pytest

from pebble_shell.self_improvement import SelfImprovementStore
from pebble_shell.tools import WorkspaceTools


def test_hook_set_registers_hook(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)

    result = tools.hook_set("email", "Summarize inbound email payloads.")

    assert result.ok
    hook = store.get_hook("email")
    assert hook is not None
    assert hook["prompt"] == "Summarize inbound email payloads."
    assert "channel_id" not in hook


def test_hook_events_are_visible_to_agent_tools(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)
    store.mark_webhook_event_completed(event_id, "Summarized dark mode request.")

    dedicated = tools.run("hook_events", {"limit": 5})
    combined = tools.run("self_improvements_list", {})

    assert dedicated.ok
    assert json.loads(dedicated.output)[0]["payload"]["suggestion"] == "Add dark mode."
    assert json.loads(dedicated.output)[0]["status"] == "completed"
    combined_payload = json.loads(combined.output)
    assert combined_payload["hook_events"][0]["name"] == "suggestion-box"
    assert combined_payload["hooks"][0]["name"] == "suggestion-box"
    assert "channel_id" not in combined.output


def test_hook_management_tools_enable_disable_remove_and_show(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)

    assert tools.run("hook_set", {"name": "suggestion-box", "prompt": "Summarize suggestions."}).ok
    assert json.loads(tools.run("hook_show", {"name": "suggestion-box"}).output)["enabled"] is True
    assert json.loads(tools.run("hook_list", {}).output)[0]["name"] == "suggestion-box"
    assert tools.run("hook_disable", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box")["enabled"] is False
    assert tools.run("hook_enable", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box")["enabled"] is True
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)
    assert tools.run("hook_remove", {"name": "suggestion-box"}).ok
    assert store.get_hook("suggestion-box") is None
    assert store.get_webhook_event(event_id)["payload"]["suggestion"] == "Add dark mode."


def test_hook_event_replay_schedules_existing_event(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    calls: list[int] = []
    tools = WorkspaceTools(
        tmp_path / "workspace",
        shell_timeout_seconds=1,
        self_improvement=store,
        webhook_replayer=lambda event_id: calls.append(event_id) or f"queued {event_id}",
    )
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)

    result = tools.run("hook_event_replay", {"event_id": event_id})

    assert result.ok
    assert result.output == f"queued {event_id}"
    assert calls == [event_id]


def test_webhook_hook_rejects_unsafe_names(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")

    with pytest.raises(ValueError):
        store.upsert_hook("../email", "prompt")
