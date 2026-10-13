"""Claim queued sessions and keep the lease alive while each agent task runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from backend.config import Settings
from backend.store import Store

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


class Runner:
    def __init__(
        self,
        store: Store,
        agent: Any,
        owner: str,
        settings: Settings,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.store = store
        self.agent = agent
        self.owner = owner
        self.settings = settings
        self._sleep = sleeper
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = False

    async def loop(self) -> None:
        while not self._stop:
            active = [session_id for session_id, task in self._tasks.items() if not task.done()]
            slots = self.settings.max_concurrent - len(active)
            if slots > 0:
                claims = await asyncio.to_thread(
                    self.store.claim_sessions,
                    self.owner,
                    slots,
                    self.settings.lease_ttl_s,
                )
                for claim in claims:
                    existing = self._tasks.get(claim.session_id)
                    if existing is not None and not existing.done():
                        continue
                    logger.info("session %s claimed resumed=%s", claim.session_id, claim.resumed)
                    self._tasks[claim.session_id] = asyncio.create_task(self._guard(claim.session_id))
            await self._sleep(self.settings.runner_poll_s)

    async def renew_loop(self) -> None:
        while not self._stop:
            live = [session_id for session_id, task in self._tasks.items() if not task.done()]
            if live:
                await asyncio.to_thread(
                    self.store.renew_leases,
                    self.owner,
                    live,
                    self.settings.lease_ttl_s,
                )
            await self._sleep(self.settings.lease_renew_s)

    async def _guard(self, session_id: str) -> None:
        try:
            await self.agent.run(session_id)
        finally:
            self._tasks.pop(session_id, None)
