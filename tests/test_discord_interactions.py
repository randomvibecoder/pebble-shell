from fastapi.testclient import TestClient
from nacl.signing import SigningKey

import json
from dataclasses import dataclass

import pebble_shell.server as server
from pebble_shell.agent import AgentResponse
from pebble_shell.discord_interactions import (
    deferred_interaction_response,
    interaction_to_message,
    send_interaction_followup,
    split_discord_content,
    verify_discord_signature,
)
from pebble_shell.server import app, get_settings


ALLOWED_USER_ID = "111111111111111111"


@dataclass
class FakeInteractionAgent:
    calls: list[str]

    async def run_user_message(self, content: str) -> AgentResponse:
        self.calls.append(content)
        return AgentResponse(content=f"handled:{content}", steps=1)


class FakeInteractionBackgroundTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    async def status_yaml(self, limit: int = 10, status: str | None = None) -> str:
        self.calls.append((limit, status))
        return "background_agents:\n  count: 0\n  jobs:\n    []"


class FakeInteractionBackgroundAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.background_tasks = FakeInteractionBackgroundTasks()

    async def run_user_message(self, content: str) -> AgentResponse:
        self.calls.append(content)
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
            "member": {"user": {"id": "user-1"}},
            "data": {
                "name": "agent",
                "options": [{"name": "prompt", "value": "read README"}],
            },
        }
    )

    assert message.content == "read README"
    assert message.author_id == "user-1"


def test_interaction_to_message_uses_background_agents_command_options() -> None:
    message = interaction_to_message(
        {
            "type": 2,
            "member": {"user": {"id": "user-1"}},
            "data": {
                "name": "background_agents",
                "options": [{"name": "status", "value": "running"}, {"name": "limit", "value": 20}],
            },
        }
    )

    assert message.content == "/background_agents status=running limit=20"
    assert message.author_id == "user-1"


def _signed_headers(signing_key: SigningKey, body: bytes) -> dict[str, str]:
    timestamp = "1710000000"
    signature = signing_key.sign(timestamp.encode("utf-8") + body).signature.hex()
    return {
        "content-type": "application/json",
        "x-signature-ed25519": signature,
        "x-signature-timestamp": timestamp,
    }
