from fastapi.testclient import TestClient

from pebble_shell.schemas import WebhookAcceptedResponse
from pebble_shell.server import app, get_agent, get_settings, set_agent


def test_webhook_ack_response_includes_event_status() -> None:
    payload = WebhookAcceptedResponse.model_validate(
        {"event_id": 7, "status": "received", "content": "accepted"}
    )

    assert payload.event_id == 7
    assert payload.status == "received"
    assert payload.content == "accepted"
    assert payload.steps == 0


def test_set_agent_overrides_cached_agent() -> None:
    first = object()
    second = object()

    set_agent(first)  # type: ignore[arg-type]
    assert get_agent() is first

    set_agent(second)  # type: ignore[arg-type]
    assert get_agent() is second

    delattr(app.state, "agent")
    get_agent.cache_clear()
