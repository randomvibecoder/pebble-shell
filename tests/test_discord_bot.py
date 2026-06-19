from __future__ import annotations

from pathlib import Path

import pytest

from pebble_shell.discord_bot import _send_file


class FakeClient:
    def __init__(self, channel: object) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int) -> object:
        return self.channel


class FlakyChannel:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def send(self, file: object) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary discord upload failure")


@pytest.mark.asyncio
async def test_send_file_retries_transient_discord_failures(tmp_path: Path) -> None:
    path = tmp_path / "tiny.txt"
    path.write_text("ok", encoding="utf-8")
    channel = FlakyChannel(failures=1)

    result = await _send_file(FakeClient(channel), "123", path, backoffs=(0, 0))

    assert result == "Sent tiny.txt to the user"
    assert channel.calls == 2


@pytest.mark.asyncio
async def test_send_file_reports_final_discord_failure_after_retries(tmp_path: Path) -> None:
    path = tmp_path / "tiny.txt"
    path.write_text("ok", encoding="utf-8")
    channel = FlakyChannel(failures=3)

    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        await _send_file(FakeClient(channel), "123", path, backoffs=(0, 0))

    assert channel.calls == 3
