from pathlib import Path

import json
import pytest

from opencode_agent.self_improvement import SelfImprovementStore
from opencode_agent.skills import SkillLoader
from opencode_agent.tools import CURRENT_CHANNEL_ID, WorkspaceTools


def test_skill_save_creates_loadable_skill(tmp_path: Path) -> None:
    skills = SkillLoader(tmp_path, tmp_path / "bundled")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, skills=skills)

    result = tools.skill_save("email-triage", "# Email triage\n\nSummarize urgent mail first.", "email workflow")

    assert result.ok
    assert "email-triage" in skills.list()
    assert "urgent mail" in skills.view("email-triage")


def test_loader_includes_default_skills_file(tmp_path: Path) -> None:
    (tmp_path / "SKILLS.md").write_text("# Defaults\n\nAlways use runtime config tools.", encoding="utf-8")
    skills = SkillLoader(tmp_path / "workspace", tmp_path)

    loaded = skills.load("change heartbeat to one hour")

    assert "Available skills:" in loaded
    assert "SKILLS" in loaded
    assert "Always use runtime config tools." in loaded


def test_loader_includes_relevant_installed_skill(tmp_path: Path) -> None:
    skills = SkillLoader(tmp_path, tmp_path / "bundled")
    skills.save("playwright-cli", "# Playwright CLI\n\nUse the playwright command for browser tests.")
    skills.save("email-triage", "# Email triage\n\nSort newsletters.")

    loaded = skills.load("run a playwright browser test")

    assert "Use the playwright command" in loaded
    assert "Sort newsletters" not in loaded


def test_skill_install_creates_loadable_skill_from_workspace_path(tmp_path: Path) -> None:
    candidate = tmp_path / "incoming" / "SKILL.md"
    candidate.parent.mkdir()
    candidate.write_text("---\nname: playwright-cli\ndescription: Browser automation.\n---\n# Browser Automation", encoding="utf-8")
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    skills = SkillLoader(tmp_path, tmp_path / "bundled")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, skills=skills, self_improvement=store)

    result = tools.skill_install("incoming/SKILL.md")

    assert result.ok
    assert "playwright-cli" in skills.list()
    assert "Browser Automation" in skills.view("playwright-cli")
    assert store.list_records()[0]["kind"] == "skill_install"


def test_skill_install_requires_text_skill_file(tmp_path: Path) -> None:
    candidate = tmp_path / "incoming" / "skill.bin"
    candidate.parent.mkdir()
    candidate.write_bytes(b"skill")
    skills = SkillLoader(tmp_path, tmp_path / "bundled")

    with pytest.raises(ValueError):
        skills.install_from_path(candidate)


def test_skill_disable_enable_and_delete(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    skills = SkillLoader(tmp_path, tmp_path / "bundled")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, skills=skills, self_improvement=store)
    tools.skill_save("email-triage", "# Email triage\n\nSort urgent mail first.")

    disabled = tools.skill_disable("email-triage")

    assert disabled.ok
    assert "email-triage" not in skills.list()
    assert any(item["name"] == "email-triage" and item["enabled"] is False for item in skills.list_details())

    enabled = tools.skill_enable("email-triage")

    assert enabled.ok
    assert "email-triage" in skills.list()

    deleted = tools.skill_delete("email-triage")

    assert deleted.ok
    assert "email-triage" not in skills.list()
    assert [record["kind"] for record in store.list_records(limit=10)][:4] == [
        "skill_delete",
        "skill_enable",
        "skill_disable",
        "skill",
    ]


def test_skill_delete_refuses_bundled_skill(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    (bundled / "skills").mkdir(parents=True)
    (bundled / "skills" / "humanizer.md").write_text("# Humanizer", encoding="utf-8")
    skills = SkillLoader(tmp_path / "workspace", bundled)
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, skills=skills)

    result = tools.skill_delete("humanizer")

    assert not result.ok
    assert "Cannot delete bundled skill humanizer" in result.output


def test_webhook_hook_save_registers_hook(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)

    token = CURRENT_CHANNEL_ID.set("local-channel")
    try:
        result = tools.webhook_hook_save("email", "Summarize inbound email payloads.")
    finally:
        CURRENT_CHANNEL_ID.reset(token)

    assert result.ok
    hook = store.get_hook("email")
    assert hook is not None
    assert hook["prompt"] == "Summarize inbound email payloads."
    assert hook["channel_id"] == "local-channel"


def test_webhook_events_are_visible_to_agent_tools(tmp_path: Path) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, self_improvement=store)
    store.upsert_hook("suggestion-box", "Summarize suggestions.", "local-channel")
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
        store.upsert_hook("../email", "prompt", "channel")
