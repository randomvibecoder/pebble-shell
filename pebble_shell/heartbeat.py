from __future__ import annotations

import asyncio
import logging

from .agent import CodingAgent
from .config import Settings

LOGGER = logging.getLogger(__name__)


class HeartbeatRunner:
    def __init__(self, agent: CodingAgent, settings: Settings) -> None:
        self.agent = agent
        self.settings = settings
        self._stop = asyncio.Event()

    async def serve(self) -> None:
        if self.settings.heartbeat_every_seconds <= 0:
            LOGGER.info("Heartbeat disabled")
            return

        while not self._stop.is_set():
            interval = self._current_interval()
            if interval <= 0:
                LOGGER.info("Heartbeat disabled by runtime config")
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                await self.tick()

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> str:
        result = await self.agent.run_heartbeat()
        if result.should_notify and self.agent._deliver:
            await self.agent._deliver(result.content)
        return result.content

    def _current_interval(self) -> int:
        configured = self.agent.runtime_config.get("heartbeat_every_seconds")
        return int(configured) if configured is not None else self.settings.heartbeat_every_seconds
