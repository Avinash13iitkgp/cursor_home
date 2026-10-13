"""Pick a daemon host, watch its health, and replace the container when it dies."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.config import Settings
from backend.hosts import DaemonClient, DaemonError, DaemonToolResult, DockerControl, DockerError, to_openai_tools
from backend.store import Host, LeaseLost, Store

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
REPLACE_TIMEOUT_S = 30.0


class HostUnreachable(Exception):
    def __init__(self, host_id: str, reason: str) -> None:
        super().__init__("%s: %s" % (host_id, reason))
        self.host_id = host_id
        self.reason = reason


@dataclass(frozen=True)
class HostAssignment:
    host_id: str
    endpoint: str
    generation: int


class Scheduler:
    def __init__(
        self,
        store: Store,
        client: DaemonClient,
        docker: DockerControl,
        settings: Settings,
        owner: str,
        sleeper: Sleeper = asyncio.sleep,
        monotonic: Clock = time.monotonic,
    ) -> None:
        self.store = store
        self.client = client
        self.docker = docker
        self.settings = settings
        self.owner = owner
        self._sleep = sleeper
        self._monotonic = monotonic
        self._replacing: set[str] = set()
        self._recovery: set[asyncio.Task[None]] = set()
        self._stop = False
        self._tools: list[dict[str, Any]] | None = None

    def pick_host(self) -> Host | None:
        healthy = [host for host in self.store.list_hosts() if host.status == "healthy"]
        if not healthy:
            return None
        counts = self.store.active_counts()
        healthy.sort(key=lambda host: (counts.get(host.host_id, 0), host.host_id))
        return healthy[0]

    async def health_loop(self) -> None:
        while not self._stop:
            for host in self.store.list_hosts():
                action = await self.observe(host)
                if action == "replace":
                    task = asyncio.create_task(self.replace_host(host.host_id))
                    self._recovery.add(task)
                    task.add_done_callback(self._recovery.discard)
            await self._sleep(self.settings.health_interval_s)

    async def observe(self, host: Host) -> str:
        if host.host_id in self._replacing or host.status == "replacing":
            return "busy"
        if host.status == "down":
            return "replace"
        try:
            health = await self.client.health(host.endpoint, self.settings.health_timeout_s)
        except DaemonError:
            health = None
        if health is not None:
            self.store.record_health_success(host.host_id, health.boot_id)
            return "healthy"
        outcome = self.store.record_health_failure(host.host_id, self.settings.health_fail_threshold)
        if outcome == "down":
            logger.info("host %s down", host.host_id)
            return "replace"
        return outcome

    async def report_failure(self, host_id: str) -> None:
        """Probe immediately after a tool call could not reach the daemon."""
        host = self.store.get_host(host_id)
        try:
            health = await self.client.health(host.endpoint, self.settings.health_timeout_s)
        except DaemonError:
            health = None
        if health is not None:
            self.store.record_health_success(host_id, health.boot_id)
            return
        self.store.force_down(host_id)
        logger.info("host %s down", host_id)
        await self.replace_host(host_id)

    async def replace_host(self, host_id: str) -> None:
        if host_id in self._replacing:
            return
        self._replacing.add(host_id)
        try:
            host = self.store.get_host(host_id)
            previous_boot = host.boot_id or ""
            self.store.mark_replacing(host_id)
            logger.info("host %s replacing", host_id)
            await self.docker.remove(host.container_name)
            await self._start_container(host)
            deadline = self._monotonic() + REPLACE_TIMEOUT_S
            while self._monotonic() < deadline:
                try:
                    health = await self.client.health(host.endpoint, self.settings.health_timeout_s)
                except DaemonError:
                    health = None
                if health is not None and health.boot_id and health.boot_id != previous_boot:
                    replaced = self.store.mark_replaced(host_id, health.boot_id)
                    logger.info("host %s replaced generation %s", host_id, replaced.generation)
                    return
                await self._sleep(0.5)
            self.store.force_down(host_id)
            logger.info("host %s down", host_id)
        finally:
            self._replacing.discard(host_id)

    async def wait_for_host(self, session_id: str) -> HostAssignment:
        """Return this session's host once it is healthy, or move the session after the deadline."""
        started = self._monotonic()
        while True:
            session = self.store.get_session(session_id)
            if session.status in ("completed", "failed", "cancelled") or session.lease_owner != self.owner:
                raise LeaseLost(session_id)
            if session.host_id:
                host = self.store.get_host(session.host_id)
                if host.status == "healthy":
                    self.store.assign_host(self.owner, session_id, host.host_id, host.generation)
                    return HostAssignment(host.host_id, host.endpoint, host.generation)
            if self._monotonic() - started >= self.settings.migrate_after_s:
                other = self.pick_host()
                if other is not None and other.host_id != session.host_id:
                    self.store.migrate_host(self.owner, session_id, other.host_id, other.generation)
                    logger.info("session %s migrated %s -> %s", session_id, session.host_id, other.host_id)
                    return HostAssignment(other.host_id, other.endpoint, other.generation)
            await self._sleep(0.2)

    async def prepare(self, session_id: str) -> HostAssignment:
        session = self.store.get_session(session_id)
        if session.host_id is None:
            chosen = self.pick_host()
            if chosen is None:
                self.store.set_status(self.owner, session_id, "waiting_host")
                assignment = await self.wait_for_host(session_id)
            else:
                self.store.assign_host(self.owner, session_id, chosen.host_id, chosen.generation)
                assignment = HostAssignment(chosen.host_id, chosen.endpoint, chosen.generation)
        else:
            host = self.store.get_host(session.host_id)
            if host.status != "healthy":
                self.store.set_status(self.owner, session_id, "waiting_host")
                assignment = await self.wait_for_host(session_id)
            else:
                assignment = HostAssignment(host.host_id, host.endpoint, host.generation)
        await self.ensure_workspace(assignment.endpoint, session_id)
        return assignment

    async def ensure_workspace(self, endpoint: str, session_id: str) -> bool:
        try:
            return await self.client.ensure_workspace(endpoint, session_id, self.settings.tool_timeout_s)
        except DaemonError as exc:
            raise HostUnreachable(endpoint, str(exc)) from exc

    async def call_tool(
        self,
        host_id: str,
        session_id: str,
        op_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> DaemonToolResult:
        host = self.store.get_host(host_id)
        try:
            return await self.client.call_tool(
                host.endpoint,
                session_id,
                op_id,
                name,
                arguments,
                self.settings.tool_timeout_s,
            )
        except DaemonError as exc:
            raise HostUnreachable(host_id, str(exc)) from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is not None:
            return self._tools
        host = self.pick_host()
        if host is None:
            raise HostUnreachable("none", "no healthy host")
        try:
            specs = await self.client.list_tools(host.endpoint, self.settings.tool_timeout_s)
        except DaemonError as exc:
            raise HostUnreachable(host.host_id, str(exc)) from exc
        self._tools = to_openai_tools(specs)
        return self._tools

    async def _start_container(self, host: Host) -> None:
        try:
            await self.docker.run(
                host.container_name,
                host.port,
                host.host_id,
                self.settings.cow_root,
                self.settings.daemon_image,
            )
        except DockerError:
            await self.docker.remove(host.container_name)
            await self._sleep(0.5)
            await self.docker.run(
                host.container_name,
                host.port,
                host.host_id,
                self.settings.cow_root,
                self.settings.daemon_image,
            )
