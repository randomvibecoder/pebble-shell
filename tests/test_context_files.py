from pathlib import Path

from pebble_shell.context_files import ContextFileLoader, ensure_workspace_context_files


def test_context_loader_does_not_load_heartbeat_md(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    for name in ("SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        (context_dir / name).write_text(f"{name} content", encoding="utf-8")

    messages = ContextFileLoader(tmp_path, tmp_path).load()
    contents = [message["content"] for message in messages]

    assert any(content.startswith("context/SOUL.md:") for content in contents)
    assert not any(content.startswith("HEARTBEAT.md:") for content in contents)
    assert not any(content.startswith("MEMORY.md:") for content in contents)


def test_context_loader_caches_until_refresh(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    tools_path = context_dir / "TOOLS.md"
    tools_path.write_text("old tools", encoding="utf-8")
    loader = ContextFileLoader(tmp_path, tmp_path)

    tools_path.write_text("new tools", encoding="utf-8")

    assert any(message["content"] == "context/TOOLS.md:\nold tools" for message in loader.load())
    assert not any(message["content"] == "context/TOOLS.md:\nnew tools" for message in loader.load())

    loader.refresh()

    assert any(message["content"] == "context/TOOLS.md:\nnew tools" for message in loader.load())


def test_bundled_tools_document_webhook_context_and_send_msg() -> None:
    tools_text = Path("context/TOOLS.md").read_text(encoding="utf-8")

    assert "Webhook events are normal foreground turns in the same single linear chat" in tools_text
    assert "`send_msg` is available during webhook work" in tools_text


def test_workspace_context_files_are_seeded_from_bundled_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundled = tmp_path / "bundled"
    (bundled / "context").mkdir(parents=True)
    (bundled / "context" / "MEMORY.md").write_text("default memory", encoding="utf-8")
    (bundled / "context" / "HEARTBEAT.md").write_text("default heartbeat", encoding="utf-8")

    ensure_workspace_context_files(workspace, bundled)

    assert (workspace / "context" / "MEMORY.md").read_text(encoding="utf-8") == "default memory"
    assert (workspace / "context" / "HEARTBEAT.md").read_text(encoding="utf-8") == "default heartbeat"
