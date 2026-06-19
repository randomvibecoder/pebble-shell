from pathlib import Path

import pytest

from opencode_agent.cron import CronStore
from opencode_agent.tools import CURRENT_CHANNEL_ID, WorkspaceTools


def test_cron_store_persists_and_lists_jobs(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    store.upsert_job("daily-report", "Summarize yesterday.", "local", 3600)

    jobs = store.list_jobs()
    assert jobs[0]["name"] == "daily-report"
    assert jobs[0]["every_seconds"] == 3600
    assert jobs[0]["enabled"]


def test_cron_store_rejects_too_fast_jobs(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")

    with pytest.raises(ValueError):
        store.upsert_job("spam", "Do a thing.", "local", 5)


def test_cron_due_jobs_respects_enabled_flag(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("check", "Check state.", "local", 60)
    store.set_enabled("check", False)

    assert store.due_jobs(now=9999999999) == []


def test_cron_tool_saves_job(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    token = CURRENT_CHANNEL_ID.set("local")
    try:
        result = tools.cron_job_save("check-build", "Check the build.", 120)
    finally:
        CURRENT_CHANNEL_ID.reset(token)

    assert result.ok
    assert store.get_job("check-build") is not None


def test_cron_jobs_list_hides_route_fields(tmp_path: Path) -> None:
    store = CronStore(tmp_path / "cron.sqlite3")
    store.upsert_job("daily-report", "Summarize yesterday.", "hidden-route", 3600)
    tools = WorkspaceTools(tmp_path / "workspace", shell_timeout_seconds=1, cron=store)

    result = tools.cron_jobs_list()

    assert result.ok
    assert "channel_id" not in result.output
    assert "hidden-route" not in result.output
