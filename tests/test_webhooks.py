from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from opencode_agent.agent import AgentResponse
from opencode_agent.self_improvement import SelfImprovementStore
import opencode_agent.server as server
from opencode_agent.server import app


@dataclass
class FakeWebhookAgent:
    self_improvement: SelfImprovementStore
    calls: list[tuple[str, str, str]]

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls.append((content, user_id, channel_id))
        return AgentResponse(content=f"handled {user_id} in {channel_id}", steps=1)


@dataclass
class FailingWebhookAgent:
    self_improvement: SelfImprovementStore

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        raise RuntimeError("model unavailable")


def test_webhook_trigger_routes_fake_email_environment(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("email-alert", "Classify priority and summarize sender intent.", "ops-channel")
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
    assert response.json() == {"content": "handled webhook:email-alert in ops-channel", "steps": 1}
    content, user_id, channel_id = fake.calls[0]
    assert user_id == "webhook:email-alert"
    assert channel_id == "ops-channel"
    assert "Classify priority" in content
    assert "mailgun" in content
    assert "Database latency" in content
    assert store.list_webhook_events()[0]["name"] == "email-alert"
    assert store.list_webhook_events()[0]["payload"]["provider"] == "mailgun"
    assert store.list_webhook_events()[0]["status"] == "completed"
    assert "handled webhook:email-alert" in store.list_webhook_events()[0]["result_excerpt"]


def test_webhook_trigger_routes_fake_ci_environment(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("ci-failure", "Inspect failed checks and propose the next bounded fix.", "builds")
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
    content, user_id, channel_id = fake.calls[0]
    assert user_id == "webhook:ci-failure"
    assert channel_id == "builds"
    assert "github-actions" in content
    assert "failed_step" in content


def test_webhook_background_mode_acknowledges_immediately(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("suggestion-box", "Summarize suggestions.", "suggestions")
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


def test_webhook_event_records_failures(tmp_path: Path, monkeypatch) -> None:
    store = SelfImprovementStore(tmp_path / "self.sqlite3")
    store.upsert_hook("failing-hook", "Handle failures.", "ops")
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
    store.upsert_hook("disabled-hook", "Do not run.", "ops")
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
