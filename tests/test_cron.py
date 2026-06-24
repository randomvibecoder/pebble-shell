from pathlib import Path

import json
import pytest

from pebble_shell.agent import AgentResponse
from pebble_shell.cron import CronRunner, CronStore
from pebble_shell.tools import WorkspaceTools


def test_cron_store_persists_and_lists_jobs(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    store.upsert_job("daily-report", 3600)

    jobs = store.list_jobs()
    assert jobs[0]["name"] == "daily-report"
    assert jobs[0]["every_seconds"] == 3600
    assert jobs[0]["enabled"]
    assert jobs[0]["remaining_runs"] == 1


def test_cron_store_persists_explicit_run_count(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    store.upsert_job("daily-report", 3600, times=7)

    job = store.get_job("daily-report")
    assert job is not None
    assert job.remaining_runs == 7
    assert store.list_jobs()[0]["remaining_runs"] == 7


def test_cron_store_rejects_too_fast_jobs(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    with pytest.raises(ValueError):
        store.upsert_job("spam", 5)


def test_cron_store_rejects_invalid_run_counts(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    with pytest.raises(ValueError, match="cron times"):
        store.upsert_job("none", 60, times=0)
    with pytest.raises(ValueError, match="cron times"):
        store.upsert_job("too-many", 60, times=501)


def test_cron_due_jobs_respects_enabled_flag(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("check", 60)
    store.set_enabled("check", False)

    assert store.due_jobs(now=9999999999) == []


def test_cron_tool_saves_job(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    result = tools.cron_job_save("check-build", 120)

    assert result.ok
    assert store.get_job("check-build") is not None


def test_cron_tool_saves_run_count(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    result = tools.cron_job_save("check-build", 120, times=3)

    assert result.ok
    assert store.get_job("check-build").remaining_runs == 3


def test_cron_list_has_no_route_fields(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("daily-report", 3600)
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    result = tools.cron_list()

    assert result.ok
    assert "channel_id" not in result.output


def test_cron_list_limits_jobs_and_runs(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    for index in range(3):
        name = f"job-{index}"
        store.upsert_job(name, 3600)
        job = store.get_job(name)
        assert job is not None
        store.record_run(job, f"done {index}", steps=1)
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    result = tools.run("cron_list", {"jobs_limit": 2, "runs_limit": 1})

    assert result.ok
    payload = json.loads(result.output)
    assert len(payload["jobs"]) == 2
    assert len(payload["runs"]) == 1


def test_cron_record_run_decrements_and_disables_at_zero(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("check", 60, times=1)
    job = store.get_job("check")
    assert job is not None

    store.record_run(job, "done", steps=1)

    updated = store.get_job("check")
    assert updated is not None
    assert updated.remaining_runs == 0
    assert not updated.enabled
    assert store.due_jobs(now=9999999999) == []


def test_cron_record_run_decrements_failed_attempts(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("check", 60, times=2)
    job = store.get_job("check")
    assert job is not None

    store.record_run(job, "failed", steps=0, ok=False)

    updated = store.get_job("check")
    assert updated is not None
    assert updated.remaining_runs == 1
    assert updated.enabled


class FakeCronAgent:
    def __init__(self) -> None:
        self.contents: list[str] = []
        self._deliver = None

    async def run_internal_event(self, content: str, source: str) -> AgentResponse:
        self.contents.append(content)
        return AgentResponse(content=f"handled {source}", steps=1)


@pytest.mark.asyncio
async def test_cron_runner_injects_utc_timestamp(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("check", 60)
    agent = FakeCronAgent()

    await CronRunner(agent, store).run_job("check")

    assert agent.contents
    assert agent.contents[0].startswith("This is a cron turn. The time is ")
    assert " UTC.\n\nScheduled job `check` fired." in agent.contents[0]
