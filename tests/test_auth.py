from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

import opencode_agent.attachments as attachments_module
import opencode_agent.server as server
from opencode_agent.agent import AgentResponse
from opencode_agent.server import app, get_settings


ALLOWED_USER_ID = "111111111111111111"


@dataclass
class FakeAgent:
    calls: int = 0

    async def run_user_message(self, content: str, images: list[object] | None = None, delivery_route: str | None = None) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content=f"ok:{content}:{delivery_route or 'primary'}", steps=1)

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content=f"ok:{content}:{user_id}:{channel_id}", steps=1)


@dataclass
class FailingAgent:
    calls: int = 0

    async def run_user_message(self, content: str, images: list[object] | None = None, delivery_route: str | None = None) -> AgentResponse:
        self.calls += 1
        raise RuntimeError("provider unavailable")

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls += 1
        raise RuntimeError("provider unavailable")


@dataclass
class ImageCapturingAgent:
    calls: list[tuple[str, str, str, list[object]]]

    async def run_user_message(self, content: str, images: list[object] | None = None, delivery_route: str | None = None) -> AgentResponse:
        self.calls.append((content, "human", delivery_route or "primary", images or []))
        return AgentResponse(content="image-ok", steps=1)

    async def run(self, content: str, user_id: str, channel_id: str, images: list[object] | None = None) -> AgentResponse:
        self.calls.append((content, user_id, channel_id, images or []))
        return AgentResponse(content="image-ok", steps=1)


@dataclass
class DumpCapturingAgent:
    calls: int = 0
    dumped: list[str] | None = None

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content="should-not-run", steps=1)

    def dump_next_heartbeat_context(self, channel_id: str):
        from pathlib import Path

        self.dumped = [channel_id]
        return Path("/tmp/workspace/context_dumps/heartbeat.jsonl")


class FakeBackgroundTasks:
    calls: list[tuple[int, str | None]]

    def __init__(self) -> None:
        self.calls = []

    async def status_yaml(self, limit: int = 10, status: str | None = None) -> str:
        self.calls.append((limit, status))
        return (
            "background_agents:\n"
            f"  limit: {limit}\n"
            f"  status_filter: {status or 'all'}\n"
            "  count: 1\n"
            "  jobs:\n"
            "    - job_id: bg_test\n"
            "      status: running"
        )


@dataclass
class BackgroundAgentsCommandAgent:
    calls: int = 0
    background_tasks: FakeBackgroundTasks | None = None

    def __post_init__(self) -> None:
        self.background_tasks = FakeBackgroundTasks()

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls += 1
        return AgentResponse(content="should-not-run", steps=1)

    def bind_background_loop(self) -> None:
        return None


class FakeRuntimeConfig:
    def all(self) -> dict[str, str]:
        return {"openai_model": "runtime/model", "heartbeat_every_seconds": "3600"}


class FakeMemory:
    def get_last_contact(self) -> str:
        return "discord-channel"


class FakeSkills:
    def list(self) -> list[str]:
        return ["SKILLS", "playwright-cli"]


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
    memory = FakeMemory()
    skills = FakeSkills()
    cron = FakeCron()
    self_improvement = FakeSelfImprovement()
    tools = FakeTools()
    background_store = FakeBackgroundStore()
    current_model = "runtime/model"

    def candidate_models(self) -> list[str]:
        return ["runtime/model", "fallback/model"]

    def flash_candidate_models(self) -> list[str]:
        return ["flash/model", "flash/fallback"]


def test_auth_token_blocks_chat_before_agent(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/chat", json={"content": "hello"})

    assert response.status_code == 401
    assert fake.calls == 0
    get_settings.cache_clear()


def test_auth_token_allows_chat_with_bearer(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/chat",
        json={"content": "hello"},
        headers={"authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"content": "ok:hello:primary", "steps": 1}
    assert fake.calls == 1
    get_settings.cache_clear()


def test_chat_returns_controlled_error_when_agent_fails(monkeypatch) -> None:
    fake = FailingAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/chat", json={"content": "hello"})

    assert response.status_code == 500
    assert response.json()["detail"] == "provider unavailable"
    assert fake.calls == 1


def test_auth_token_blocks_local_discord_message(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "channel_id": "c",
                "author": {"id": ALLOWED_USER_ID, "bot": False},
                "content": "hello",
            },
        },
    )

    assert response.status_code == 401
    assert fake.calls == 0
    get_settings.cache_clear()


def test_local_discord_message_returns_controlled_error_when_agent_fails(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    get_settings.cache_clear()
    fake = FailingAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "channel_id": "c",
                "author": {"id": ALLOWED_USER_ID, "bot": False},
                "content": "hello",
            },
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "provider unavailable"
    assert fake.calls == 1
    get_settings.cache_clear()


def test_local_discord_message_passes_image_attachments(monkeypatch, tmp_path) -> None:
    fake = ImageCapturingAgent([])
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)
    monkeypatch.setattr(
        attachments_module,
        "_download_attachment",
        lambda url, content_type, max_bytes: (
            b"cat" if url.endswith("cat.png") else b"notes",
            content_type,
        ),
    )

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "channel_id": "c",
                "author": {"id": ALLOWED_USER_ID, "bot": False},
                "content": "what is this?",
                "attachments": [
                    {
                        "id": "a1",
                        "filename": "cat.png",
                        "content_type": "image/png",
                        "url": "https://cdn.discordapp.com/attachments/1/cat.png",
                    },
                    {
                        "id": "a2",
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "url": "https://cdn.discordapp.com/attachments/1/notes.txt",
                    },
                ],
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {"content": "image-ok", "steps": 1}
    assert fake.calls[0][0].startswith("what is this?\n\n[attached image file: sent_attachments/")
    assert "do not call inspect_image or read_file" in fake.calls[0][0]
    assert "cat.png; already included as an image" in fake.calls[0][0]
    assert "notes.txt]" in fake.calls[0][0]
    assert len(fake.calls[0][3]) == 1
    image = fake.calls[0][3][0]
    assert image.url == "data:image/png;base64,Y2F0"
    assert image.source_url.startswith("sent_attachments/")
    assert image.content_type == "image/png"
    assert image.filename == "cat.png"
    get_settings.cache_clear()


def test_local_discord_message_dump_context_intercepts_before_agent_run(monkeypatch, tmp_path) -> None:
    fake = DumpCapturingAgent()
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    monkeypatch.setenv("AGENT_WORKSPACE", "/tmp/workspace")
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "m1",
                "channel_id": "c",
                "author": {"id": ALLOWED_USER_ID, "bot": False},
                "content": "/dump_context",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {"content": "dumped next heartbeat context to context_dumps/heartbeat.jsonl", "steps": 0}
    assert fake.calls == 0
    assert fake.dumped == ["c"]
    get_settings.cache_clear()


def test_local_discord_message_background_agents_intercepts_before_agent_run(monkeypatch) -> None:
    fake = BackgroundAgentsCommandAgent()
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "m1",
                "channel_id": "c",
                "author": {"id": ALLOWED_USER_ID, "bot": False},
                "content": "/background_agents status=running limit=5",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["steps"] == 0
    assert response.json()["content"].startswith("```yaml\nbackground_agents:")
    assert fake.calls == 0
    assert fake.background_tasks.calls == [(5, "running")]
    get_settings.cache_clear()


def test_local_discord_message_rejects_unauthorized_user(monkeypatch) -> None:
    fake = ImageCapturingAgent([])
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/message",
        json={
            "t": "MESSAGE_CREATE",
            "d": {
                "channel_id": "c",
                "author": {"id": "not-allowed", "bot": False},
                "content": "hello",
            },
        },
    )

    assert response.status_code == 403
    assert fake.calls == []
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
    assert payload["heartbeat"]["last_delivery_route_configured"] is True
    assert payload["discord"]["gateway_enabled"] is True
    assert payload["discord"]["interactions_enabled"] is True
    assert payload["discord"]["client_secret_configured"] is True
    assert payload["security"]["api_auth_enabled"] is True
    assert payload["skills"] == ["SKILLS", "playwright-cli"]
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
