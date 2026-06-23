from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

import pebble_shell.server as server
from pebble_shell.agent import AgentResponse
from pebble_shell.server import app, get_settings


ALLOWED_USER_ID = "111111111111111111"


@dataclass
class FakeAgent:
    calls: int = 0

    async def run_user_message(self, content: str, images: list[object] | None = None) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content=f"ok:{content}:primary", steps=1)


@dataclass
class FakeRuntimeConfig:
    def all(self) -> dict[str, str]:
        return {"openai_model": "runtime/model", "heartbeat_every_seconds": "3600"}


class FakeCron:
    def list_jobs(self) -> list[dict[str, object]]:
        return [{"name": "daily", "enabled": True}, {"name": "paused", "enabled": False}]

    def list_runs(self, limit: int = 20) -> list[dict[str, object]]:
        return [{"job_name": "daily"}]


class FakeSelfImprovement:
    def list_hooks(self) -> list[dict[str, object]]:
        return [{"name": "email", "enabled": True}]

    def list_records(self, limit: int = 20) -> list[dict[str, object]]:
        return [{"kind": "skill", "name": "playwright-cli"}]

    def list_webhook_events(self, limit: int = 20) -> list[dict[str, object]]:
        return [{"name": "email", "payload": {"subject": "hello"}, "background": True}]


class FakeProcesses:
    def list(self) -> list[dict[str, object]]:
        return [{"name": "web-dev", "running": True, "pid": 1234}]


class FakeTools:
    processes = FakeProcesses()


class FakeBackgroundStore:
    def count_active(self) -> int:
        return 2

    def list_jobs(self, limit: int = 10) -> list[dict[str, object]]:
        return [{"id": "bg_test", "status": "running", "folder": "background_jobs/bg_test"}]


class FakeStatusAgent:
    runtime_config = FakeRuntimeConfig()
    cron = FakeCron()
    self_improvement = FakeSelfImprovement()
    tools = FakeTools()
    background_store = FakeBackgroundStore()
    current_model = "runtime/model"

    def candidate_models(self) -> list[str]:
        return ["runtime/model", "fallback/model"]

    def flash_candidate_models(self) -> list[str]:
        return ["flash/model", "flash/fallback"]


def test_chat_endpoint_is_not_exposed(monkeypatch) -> None:
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/chat", json={"content": "hello"})

    assert response.status_code == 404
    assert fake.calls == 0


def test_auth_token_blocks_webhook_before_agent(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/webhooks/test", json={"content": "hello"})

    assert response.status_code == 401
    assert fake.calls == 0
    get_settings.cache_clear()


def test_health_does_not_require_auth(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    get_settings.cache_clear()


def test_status_requires_auth(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: FakeStatusAgent())

    response = TestClient(app).get("/status")

    assert response.status_code == 401
    get_settings.cache_clear()


def test_status_reports_runtime_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-secret-value")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token-value")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "public-key-value")
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: FakeStatusAgent())

    response = TestClient(app).get("/status", headers={"authorization": "Bearer secret-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent"]["version"] == "0.0.1"
    assert payload["model"]["current"] == "runtime/model"
    assert payload["model"]["fallbacks"] == ["fallback/model"]
    assert payload["model"]["flash_fallbacks"] == ["flash/model", "flash/fallback"]
    assert payload["heartbeat"]["every_seconds"] == 3600
    assert payload["discord"]["gateway_enabled"] is True
    assert payload["discord"]["interactions_enabled"] is True
    assert payload["discord"]["client_secret_configured"] is True
    assert payload["security"]["api_auth_enabled"] is True
    assert payload["processes"] == [{"name": "web-dev", "running": True, "pid": 1234}]
    assert payload["background_tasks"]["active_count"] == 2
    assert payload["background_tasks"]["recent"][0]["id"] == "bg_test"
    assert payload["cron"]["job_count"] == 2
    assert payload["cron"]["enabled_job_count"] == 1
    assert payload["self_improvement"]["recent_webhook_events"][0]["name"] == "email"
    assert "should-not-leak" not in response.text
    assert "discord-secret-value" not in response.text
    assert "bot-token-value" not in response.text
    get_settings.cache_clear()
