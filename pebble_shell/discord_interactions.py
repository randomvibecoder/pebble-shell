from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from . import __version__


PING = 1
APPLICATION_COMMAND = 2
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
DISCORD_MESSAGE_LIMIT = 1900


@dataclass(frozen=True)
class InteractionMessage:
    content: str
    author_id: str


def verify_discord_signature(public_key: str, signature: str | None, timestamp: str | None, body: bytes) -> bool:
    if not public_key or not signature or not timestamp:
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
    except (BadSignatureError, ValueError):
        return False
    return True


def interaction_response(content: str) -> dict[str, Any]:
    return {
        "type": CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {"content": split_discord_content(content)[0]},
    }


def deferred_interaction_response() -> dict[str, Any]:
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE}


def send_interaction_followup(application_id: str, interaction_token: str, content: str) -> list[dict[str, Any]]:
    responses = []
    for chunk in split_discord_content(content):
        request = urllib.request.Request(
            f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}",
            data=json.dumps({"content": chunk}).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "user-agent": f"PebbleShell/{__version__}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                responses.append(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord interaction follow-up failed: HTTP {exc.code}: {detail}") from exc
    return responses


def split_discord_content(content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    text = content.strip() or "(no response)"
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    return chunks


def interaction_to_message(payload: dict[str, Any]) -> InteractionMessage:
    data = payload.get("data") or {}
    command_name = data.get("name") or "discord-command"
    options = data.get("options") or []
    option_values = _flatten_option_values(options)
    if command_name in {"background_agents", "background-agents", "bg"}:
        parts = ["/background_agents"]
        if "status" in option_values:
            parts.append(f"status={option_values['status']}")
        if "limit" in option_values:
            parts.append(f"limit={option_values['limit']}")
        prompt = " ".join(parts)
    else:
        prompt = _first_named(option_values, ("prompt", "message", "content", "question", "text"))
        if not prompt:
            prompt = json.dumps({"command": command_name, "options": option_values}, sort_keys=True)

    user = payload.get("user") or (payload.get("member") or {}).get("user") or {}
    author_id = str(user.get("id") or "discord-interaction-user")
    return InteractionMessage(content=str(prompt), author_id=author_id)


def _flatten_option_values(options: list[dict[str, Any]], prefix: str = "") -> dict[str, Any]:
    values: dict[str, Any] = {}
    for option in options:
        name = str(option.get("name") or "option")
        key = f"{prefix}.{name}" if prefix else name
        if "value" in option:
            values[key] = option["value"]
        nested = option.get("options")
        if isinstance(nested, list):
            values.update(_flatten_option_values(nested, key))
    return values


def _first_named(values: dict[str, Any], names: tuple[str, ...]) -> Any | None:
    lowered = {key.lower(): value for key, value in values.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    for key, value in lowered.items():
        if key.split(".")[-1] in names:
            return value
    return None
