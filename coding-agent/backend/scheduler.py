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
RESTART_DELAY_S = 20.0


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
        self._down_until: dict[str, float] = {}
        self._switch_after: dict[str, float] = {}
        self._recovery: set[asyncio.Task[None]] = set()
        self._stop = False
        self._tools: list[dict[str, Any]] | None = None

    def _restart_delay(self) -> float:
        return max(RESTART_DELAY_S, float(self.settings.migrate_after_s))

    def _held_down(self, host_id: str) -> bool:
        until = self._down_until.get(host_id)
        return until is not None and self._monotonic() < until

    def _arm_restart_hold(self, host_id: str) -> None:
        """Start the 20s clock once. Later calls do not extend it."""
        if host_id in self._down_until:
            return
        self._down_until[host_id] = self._monotonic() + self._restart_delay()

    async def _wait_restart_hold(self, host_id: str) -> None:
        self._arm_restart_hold(host_id)
        remaining = self._down_until[host_id] - self._monotonic()
        if remaining > 0:
            logger.info("host %s restart held %.0fs", host_id, remaining)
            await self._sleep(remaining)

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
                    self._move_sessions(host.host_id)
                    self._schedule_replace(host.host_id)
            self._rehome_stranded_sessions()
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
            self._switch_after.pop(host.host_id, None)
            self.store.record_health_success(host.host_id, health.boot_id)
            return "healthy"
        return self._note_failure(host.host_id)

    def _note_failure(self, host_id: str) -> str:
        """Count one missed heartbeat. Move sessions only after the threshold."""
        host = self.store.get_host(host_id)
        if host.status == "down":
            return "replace"
        if host.status == "replacing":
            return "replacing"
        outcome = self.store.record_health_failure(host_id, self.settings.health_fail_threshold)
        if outcome != "down":
            return outcome
        logger.info("host %s down", host_id)
        self._arm_restart_hold(host_id)
        self._move_sessions(host_id)
        self._schedule_replace(host_id)
        return "replace"

    async def report_failure(self, host_id: str) -> None:
        """Probe immediately after a tool call could not reach the daemon."""
        if self._held_down(host_id):
            return
        host = self.store.get_host(host_id)
        try:
            health = await self.client.health(host.endpoint, self.settings.health_timeout_s)
        except DaemonError:
            health = None
        if health is not None:
            self.store.record_health_success(host_id, health.boot_id)
            return
        self._note_failure(host_id)

    def _switch_ready(self, host_id: str) -> bool:
        ready_at = self._switch_after.get(host_id)
        if ready_at is None:
            return True
        return self._monotonic() >= ready_at

    async def abandon_host(self, host_id: str) -> None:
        """Start the clocks. The session moves after 3 heartbeats, not on the click."""
        self._arm_restart_hold(host_id)
        wait = self.settings.health_fail_threshold * float(self.settings.health_interval_s)
        self._switch_after[host_id] = self._monotonic() + wait

    def _schedule_replace(self, host_id: str, delay_s: float = 0) -> None:
        del delay_s
        if host_id in self._replacing:
            return
        self._replacing.add(host_id)
        task = asyncio.create_task(self.replace_host(host_id, claimed=True))
        self._recovery.add(task)
        task.add_done_callback(self._recovery.discard)

    def _peer(self, host_id: str | None) -> Host | None:
        healthy = [
            host
            for host in self.store.list_hosts()
            if host.host_id != host_id and host.status == "healthy" and not self._held_down(host.host_id)
        ]
        if not healthy:
            return None
        counts = self.store.active_counts()
        healthy.sort(key=lambda host: (counts.get(host.host_id, 0), host.host_id))
        return healthy[0]

    def _rehome_stranded_sessions(self) -> None:
        """A session left on a dead host moves as soon as some other host is healthy."""
        for host in self.store.list_hosts():
            if host.status == "suspect":
                continue
            if host.status == "healthy" and not self._held_down(host.host_id):
                continue
            self._move_sessions(host.host_id)

    def _move_sessions(self, host_id: str) -> None:
        if not self._switch_ready(host_id):
            return
        peer = self._peer(host_id)
        if peer is None:
            return
        for session in self.store.list_sessions():
            if session.host_id != host_id or session.status not in ("queued", "running", "waiting_host"):
                continue
            try:
                self.store.migrate_host(self.owner, session.session_id, peer.host_id, peer.generation)
            except LeaseLost:
                continue
            logger.info("session %s migrated %s -> %s", session.session_id, host_id, peer.host_id)

    async def replace_host(self, host_id: str, delay_s: float = 0, claimed: bool = False) -> None:
        del delay_s
        if not claimed:
            if host_id in self._replacing:
                return
            self._replacing.add(host_id)
        try:
            await self._wait_restart_hold(host_id)
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
                    self._down_until.pop(host_id, None)
                    self._rehome_stranded_sessions()
                    return
                await self._sleep(0.5)
            self.store.force_down(host_id)
            logger.info("host %s down", host_id)
        finally:
            self._replacing.discard(host_id)
            self._down_until.pop(host_id, None)

    async def wait_for_host(self, session_id: str) -> HostAssignment:
        """Stay through a short miss. Move only after the host is down."""
        while True:
            session = self.store.get_session(session_id)
            if session.status in ("completed", "failed", "cancelled") or session.lease_owner != self.owner:
                raise LeaseLost(session_id)
            assigned = self.store.get_host(session.host_id) if session.host_id else None
            assigned_ok = (
                assigned is not None
                and assigned.status == "healthy"
                and not self._held_down(assigned.host_id)
            )
            if assigned_ok:
                self.store.assign_host(self.owner, session_id, assigned.host_id, assigned.generation)
                return HostAssignment(assigned.host_id, assigned.endpoint, assigned.generation)
            # Down or replacing: leave after the heartbeat window, once another host can take the work.
            if assigned is None or assigned.status != "suspect":
                if session.host_id and not self._switch_ready(session.host_id):
                    await self._sleep(0.2)
                    continue
                peer = self._peer(session.host_id)
                if peer is not None:
                    self.store.migrate_host(self.owner, session_id, peer.host_id, peer.generation)
                    logger.info("session %s migrated %s -> %s", session_id, session.host_id, peer.host_id)
                    return HostAssignment(peer.host_id, peer.endpoint, peer.generation)
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
