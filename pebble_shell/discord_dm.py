from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .config import Settings
from .discord_interactions import DISCORD_MESSAGE_LIMIT, split_discord_content

DISCORD_API = "https://discord.com/api/v10"


def send_dm(bot_token: str, user_id: str, content: str) -> list[dict[str, Any]]:
    if not bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required to send Discord DMs")
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("user_id is required")
    channel = _api_request(
        bot_token,
        "POST",
        "/users/@me/channels",
        {"recipient_id": user_id},
    )
    channel_id = str(channel.get("id") or "")
    if not channel_id:
        raise RuntimeError(f"Discord DM channel response missing id: {channel}")

    sent = []
    for chunk in split_discord_content(content, DISCORD_MESSAGE_LIMIT):
        sent.append(_api_request(bot_token, "POST", f"/channels/{channel_id}/messages", {"content": chunk}))
    return sent


def _api_request(bot_token: str, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{DISCORD_API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": f"Bot {bot_token}",
            "content-type": "application/json",
            "user-agent": f"PebbleShell/{__version__}",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord DM request failed: HTTP {exc.code}: {detail}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a Discord DM through the configured bot token.")
    parser.add_argument("--user-id", required=True, help="Discord user ID to DM.")
    parser.add_argument("--content", help="Message content to send.")
    parser.add_argument("--random-number", action="store_true", help="Send a random six-digit number.")
    args = parser.parse_args(argv)
    if args.random_number:
        content = str(100000 + secrets.randbelow(900000))
    elif args.content:
        content = args.content
    else:
        parser.error("set --content or --random-number")

    settings = Settings()
    try:
        result = send_dm(settings.discord_bot_token, args.user_id, content)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"sent": len(result), "content": content}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
