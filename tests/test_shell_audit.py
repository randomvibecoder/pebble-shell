from pathlib import Path
import json

from pebble_shell.shell_audit import ShellAuditStore
from pebble_shell.tools import WorkspaceTools


def test_shell_command_runs_inside_workspace(tmp_path: Path) -> None:
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1)

    result = tools.bash("echo ok")

    assert result.ok
    assert result.output.strip() == "ok"


def test_shell_audits_allowed_command(tmp_path: Path) -> None:
    audit = ShellAuditStore(tmp_path / "audit.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, shell_audit=audit)

    result = tools.bash("echo ok")

    assert result.ok
    records = audit.recent()
    assert records[0]["command"] == "echo ok"
    assert records[0]["allowed"]


def test_workspace_removal_command_runs_without_runtime_toggle(tmp_path: Path) -> None:
    audit = ShellAuditStore(tmp_path / "audit.sqlite3")
    workspace = tmp_path / "workspace"
    (workspace / "build").mkdir(parents=True)
    tools = WorkspaceTools(workspace, shell_timeout_seconds=1, shell_audit=audit)

    result = tools.bash("rm -rf build")

    assert result.ok
    assert not (workspace / "build").exists()
    assert audit.recent()[0]["risk"] == "normal"


def test_shell_audit_tool_limits_results(tmp_path: Path) -> None:
    audit = ShellAuditStore(tmp_path / "audit.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, shell_audit=audit)
    for index in range(3):
        assert tools.bash(f"echo {index}").ok

    result = tools.run("shell_audit", {"limit": 2})

    assert result.ok
    assert len(json.loads(result.output)) == 2
