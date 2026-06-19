from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__
from .config import Settings

DISCORD_API = "https://discord.com/api/v10"


def agent_command_payload() -> dict[str, Any]:
    return {
        "name": "agent",
        "description": "Send a task to Pebble Shell",
        "type": 1,
        "options": [
            {
                "name": "prompt",
                "description": "Task or question for the agent",
                "type": 3,
                "required": True,
            }
        ],
    }


def background_agents_command_payload() -> dict[str, Any]:
    return {
        "name": "background_agents",
        "description": "Show Pebble Shell background workers as YAML",
        "type": 1,
        "options": [
            {
                "name": "status",
                "description": "Optional job status filter, such as running, completed, blocked, or failed",
                "type": 3,
                "required": False,
            },
            {
                "name": "limit",
                "description": "Maximum number of recent jobs to show",
                "type": 4,
                "required": False,
                "min_value": 1,
                "max_value": 100,
            },
        ],
    }


def command_payloads() -> list[dict[str, Any]]:
    return [agent_command_payload(), background_agents_command_payload()]


def invite_url(client_id: str, permissions: str = "0") -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": "bot applications.commands",
            "permissions": permissions,
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"


def register_agent_command(settings: Settings, guild_id: str | None = None) -> dict[str, Any]:
    return _register_command(settings, agent_command_payload(), guild_id)


def register_agent_commands(settings: Settings, guild_id: str | None = None) -> list[dict[str, Any]]:
    return [_register_command(settings, payload, guild_id) for payload in command_payloads()]


def _register_command(settings: Settings, payload: dict[str, Any], guild_id: str | None = None) -> dict[str, Any]:
    authorization = _authorization(settings)
    if guild_id:
        path = f"/applications/{settings.discord_client_id}/guilds/{guild_id}/commands"
    else:
        path = f"/applications/{settings.discord_client_id}/commands"
    request = urllib.request.Request(
        f"{DISCORD_API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": authorization,
            "content-type": "application/json",
            "user-agent": f"PebbleShell/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord command registration failed: HTTP {exc.code}: {detail}") from exc


def _authorization(settings: Settings) -> str:
    if settings.discord_bot_token:
        return f"Bot {settings.discord_bot_token}"
    if settings.discord_client_id and settings.discord_client_secret:
        return f"Bearer {_client_credentials_token(settings.discord_client_id, settings.discord_client_secret)}"
    raise RuntimeError("Set DISCORD_BOT_TOKEN or both DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET")


def _client_credentials_token(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": "applications.commands.update",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{DISCORD_API}/oauth2/token",
        data=body,
        headers={
            "authorization": f"Basic {encoded}",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": f"PebbleShell/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord OAuth token request failed: HTTP {exc.code}: {detail}") from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Discord OAuth token response missing access_token: {payload}")
    return str(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the Pebble Shell Discord slash command.")
    parser.add_argument("--guild-id", help="Register as a guild command for immediate testing.")
    parser.add_argument("--print-invite", action="store_true", help="Print an OAuth invite URL and exit.")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.print_invite:
        print(invite_url(settings.discord_client_id))
        return 0
    result = register_agent_commands(settings, args.guild_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
