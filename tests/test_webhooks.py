from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from pebble_shell.agent import AgentResponse
from pebble_shell.self_improvement import SelfImprovementStore, format_webhook_message
import pebble_shell.server as server
from pebble_shell.server import app


@dataclass
class FakeWebhookAgent:
    self_improvement: SelfImprovementStore
    calls: list[tuple[str, str]]

    async def run_internal_event(self, content: str, source: str) -> AgentResponse:
        self.calls.append((content, source))
        return AgentResponse(content=f"handled {source}", steps=1)

    async def replay_hook_event(self, event_id: int) -> AgentResponse:
        event = self.self_improvement.get_webhook_event(event_id)
        hook = self.self_improvement.get_hook(event["name"])
        replay_event_id = self.self_improvement.record_webhook_event(event["name"], event["payload"], background=True)
        self.self_improvement.mark_webhook_event_processing(replay_event_id)
        response = await self.run_internal_event(
            format_webhook_message(event["name"], hook["prompt"], event["payload"]),
            f"webhook:{event['name']}:replay",
        )
        self.self_improvement.mark_webhook_event_completed(replay_event_id, response.content)
        return response


@dataclass
class FailingWebhookAgent:
    self_improvement: SelfImprovementStore

    async def run_internal_event(self, content: str, source: str) -> AgentResponse:
        raise RuntimeError("model unavailable")


def test_webhook_trigger_routes_fake_email_environment(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("email-alert", "Classify priority and summarize sender intent.")
    fake = FakeWebhookAgent(store, [])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/webhooks/email-alert",
        json={
            "environment": "staging",
            "provider": "mailgun",
            "from": "alerts@example.com",
            "subject": "Database latency above threshold",
            "body": "p95 latency exceeded 500ms for 15 minutes.",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"content": "handled webhook:email-alert", "steps": 1}
    content, source = fake.calls[0]
    assert source == "webhook:email-alert"
    assert "This is a webhook turn. The time is " in content
    assert " UTC." in content
    assert "Classify priority" in content
    assert "mailgun" in content
    assert "Database latency" in content
    assert store.list_webhook_events()[0]["name"] == "email-alert"
    assert store.list_webhook_events()[0]["payload"]["provider"] == "mailgun"
    assert store.list_webhook_events()[0]["status"] == "completed"
    assert "handled webhook:email-alert" in store.list_webhook_events()[0]["result_excerpt"]


def test_webhook_trigger_routes_fake_ci_environment(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("ci-failure", "Inspect failed checks and propose the next bounded fix.")
    fake = FakeWebhookAgent(store, [])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/webhooks/ci-failure",
        json={
            "environment": "production",
            "provider": "github-actions",
            "repository": "example/service",
            "branch": "main",
            "workflow": "docker-build",
            "status": "failure",
            "failed_step": "pytest",
        },
    )

    assert response.status_code == 200
    content, source = fake.calls[0]
    assert source == "webhook:ci-failure"
    assert "github-actions" in content
    assert "failed_step" in content


def test_webhook_background_mode_acknowledges_immediately(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    fake = FakeWebhookAgent(store, [])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/webhooks/suggestion-box?background=true",
        json={"name": "Tester", "suggestion": "Add keyboard shortcuts."},
    )

    assert response.status_code == 200
    assert response.json() == {
        "content": "Webhook hook `suggestion-box` accepted for background processing.",
        "steps": 0,
    }
    assert fake.calls[0][1] == "webhook:suggestion-box"
    event = store.list_webhook_events()[0]
    assert event["name"] == "suggestion-box"
    assert event["background"] is True
    assert event["payload"]["suggestion"] == "Add keyboard shortcuts."
    assert event["status"] == "completed"
    assert event["processed_at"] is not None


def test_webhook_event_replay_routes_original_payload(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("suggestion-box", "Summarize suggestions.")
    event_id = store.record_webhook_event("suggestion-box", {"suggestion": "Add keyboard shortcuts."}, background=True)
    fake = FakeWebhookAgent(store, [])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(f"/webhooks/events/{event_id}/replay?background=false")

    assert response.status_code == 200
    assert response.json() == {"content": "handled webhook:suggestion-box:replay", "steps": 1}
    content, source = fake.calls[0]
    assert source == "webhook:suggestion-box:replay"
    assert "This is a webhook turn. The time is " in content
    assert "Add keyboard shortcuts" in content
    events = store.list_webhook_events()
    assert events[0]["name"] == "suggestion-box"
    assert events[0]["status"] == "completed"
    assert events[0]["payload"]["suggestion"] == "Add keyboard shortcuts."


def test_webhook_event_records_failures(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("failing-hook", "Handle failures.")
    monkeypatch.setattr(server, "get_agent", lambda: FailingWebhookAgent(store))

    response = TestClient(app, raise_server_exceptions=False).post("/webhooks/failing-hook", json={"event": "boom"})

    assert response.status_code == 500
    event = store.list_webhook_events()[0]
    assert event["name"] == "failing-hook"
    assert event["status"] == "failed"
    assert event["error"] == "model unavailable"
    assert event["processed_at"] is not None


def test_webhook_trigger_rejects_disabled_hook(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("disabled-hook", "Do not run.")
    with sqlite3.connect(tmp_path / "self.sqlite3") as conn:
        conn.execute("update webhook_hooks set enabled = 0 where name = 'disabled-hook'")
    fake = FakeWebhookAgent(store, [])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/webhooks/disabled-hook", json={"environment": "test"})

    assert response.status_code == 409
    assert fake.calls == []


def test_webhook_preflight_allows_browser_forms() -> None:
    response = TestClient(app).options(
        "/webhooks/suggestion-box",
        headers={
            "origin": "http://localhost:18081",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
