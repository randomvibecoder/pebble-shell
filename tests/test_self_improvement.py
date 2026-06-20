from pathlib import Path

import json
import pytest

from pebble_shell.self_improvement import SelfImprovementStore
from pebble_shell.tools import WorkspaceTools


def test_webhook_hook_save_registers_hook(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)

    result = tools.webhook_hook_save("email", "Summarize inbound email payloads.")

    assert result.ok
    hook = store.get_hook("email")
    assert hook is not None
    assert hook["prompt"] == "Summarize inbound email payloads."
    assert "channel_id" not in hook


def test_webhook_events_are_visible_to_agent_tools(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add dark mode."}, background=True)
    store.mark_webhook_event_completed(event_id, "Summarized dark mode request.")

    dedicated = tools.run("webhook_events_list", {"limit": 5})
    combined = tools.run("self_improvements_list", {})

    assert dedicated.ok
    assert json.loads(dedicated.output)[0]["payload"]["suggestion"] == "Add dark mode."
    assert json.loads(dedicated.output)[0]["status"] == "completed"
    combined_payload = json.loads(combined.output)
    assert combined_payload["webhook_events"][0]["name"] == "suggestion-box"
    assert "channel_id" not in combined.output


def test_webhook_hook_rejects_unsafe_names(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")

    with pytest.raises(ValueError):
        store.upsert_hook("../email", "prompt")
