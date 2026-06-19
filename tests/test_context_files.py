from pathlib import Path

from opencode_agent.context_files import ContextFileLoader


def test_context_loader_does_not_load_heartbeat_md(tmp_path: Path) -> None:
    for name in ("SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        (tmp_path / name).write_text(f"{name} content", encoding="utf-8")

    messages = ContextFileLoader(tmp_path, tmp_path).load()
    contents = [message["content"] for message in messages]

    assert any(content.startswith("SOUL.md:") for content in contents)
    assert not any(content.startswith("HEARTBEAT.md:") for content in contents)
    assert not any(content.startswith("MEMORY.md:") for content in contents)


def test_context_loader_caches_until_refresh(tmp_path: Path) -> None:
    tools_path = tmp_path / "TOOLS.md"
    tools_path.write_text("old tools", encoding="utf-8")
    loader = ContextFileLoader(tmp_path, tmp_path)

    tools_path.write_text("new tools", encoding="utf-8")

    assert any(message["content"] == "TOOLS.md:\nold tools" for message in loader.load())
    assert not any(message["content"] == "TOOLS.md:\nnew tools" for message in loader.load())

    loader.refresh()

    assert any(message["content"] == "TOOLS.md:\nnew tools" for message in loader.load())
