from opencode_agent.config import Settings
import opencode_agent.discord_register as discord_register
from opencode_agent.discord_register import _authorization, agent_command_payload, background_agents_command_payload, command_payloads, invite_url


def test_agent_command_payload() -> None:
    payload = agent_command_payload()

    assert payload["name"] == "agent"
    assert payload["type"] == 1
    assert payload["options"][0]["name"] == "prompt"
    assert payload["options"][0]["required"] is True


def test_background_agents_command_payload() -> None:
    payload = background_agents_command_payload()

    assert payload["name"] == "background_agents"
    assert payload["type"] == 1
    assert [option["name"] for option in payload["options"]] == ["status", "limit"]
    assert payload["options"][1]["max_value"] == 100


def test_command_payloads_include_agent_and_background_agents() -> None:
    assert [payload["name"] for payload in command_payloads()] == ["agent", "background_agents"]


def test_invite_url_includes_command_scope() -> None:
    url = invite_url("123", permissions="0")

    assert "client_id=123" in url
    assert "scope=bot+applications.commands" in url
    assert "permissions=0" in url


def test_authorization_prefers_bot_token() -> None:
    settings = Settings(openai_api_key="x", discord_bot_token="bot-token", discord_client_secret="client-secret")

    assert _authorization(settings) == "Bot bot-token"


def test_authorization_uses_client_credentials(monkeypatch) -> None:
    calls = {}

    def fake_client_credentials_token(client_id: str, client_secret: str) -> str:
        calls["client_id"] = client_id
        calls["client_secret"] = client_secret
        return "access-token"

    monkeypatch.setattr(discord_register, "_client_credentials_token", fake_client_credentials_token)
    settings = Settings(openai_api_key="x", discord_client_id="client-id", discord_client_secret="client-secret")

    assert _authorization(settings) == "Bearer access-token"
    assert calls == {"client_id": "client-id", "client_secret": "client-secret"}
