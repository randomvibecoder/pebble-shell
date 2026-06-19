from opencode_agent.schemas import DiscordGatewayPayload
from fastapi.testclient import TestClient

from opencode_agent.server import _extract_message, app, get_agent, get_settings, set_agent


def test_extracts_discord_gateway_message() -> None:
    payload = DiscordGatewayPayload.model_validate(
        {
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "42",
                "channel_id": "abc",
                "author": {"id": "user-1", "username": "tester"},
                "content": "hello",
            },
        }
    )

    message = _extract_message(payload)

    assert message.channel_id == "abc"
    assert message.author.id == "user-1"
    assert message.content == "hello"


def test_public_route_serves_workspace_public_files(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public" / "demo"
    public_dir.mkdir(parents=True)
    (public_dir / "index.html").write_text("<h1>Demo</h1>", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    get_settings.cache_clear()

    response = TestClient(app).get("/public/demo/")

    assert response.status_code == 200
    assert "<h1>Demo</h1>" in response.text
    get_settings.cache_clear()


def test_public_route_rejects_hidden_files(tmp_path, monkeypatch) -> None:
    hidden_dir = tmp_path / "public" / ".secret"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "index.html").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    get_settings.cache_clear()

    response = TestClient(app).get("/public/.secret/index.html")

    assert response.status_code == 404
    get_settings.cache_clear()


def test_set_agent_overrides_cached_agent() -> None:
    first = object()
    second = object()

    set_agent(first)  # type: ignore[arg-type]
    assert get_agent() is first

    set_agent(second)  # type: ignore[arg-type]
    assert get_agent() is second

    delattr(app.state, "agent")
    get_agent.cache_clear()
