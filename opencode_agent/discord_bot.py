from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import discord

from .agent import CodingAgent
from .attachments import append_attachment_lines, save_discord_attachments
from .config import Settings
from .discord_interactions import split_discord_content
from .heartbeat import HeartbeatRunner

LOGGER = logging.getLogger(__name__)


async def run_discord_bot(settings: Settings, agent: CodingAgent | None = None) -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    agent = agent or CodingAgent(settings)
    agent.set_deliver(lambda channel_id, text: _deliver(client, channel_id, text))
    agent.tools.text_sender = lambda channel_id, text: _send_text_sync(client, channel_id, text)
    agent.tools.file_sender = lambda channel_id, path: _send_file_sync(client, channel_id, path)
    agent.background_tasks.set_deliver(lambda channel_id, text: _deliver(client, channel_id, text))
    heartbeat = HeartbeatRunner(agent, settings, deliver=lambda channel_id, text: _deliver(client, channel_id, text))

    @client.event
    async def on_ready() -> None:
        LOGGER.info("Discord bot connected as %s", client.user)
        if not hasattr(client, "_opencode_heartbeat_task"):
            client._opencode_heartbeat_task = client.loop.create_task(heartbeat.serve())  # type: ignore[attr-defined]

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        allowed_user_id = settings.discord_allowed_user_id.strip()
        if allowed_user_id and str(message.author.id) != allowed_user_id:
            LOGGER.info("Ignoring Discord message from unauthorized user %s", message.author.id)
            return
        if client.user and client.user not in message.mentions and not isinstance(message.channel, discord.DMChannel):
            return

        content = message.content
        if client.user:
            content = content.replace(client.user.mention, "").strip()
        if content.strip() == "/dump_context":
            path = agent.dump_next_heartbeat_context(str(message.channel.id))
            relative = path.relative_to(settings.agent_workspace)
            await message.reply(f"dumped next heartbeat context to `{relative}`", mention_author=False)
            return
        if _is_background_agents_command(content):
            agent.bind_background_loop()
            limit, status = _parse_background_agents_args(content)
            yaml = await agent.background_tasks.status_yaml(limit=limit, status=status)
            chunks = split_discord_content(f"```yaml\n{yaml}\n```")
            await message.reply(chunks[0], mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
            return
        saved = await asyncio.to_thread(
            save_discord_attachments,
            list(message.attachments),
            settings.agent_workspace,
            settings.discord_attachments_dir,
            str(message.channel.id),
            str(message.id),
            settings.max_discord_attachment_bytes,
        )
        content = append_attachment_lines(content, saved.lines)
        if await agent.enqueue_user_message(content, saved.images):
            return
        async with message.channel.typing():
            response = await agent.run_user_message(content, saved.images, delivery_route=str(message.channel.id))
        chunks = split_discord_content(response.content)
        await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)

    await client.start(settings.discord_bot_token)


def _is_background_agents_command(content: str) -> bool:
    return content.strip().split(maxsplit=1)[0] in {"/background_agents", "/background-agents", "/bg"}


def _parse_background_agents_args(content: str) -> tuple[int, str | None]:
    limit = 10
    status: str | None = None
    for part in content.strip().split()[1:]:
        if part.isdigit():
            limit = int(part)
            continue
        key, sep, value = part.partition("=")
        if not sep:
            key, sep, value = part.partition(":")
        key = key.lower().strip()
        value = value.strip()
        if key == "limit" and value.isdigit():
            limit = int(value)
        elif key == "status" and value:
            status = value
    return max(1, min(limit, 100)), status


async def _deliver(client: discord.Client, channel_id: str, text: str) -> None:
    channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    if hasattr(channel, "send"):
        for chunk in split_discord_content(text):
            await channel.send(chunk)


def _send_file_sync(client: discord.Client, channel_id: str, path: Path) -> str:
    future = asyncio.run_coroutine_threadsafe(_send_file(client, channel_id, path), client.loop)
    try:
        return future.result(timeout=90)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError(f"Discord file upload timed out after 90s for {path.name}") from None


def _send_text_sync(client: discord.Client, channel_id: str, text: str) -> str:
    future = asyncio.run_coroutine_threadsafe(_deliver(client, channel_id, text), client.loop)
    try:
        future.result(timeout=30)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError("Discord text send timed out after 30s") from None
    return "Sent progress message to the user"


async def _send_file(client: discord.Client, channel_id: str, path: Path, backoffs: Sequence[float] = (1, 2, 4)) -> str:
    attempts = len(backoffs) + 1
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            if not hasattr(channel, "send"):
                raise RuntimeError("Configured delivery route does not support sending files")
            await channel.send(file=discord.File(str(path), filename=path.name))
            return f"Sent {path.name} to the user"
        except Exception as exc:  # noqa: BLE001 - retry and report Discord/API failure details.
            last_error = exc
            LOGGER.warning(
                "discord_file_send_failed route=%s file=%s attempt=%s/%s error_type=%s error=%s",
                channel_id,
                path.name,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
            )
            if attempt < attempts:
                await asyncio.sleep(backoffs[attempt - 1])
    assert last_error is not None
    raise RuntimeError(f"Discord file upload failed after {attempts} attempts: {type(last_error).__name__}: {last_error}") from last_error
