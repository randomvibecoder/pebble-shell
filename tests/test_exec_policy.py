from pathlib import Path

from opencode_agent.exec_policy import ExecAuditStore, ExecPolicy
from opencode_agent.tools import WorkspaceTools


def test_policy_allows_container_commands() -> None:
    decision = ExecPolicy().decide("docker ps", "docker")

    assert decision.allowed
    assert decision.risk == "normal"
    assert "Docker container" in decision.reason


def test_policy_allows_file_removal_inside_container() -> None:
    decision = ExecPolicy().decide("rm -rf build", "rm")

    assert decision.allowed
    assert decision.risk == "normal"


def test_shell_audits_allowed_command(tmp_path: Path) -> None:
    audit = ExecAuditStore(tmp_path / "audit.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, exec_audit=audit)

    result = tools.shell("echo ok")

    assert result.ok
    records = audit.recent()
    assert records[0]["command"] == "echo ok"
    assert records[0]["allowed"]


def test_workspace_removal_command_runs_without_runtime_toggle(tmp_path: Path) -> None:
    audit = ExecAuditStore(tmp_path / "audit.sqlite3")
    workspace = tmp_path / "workspace"
    (workspace / "build").mkdir(parents=True)
    tools = WorkspaceTools(workspace, shell_timeout_seconds=1, exec_audit=audit)

    result = tools.shell("rm -rf build")

    assert result.ok
    assert not (workspace / "build").exists()
    assert audit.recent()[0]["risk"] == "normal"
