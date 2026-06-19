import json

import pytest

from opencode_agent.discord_dm import main, send_dm


def test_send_dm_requires_bot_token() -> None:
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        send_dm("", "123", "hello")


def test_send_dm_creates_channel_and_sends_message(monkeypatch) -> None:
    requests = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout: int):
        requests.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        if request.full_url.endswith("/users/@me/channels"):
            return FakeResponse({"id": "dm-channel"})
        return FakeResponse({"id": "message-id", "content": requests[-1]["body"]["content"]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = send_dm("bot-token", "111111111111111111", "hello")

    assert len(result) == 1
    assert requests[0]["url"].endswith("/users/@me/channels")
    assert requests[0]["headers"]["Authorization"] == "Bot bot-token"
    assert requests[0]["headers"]["User-agent"] == "PebbleShell/0.0.1"
    assert requests[0]["body"] == {"recipient_id": "111111111111111111"}
    assert requests[1]["url"].endswith("/channels/dm-channel/messages")
    assert requests[1]["body"] == {"content": "hello"}


def test_dm_cli_reports_missing_bot_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    exit_code = main(["--user-id", "123", "--content", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "DISCORD_BOT_TOKEN" in captured.err
