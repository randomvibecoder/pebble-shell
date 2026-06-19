from fastapi.testclient import TestClient
from nacl.signing import SigningKey

import json
from dataclasses import dataclass

import opencode_agent.server as server
from opencode_agent.agent import AgentResponse
from opencode_agent.discord_interactions import (
    deferred_interaction_response,
    interaction_to_message,
    send_interaction_followup,
    split_discord_content,
    verify_discord_signature,
)
from opencode_agent.server import app, get_settings


ALLOWED_USER_ID = "111111111111111111"


@dataclass
class FakeInteractionAgent:
    calls: list[tuple[str, str, str]]

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls.append((content, user_id, channel_id))
        return AgentResponse(content=f"handled:{content}:{user_id}:{channel_id}", steps=1)


class FakeInteractionBackgroundTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    async def status_yaml(self, limit: int = 10, status: str | None = None) -> str:
        self.calls.append((limit, status))
        return "background_agents:\n  count: 0\n  jobs:\n    []"


class FakeInteractionBackgroundAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.background_tasks = FakeInteractionBackgroundTasks()

    async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
        self.calls.append((content, user_id, channel_id))
        return AgentResponse(content="should-not-run", steps=1)

    def bind_background_loop(self) -> None:
        return None


def test_verifies_discord_signature() -> None:
    signing_key = SigningKey.generate()
    body = b'{"type":1}'
    timestamp = "1710000000"
    signature = signing_key.sign(timestamp.encode("utf-8") + body).signature.hex()

    assert verify_discord_signature(signing_key.verify_key.encode().hex(), signature, timestamp, body)
    assert not verify_discord_signature(signing_key.verify_key.encode().hex(), "00", timestamp, body)


def test_discord_interactions_ping(monkeypatch) -> None:
    signing_key = SigningKey.generate()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", signing_key.verify_key.encode().hex())
    get_settings.cache_clear()
    body = b'{"type":1}'
    headers = _signed_headers(signing_key, body)

    response = TestClient(app).post("/discord/interactions", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"type": 1}
    get_settings.cache_clear()


def test_discord_interactions_rejects_bad_signature(monkeypatch) -> None:
    signing_key = SigningKey.generate()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", signing_key.verify_key.encode().hex())
    get_settings.cache_clear()

    response = TestClient(app).post(
        "/discord/interactions",
        content=b'{"type":1}',
        headers={"x-signature-ed25519": "00", "x-signature-timestamp": "1710000000"},
    )

    assert response.status_code == 401
    get_settings.cache_clear()


def test_discord_interactions_requires_token_for_application_command(monkeypatch) -> None:
    signing_key = SigningKey.generate()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", signing_key.verify_key.encode().hex())
    get_settings.cache_clear()
    body = (
        b'{"type":2,"user":{"id":"'
        + ALLOWED_USER_ID.encode("utf-8")
        + b'"},"data":{"name":"agent","options":[{"name":"prompt","value":"hello"}]}}'
    )

    response = TestClient(app).post("/discord/interactions", content=body, headers=_signed_headers(signing_key, body))

    assert response.status_code == 400
    assert "token" in response.json()["detail"]
    get_settings.cache_clear()


def test_discord_interaction_test_runs_application_command(monkeypatch) -> None:
    fake = FakeInteractionAgent([])
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/interaction-test",
        json={
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": ALLOWED_USER_ID}},
            "data": {"name": "agent", "options": [{"name": "prompt", "value": "build a page"}]},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"content": "handled:build a page:human:chan-1", "steps": 1}
    assert fake.calls == [("build a page", "human", "chan-1")]
    get_settings.cache_clear()


def test_discord_interaction_test_background_agents_intercepts_before_agent_run(monkeypatch) -> None:
    fake = FakeInteractionBackgroundAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/interaction-test",
        json={
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": ALLOWED_USER_ID}},
            "data": {
                "name": "background_agents",
                "options": [{"name": "status", "value": "blocked"}, {"name": "limit", "value": 25}],
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["steps"] == 0
    assert response.json()["content"].startswith("```yaml\nbackground_agents:")
    assert fake.calls == []
    assert fake.background_tasks.calls == [(25, "blocked")]


def test_discord_interaction_test_returns_controlled_error_when_agent_fails(monkeypatch) -> None:
    class FailingInteractionAgent:
        async def run(self, content: str, user_id: str, channel_id: str) -> AgentResponse:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(server, "get_agent", lambda: FailingInteractionAgent())

    response = TestClient(app).post(
        "/discord/interaction-test",
        json={
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": ALLOWED_USER_ID}},
            "data": {"name": "agent", "options": [{"name": "prompt", "value": "build a page"}]},
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "provider unavailable"


def test_discord_interaction_test_rejects_non_commands(monkeypatch) -> None:
    fake = FakeInteractionAgent([])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post("/discord/interaction-test", json={"type": 1})

    assert response.status_code == 400
    assert fake.calls == []


def test_discord_interaction_test_rejects_unauthorized_user(monkeypatch) -> None:
    fake = FakeInteractionAgent([])
    monkeypatch.setenv("DISCORD_ALLOWED_USER_ID", ALLOWED_USER_ID)
    get_settings.cache_clear()
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/interaction-test",
        json={
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": "not-allowed"}},
            "data": {"name": "agent", "options": [{"name": "prompt", "value": "build a page"}]},
        },
    )

    assert response.status_code == 403
    assert fake.calls == []
    get_settings.cache_clear()


def test_discord_interaction_test_requires_auth(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    fake = FakeInteractionAgent([])
    monkeypatch.setattr(server, "get_agent", lambda: fake)

    response = TestClient(app).post(
        "/discord/interaction-test",
        json={
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": ALLOWED_USER_ID}},
            "data": {"name": "agent", "options": [{"name": "prompt", "value": "hello"}]},
        },
    )

    assert response.status_code == 401
    assert fake.calls == []
    get_settings.cache_clear()


def test_deferred_interaction_response() -> None:
    assert deferred_interaction_response() == {"type": 5}


def test_split_discord_content_preserves_long_content() -> None:
    content = ("alpha " * 500) + "\n\n" + ("beta " * 500)

    chunks = split_discord_content(content, limit=1000)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 1000 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(content.split())


def test_send_interaction_followup_posts_all_chunks(monkeypatch) -> None:
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b'{"id":"message"}'

    def fake_urlopen(request, timeout: int):
        assert timeout == 20
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    responses = send_interaction_followup("app-id", "token", "x" * 3900)

    assert responses == [{"id": "message"}, {"id": "message"}, {"id": "message"}]
    payloads = [json.loads(request.data.decode("utf-8")) for request in requests]
    assert "".join(payload["content"] for payload in payloads) == "x" * 3900
    assert all(len(payload["content"]) <= 1900 for payload in payloads)


def test_interaction_to_message_uses_prompt_option() -> None:
    message = interaction_to_message(
        {
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": "user-1"}},
            "data": {
                "name": "agent",
                "options": [{"name": "prompt", "value": "read README"}],
            },
        }
    )

    assert message.content == "read README"
    assert message.user_id == "user-1"
    assert message.channel_id == "chan-1"


def test_interaction_to_message_uses_background_agents_command_options() -> None:
    message = interaction_to_message(
        {
            "type": 2,
            "channel_id": "chan-1",
            "member": {"user": {"id": "user-1"}},
            "data": {
                "name": "background_agents",
                "options": [{"name": "status", "value": "running"}, {"name": "limit", "value": 20}],
            },
        }
    )

    assert message.content == "/background_agents status=running limit=20"
    assert message.user_id == "user-1"
    assert message.channel_id == "chan-1"


def _signed_headers(signing_key: SigningKey, body: bytes) -> dict[str, str]:
    timestamp = "1710000000"
    signature = signing_key.sign(timestamp.encode("utf-8") + body).signature.hex()
    return {
        "content-type": "application/json",
        "x-signature-ed25519": signature,
        "x-signature-timestamp": timestamp,
    }
