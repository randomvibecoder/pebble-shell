from pathlib import Path

import json
import time

from pebble_shell.memory import MemoryStore
from pebble_shell.tools import WorkspaceTools


def test_workspace_paths_cannot_escape(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.read_file("../outside")

    assert not result.ok
    assert "escapes workspace" in result.output


def test_write_read_and_list_file(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    written = tools.write_file("notes/todo.txt", "hello")
    listed = tools.list_files("notes")
    read = tools.read_file("notes/todo.txt")

    assert written.ok
    assert listed.output == "notes/todo.txt"
    assert read.output == "hello"


def test_edit_file_replaces_exact_text(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = tools.run("edit_file", {"path": "notes.txt", "old": "beta", "new": "BETA"})

    assert result.ok
    assert "1 replacement" in result.output
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_file_rejects_ambiguous_replacement(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("same\nsame\n", encoding="utf-8")

    result = tools.run("edit_file", {"path": "notes.txt", "old": "same", "new": "new"})

    assert not result.ok
    assert "occurs 2 times" in result.output


def test_apply_patch_add_update_delete_files(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("delete me\n", encoding="utf-8")

    patch = """*** Begin Patch
*** Add File: added.txt
+created
*** Update File: notes.txt
@@
 alpha
-beta
+BETA
 gamma
*** Delete File: old.txt
*** End Patch
"""
    result = tools.run("apply_patch", {"patch": patch})

    assert result.ok
    assert "added added.txt" in result.output
    assert "updated notes.txt" in result.output
    assert "deleted old.txt" in result.output
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "created\n"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert not (tmp_path / "old.txt").exists()


def test_apply_patch_rejects_non_matching_hunk(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")

    patch = """*** Begin Patch
*** Update File: notes.txt
@@
-missing
+new
*** End Patch
"""
    result = tools.run("apply_patch", {"patch": patch})

    assert not result.ok
    assert "did not match" in result.output
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\n"


def test_file_edit_tools_are_exposed(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    names = {definition["function"]["name"] for definition in tools.definitions()}

    assert "edit_file" in names
    assert "apply_patch" in names


def test_send_msg_is_foreground_only_tool_definition(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    foreground_names = {definition["function"]["name"] for definition in tools.definitions(include_background_tools=True)}
    background_names = {definition["function"]["name"] for definition in tools.definitions(include_background_tools=False)}

    assert "send_msg" in foreground_names
    assert "send_msg" not in background_names


def test_model_tools_do_not_expose_route_parameters(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    definitions = {definition["function"]["name"]: definition["function"] for definition in tools.definitions()}

    webhook_schema = definitions["webhook_hook_save"]["parameters"]
    cron_schema = definitions["cron_job_save"]["parameters"]

    assert "channel_id" not in webhook_schema["required"]
    assert "channel_id" not in webhook_schema["properties"]
    assert "channel_id" not in cron_schema["required"]
    assert "channel_id" not in cron_schema["properties"]


def test_read_file_rejects_binary_files_before_model_context(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 1000)

    result = tools.read_file("paper.pdf")

    assert result.ok is False
    assert "Refusing to read likely binary file" in result.output


def test_read_file_truncates_large_text_files(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "large.txt").write_text("a" * 250_000, encoding="utf-8")

    result = tools.read_file("large.txt")

    assert result.ok is True
    assert len(result.output) < 50_000
    assert "read_file truncated" in result.output


def test_publish_static_site_copies_directory_without_hidden_files(tmp_path: Path) -> None:
    source = tmp_path / "site"
    source.mkdir()
    (source / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    (source / ".secret").write_text("hidden", encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.publish_static_site("site", "demo")

    assert result.ok
    assert "/public/demo/index.html" in result.output
    assert (tmp_path / "public" / "demo" / "index.html").read_text(encoding="utf-8") == "<h1>Hello</h1>"
    assert not (tmp_path / "public" / "demo" / ".secret").exists()


def test_publish_static_site_rejects_hidden_source(tmp_path: Path) -> None:
    hidden = tmp_path / ".pebble_shell"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("secret", encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.publish_static_site(".pebble_shell", "bad")

    assert not result.ok
    assert "hidden" in result.output


def test_send_file_to_user_uses_configured_file_sender(tmp_path: Path) -> None:
    sent = []

    def sender(path: Path) -> str:
        sent.append(path.name)
        return f"sent {path.name}"

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, file_sender=sender)
    result = tools.run("send_file_to_user", {"path": "report.pdf"})

    assert result.ok
    assert result.output == "sent report.pdf"
    assert sent == ["report.pdf"]


def test_send_file_to_user_without_sender_reports_ready_path(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    result = tools.run("send_file_to_user", {"path": "report.pdf"})

    assert result.ok
    assert "File ready at report.pdf" in result.output


def test_send_file_to_user_reports_discord_sender_failure_with_path(tmp_path: Path) -> None:
    def sender(path: Path) -> str:
        raise RuntimeError("discord gateway unavailable")

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, file_sender=sender)
    result = tools.run("send_file_to_user", {"path": "report.pdf"})

    assert not result.ok
    assert "File send failed for report.pdf" in result.output
    assert "discord gateway unavailable" in result.output


def test_send_file_to_user_rejects_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "large.pdf").write_bytes(b"x" * 11)
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, max_send_file_bytes=10)
    result = tools.run("send_file_to_user", {"path": "large.pdf"})

    assert not result.ok
    assert "exceeds 10 bytes" in result.output


def test_send_msg_uses_configured_text_sender(tmp_path: Path) -> None:
    sent = []

    def sender(text: str) -> str:
        sent.append(text)
        return "sent progress"

    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, text_sender=sender)
    result = tools.run("send_msg", {"msg": "I started the browser verification."})

    assert result.ok
    assert result.output == "sent progress"
    assert sent == ["I started the browser verification."]


def test_send_msg_validates_length(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, text_sender=lambda text: "sent")

    empty_result = tools.run("send_msg", {"msg": "   "})
    long_result = tools.run("send_msg", {"msg": "x" * 501})

    assert not empty_result.ok
    assert "non-empty" in empty_result.output
    assert not long_result.ok
    assert "500 characters" in long_result.output


def test_public_sites_list_reports_published_sites(tmp_path: Path) -> None:
    source = tmp_path / "site"
    source.mkdir()
    (source / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    tools.publish_static_site("site", "demo")

    result = tools.run("public_sites_list", {})

    assert result.ok
    sites = json.loads(result.output)
    assert sites == [{"file_count": 1, "has_index": True, "name": "demo", "url": "/public/demo/index.html"}]


def test_shell_runs_inside_workspace(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=2)

    result = tools.shell("pwd")

    assert result.ok
    assert result.output.strip() == str(tmp_path)


def test_background_process_lifecycle(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    start = tools.run(
        "process_start",
        {
            "name": "dev-server",
            "command": "python -c \"import time; print('READY', flush=True); time.sleep(30)\"",
        },
    )

    try:
        assert start.ok
        start_status = json.loads(start.output)
        assert start_status["running"] is True
        assert start_status["name"] == "dev-server"

        logs = ""
        for _ in range(20):
            logs_result = tools.run("process_logs", {"name": "dev-server"})
            logs = logs_result.output
            if "READY" in logs:
                break
            time.sleep(0.05)
        assert "READY" in logs

        listed = json.loads(tools.run("processes_list", {}).output)
        assert listed[0]["name"] == "dev-server"
        assert json.loads(tools.run("process_status", {"name": "dev-server"}).output)["running"] is True
    finally:
        stop = tools.run("process_stop", {"name": "dev-server"})

    assert stop.ok
    assert json.loads(stop.output)["running"] is False


def test_background_process_start_allows_container_commands(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.run("process_start", {"name": "container-command", "command": "docker ps"})

    try:
        assert result.ok
        assert json.loads(result.output)["name"] == "container-command"
    finally:
        tools.run("process_stop", {"name": "container-command"})


def test_exa_search_uses_api_key_and_limits_results(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b'{"results":[{"title":"Example","url":"https://example.com"}]}'

    def fake_urlopen(request, timeout: int):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, exa_api_key="test-exa-key")

    result = tools.run("exa_search", {"query": "OpenClaw heartbeat", "num_results": 99})

    assert result.ok
    assert captured["url"] == "https://api.exa.ai/search"
    assert captured["headers"]["X-api-key"] == "test-exa-key"
    assert captured["headers"]["User-agent"] == "PebbleShell/0.0.1"
    assert captured["body"] == {"query": "OpenClaw heartbeat", "numResults": 10}
    assert captured["timeout"] == 20
    assert json.loads(result.output)["results"][0]["title"] == "Example"


def test_exa_search_requires_api_key(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.run("exa_search", {"query": "OpenClaw heartbeat"})

    assert not result.ok
    assert "EXA_API_KEY" in result.output


def test_db_memory_tools_are_not_exposed(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, memory=MemoryStore(tmp_path / "memory.sqlite3"))
    names = {definition["function"]["name"] for definition in tools.definitions()}

    assert "record_memory" not in names
    assert "memory_index_file" not in names
    assert "memory_search" not in names
    assert not tools.run("record_memory", {"memory": "User prefers concise answers."}).ok
    assert not tools.run("memory_index_file", {"path": "runbook.md"}).ok
    assert not tools.run("memory_search", {"query": "anything"}).ok
