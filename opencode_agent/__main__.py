from __future__ import annotations

import asyncio
import logging

import uvicorn

from .agent import PRIMARY_CONVERSATION_ID, CodingAgent
from .config import Settings
from .cron import CronRunner
from .discord_bot import run_discord_bot
from .discord_dm import send_dm
from .heartbeat import HeartbeatRunner
from .server import app, set_agent


async def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper())

    automation_agent = CodingAgent(settings) if settings.openai_api_key else None
    if automation_agent:
        automation_agent.bind_background_loop()
        set_agent(automation_agent)

    config = uvicorn.Config(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    tasks = [asyncio.create_task(server.serve(), name="http-server")]
    if automation_agent:
        tasks.append(
            asyncio.create_task(
                CronRunner(automation_agent, automation_agent.cron).serve(settings.cron_poll_seconds),
                name="cron-runner",
            )
        )
        if not settings.discord_bot_token:
            tasks.append(
                asyncio.create_task(
                    HeartbeatRunner(automation_agent, settings).serve(),
                    name="heartbeat-runner",
                )
            )
    if settings.discord_bot_token:
        if automation_agent:
            tasks.append(asyncio.create_task(_send_initial_dm_once(settings, automation_agent), name="initial-dm"))
        tasks.append(asyncio.create_task(run_discord_bot(settings, automation_agent), name="discord-bot"))

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


async def _send_initial_dm_once(settings: Settings, agent: CodingAgent) -> None:
    user_id = settings.initial_dm_user_id.strip()
    message = settings.initial_dm_message.strip()
    if not user_id or not message:
        return

    marker_key = f"initial_dm_sent:{user_id}:{message}"
    if agent.memory.get_contact(marker_key):
        return

    sent = await asyncio.to_thread(send_dm, settings.discord_bot_token, user_id, message)
    channel_id = str((sent[0] if sent else {}).get("channel_id") or f"dm:{user_id}")
    agent.memory.add_message(PRIMARY_CONVERSATION_ID, "assistant", message)
    agent.memory.set_last_contact(channel_id)
    agent.memory.set_contact(marker_key, channel_id)


if __name__ == "__main__":
    asyncio.run(main())
