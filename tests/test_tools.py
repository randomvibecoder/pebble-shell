from pathlib import Path

import json
import time

from pebble_shell.memory import MemoryStore
from pebble_shell.tools import WorkspaceTools


def test_file_tools_allow_parent_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.read(f"../{outside.name}")

    assert result.ok
    assert result.output == "outside"


def test_file_tools_allow_workspace_root_backtracking(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-slash-outside.txt"
    outside.write_text("outside from slash", encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.read(f"/../{outside.name}")

    assert result.ok
    assert result.output == "outside from slash"


def test_write_read_and_list_file(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    written = tools.write("notes/todo.txt", "hello")
    listed = tools.ls("notes")
    read = tools.read("notes/todo.txt")

    assert written.ok
    assert listed.output == "notes/todo.txt"
    assert read.output == "hello"


def test_ls_glob_grep_and_worker_cwd(tmp_path: Path) -> None:
    worker = tmp_path / "project"
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, cwd=worker)

    assert tools.run("write", {"path": "src/app.py", "content": "print('needle')\n"}).ok
    assert (worker / "src" / "app.py").is_file()
    assert tools.run("bash", {"command": "pwd"}).output.strip() == worker.as_posix()
    assert tools.run("ls", {"path": "src"}).output == "project/src/app.py"
    assert tools.run("glob", {"pattern": "**/*.py"}).output == "project/src/app.py"
    assert "needle" in tools.run("grep", {"pattern": "needle"}).output
    assert tools.run("read", {"path": "src/app.py"}).output == "print('needle')\n"


def test_old_model_tool_names_are_not_accepted(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    assert not tools.run("read_file", {"path": "x"}).ok
    assert not tools.run("write_file", {"path": "x", "content": "x"}).ok
    assert not tools.run("edit_file", {"path": "x", "old": "x", "new": "y"}).ok
    assert not tools.run("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}).ok
    assert not tools.run("inspect_image", {"path": "x.png"}).ok
    assert not tools.run("browser_visit", {"url": "https://example.com"}).ok
    assert not tools.run("exa_search", {"query": "test"}).ok


def test_edit_replaces_exact_text(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = tools.run("edit", {"path": "notes.txt", "old": "beta", "new": "BETA"})

    assert result.ok
    assert "1 replacement" in result.output
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_rejects_ambiguous_replacement(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("same\nsame\n", encoding="utf-8")

    result = tools.run("edit", {"path": "notes.txt", "old": "same", "new": "new"})

    assert not result.ok
    assert "occurs 2 times" in result.output


def test_patch_add_update_delete_files(tmp_path: Path) -> None:
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
    result = tools.run("patch", {"patch": patch})

    assert result.ok
    assert "added added.txt" in result.output
    assert "updated notes.txt" in result.output
    assert "deleted old.txt" in result.output
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "created\n"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert not (tmp_path / "old.txt").exists()


def test_patch_rejects_non_matching_hunk(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")

    patch = """*** Begin Patch
*** Update File: notes.txt
@@
-missing
+new
*** End Patch
"""
    result = tools.run("patch", {"patch": patch})

    assert not result.ok
    assert "did not match" in result.output
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\n"


def test_file_edit_tools_are_exposed(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    names = {definition["function"]["name"] for definition in tools.definitions()}

    assert "edit" in names
    assert "patch" in names


def test_ls_limits_directory_output(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.run("ls", {"limit": 3})

    assert result.ok
    assert len(result.output.splitlines()) == 4
    assert "[ls truncated at 3 entries]" in result.output


def test_list_tools_have_limits_and_short_names(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    definitions = {definition["function"]["name"]: definition["function"] for definition in tools.definitions()}

    assert "limit" in definitions["ls"]["parameters"]["properties"]
    assert "limit" in definitions["hook_list"]["parameters"]["properties"]
    assert "jobs_limit" in definitions["cron_list"]["parameters"]["properties"]
    assert "runs_limit" in definitions["cron_list"]["parameters"]["properties"]
    assert "limit" in definitions["shell_audit"]["parameters"]["properties"]
    assert "heartbeat_set" in definitions

    assert "cron_jobs_list" not in definitions
    assert "cron_job_set_enabled" not in definitions
    assert "shell_audit_recent" not in definitions
    assert "get_runtime_config" not in definitions
    assert "set_runtime_config" not in definitions


def test_send_msg_is_foreground_only_tool_definition(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    worker_tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, text_sender=lambda text: "sent")

    foreground = {definition["function"]["name"]: definition["function"] for definition in tools.definitions(include_background_tools=True)}
    background = {definition["function"]["name"]: definition["function"] for definition in tools.definitions(include_background_tools=False)}
    worker_background = {definition["function"]["name"]: definition["function"] for definition in worker_tools.definitions(include_background_tools=False)}

    assert "send_msg" in foreground
    assert "the user" in foreground["send_msg"]["description"]
    assert "send_msg" not in background
    assert "send_msg" in worker_background
    assert "foreground Pebble" in worker_background["send_msg"]["description"]
    assert "send_file" not in worker_background


def test_background_worker_tool_schema_excludes_orchestration_tools(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, text_sender=lambda text: "sent")

    names = {definition["function"]["name"] for definition in tools.definitions(include_background_tools=False)}

    assert {"ls", "glob", "grep", "read", "write", "edit", "patch", "bash", "exec_command", "write_stdin", "read_image", "websearch", "send_msg"} <= names
    assert not {
        "subagent_start",
        "subagent_dashboard",
        "subagent_send",
        "hook_set",
        "hook_list",
        "hook_events",
        "hook_event_replay",
        "cron_job_save",
        "cron_list",
        "cron_enable",
        "heartbeat_set",
        "send_file",
        "shell_audit",
    } & names


def test_model_tools_do_not_expose_route_parameters(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    definitions = {definition["function"]["name"]: definition["function"] for definition in tools.definitions()}

    webhook_schema = definitions["hook_set"]["parameters"]
    cron_schema = definitions["cron_job_save"]["parameters"]

    assert "channel_id" not in webhook_schema["required"]
    assert "channel_id" not in webhook_schema["properties"]
    assert "prompt" not in webhook_schema["required"]
    assert "prompt" not in webhook_schema["properties"]
    assert "channel_id" not in cron_schema["required"]
    assert "channel_id" not in cron_schema["properties"]
    assert "prompt" not in cron_schema["required"]
    assert "prompt" not in cron_schema["properties"]
    assert cron_schema["properties"]["times"]["minimum"] == 1
    assert cron_schema["properties"]["times"]["maximum"] == 500


def test_read_rejects_binary_files_before_model_context(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 1000)

    result = tools.read("paper.pdf")

    assert result.ok is False
    assert "Refusing to read likely binary file" in result.output


def test_read_truncates_large_text_files(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "large.txt").write_text("a" * 250_000, encoding="utf-8")

    result = tools.read("large.txt")

    assert result.ok is True
    assert len(result.output) < 50_000
    assert "read truncated" in result.output
    assert "Use targeted shell commands" in result.output
    assert "sed, rg, head, tail" in result.output


def test_send_file_uses_configured_file_sender(tmp_path: Path) -> None:
    sent = []

    def sender(path: Path) -> str:
        sent.append(path.name)
        return f"sent {path.name}"

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, file_sender=sender)
    result = tools.run("send_file", {"path": "report.pdf"})

    assert result.ok
    assert result.output == "sent report.pdf"
    assert sent == ["report.pdf"]


def test_send_file_without_sender_reports_ready_path(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    result = tools.run("send_file", {"path": "report.pdf"})

    assert result.ok
    assert "File ready at report.pdf" in result.output


def test_send_file_reports_discord_sender_failure_with_path(tmp_path: Path) -> None:
    def sender(path: Path) -> str:
        raise RuntimeError("discord gateway unavailable")

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, file_sender=sender)
    result = tools.run("send_file", {"path": "report.pdf"})

    assert not result.ok
    assert "File send failed for report.pdf" in result.output
    assert "discord gateway unavailable" in result.output


def test_send_file_rejects_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "large.pdf").write_bytes(b"x" * 11)
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, max_send_file_bytes=10)
    result = tools.run("send_file", {"path": "large.pdf"})

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


def test_bash_truncates_large_output_to_tmp_file(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=5)

    result = tools.run("bash", {"command": "python - <<'PY'\nprint('x' * 60000)\nPY"})

    assert result.ok
    assert len(result.output) < 52_000
    assert "bash output truncated" in result.output
    marker = "Full stdout/stderr saved at "
    saved = result.output.split(marker, 1)[1].split(";", 1)[0]
    assert saved.startswith("/tmp/pebble_shell_tool_outputs/")
    assert Path(saved).read_text(encoding="utf-8").startswith("x" * 100)


def test_shell_runs_inside_workspace(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=2)

    result = tools.bash("pwd")

    assert result.ok
    assert result.output.strip() == str(tmp_path)


def test_background_process_lifecycle(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    session_id = None

    start = tools.run(
        "exec_command",
        {
            "cmd": "python -c \"import time; print('READY', flush=True); time.sleep(30)\"",
            "yield_time_ms": 50,
            "max_output_tokens": 4000,
            "login": True,
        },
    )

    try:
        assert start.ok
        start_status = json.loads(start.output)
        assert start_status["running"] is True
        assert isinstance(start_status["session_id"], int)
        session_id = start_status["session_id"]

        output = start_status["output"]
        for _ in range(20):
            poll_result = tools.run("write_stdin", {"session_id": session_id, "chars": "", "yield_time_ms": 50})
            output += json.loads(poll_result.output)["output"]
            if "READY" in output:
                break
            time.sleep(0.05)
        assert "READY" in output

        definitions = {definition["function"]["name"] for definition in tools.definitions()}
        assert "exec_command" in definitions
        assert "write_stdin" in definitions
        assert "process_start" not in definitions
        assert "process_status" not in definitions
        assert "process_logs" not in definitions
        assert "process_stop" not in definitions
    finally:
        stop_payload = tools.processes.stop(session_id) if session_id is not None else None

    assert stop_payload is not None
    assert stop_payload["running"] is False


def test_exec_command_supports_stdin(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    start = tools.run(
        "exec_command",
        {
            "cmd": "python -c \"import sys; line=sys.stdin.readline(); print('ECHO:' + line.strip(), flush=True)\"",
            "yield_time_ms": 50,
        },
    )

    assert start.ok
    session_id = json.loads(start.output)["session_id"]
    result = tools.run("write_stdin", {"session_id": session_id, "chars": "hello\n", "yield_time_ms": 1000})

    assert result.ok
    payload = json.loads(result.output)
    output = payload["output"]
    for _ in range(5):
        if "ECHO:hello" in output and not payload["running"]:
            break
        payload = json.loads(tools.run("write_stdin", {"session_id": session_id, "chars": "", "yield_time_ms": 1000}).output)
        output += payload["output"]
    assert "ECHO:hello" in output
    assert payload["running"] is False


def test_exec_command_supports_codex_shaped_arguments(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    (tmp_path / "subdir").mkdir()

    result = tools.run(
        "exec_command",
        {
            "cmd": "pwd",
            "workdir": "subdir",
            "yield_time_ms": 1000,
            "max_output_tokens": 2000,
            "tty": True,
            "shell": "/bin/bash",
            "login": True,
        },
    )

    assert result.ok
    payload = json.loads(result.output)
    assert payload["running"] is False
    assert payload["tty"] is True
    assert payload["output"].strip() == str(tmp_path / "subdir")


def test_exec_command_allows_parent_workdir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = WorkspaceTools(workspace, shell_timeout_seconds=1)

    result = tools.run("exec_command", {"cmd": "pwd", "workdir": "..", "yield_time_ms": 1000})

    assert result.ok
    assert json.loads(result.output)["output"].strip() == str(tmp_path)


def test_old_process_tool_names_are_not_model_tools(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)
    definitions = {definition["function"]["name"] for definition in tools.definitions()}

    assert {"exec_command", "write_stdin"} <= definitions
    for name in ("process_start", "process_status", "process_logs", "process_stop", "processes_list"):
        assert name not in definitions
        assert not tools.run(name, {}).ok


def test_websearch_uses_api_key_and_limits_results(tmp_path: Path, monkeypatch) -> None:
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

    result = tools.run("websearch", {"query": "OpenClaw heartbeat", "num_results": 99})

    assert result.ok
    assert captured["url"] == "https://api.exa.ai/search"
    assert captured["headers"]["X-api-key"] == "test-exa-key"
    assert captured["headers"]["User-agent"] == "PebbleShell/0.0.1"
    assert captured["body"] == {"query": "OpenClaw heartbeat", "numResults": 10}
    assert captured["timeout"] == 20
    assert json.loads(result.output)["results"][0]["title"] == "Example"


def test_websearch_requires_api_key(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1)

    result = tools.run("websearch", {"query": "OpenClaw heartbeat"})

    assert not result.ok
    assert "EXA_API_KEY" in result.output


def test_db_memory_tools_are_not_exposed(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path, shell_timeout_seconds=1, memory=MemoryStore(tmp_path / "memory.sqlite3"))
    names = {definition["function"]["name"] for definition in tools.definitions()}

    assert "record_memory" not in names
    assert "memory_index_file" not in names
    assert "memory_search" not in names
    assert "skills_list" not in names
    assert "skill_save" not in names
    assert "skill_install" not in names
    assert not tools.run("record_memory", {"memory": "User prefers concise answers."}).ok
    assert not tools.run("memory_index_file", {"path": "runbook.md"}).ok
    assert not tools.run("memory_search", {"query": "anything"}).ok
    assert not tools.run("skills_list", {}).ok
