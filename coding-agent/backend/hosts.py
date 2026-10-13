"""HTTP client for a daemon host, and the Docker commands that replace one."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DaemonError(Exception):
    """The daemon could not be reached or returned a transport-level failure."""


class DockerError(Exception):
    """A docker CLI command exited non-zero."""


@dataclass(frozen=True)
class Health:
    host_id: str
    boot_id: str
    base_ready: bool
    uptime_s: float


@dataclass(frozen=True)
class DaemonToolResult:
    text: str
    is_error: bool
    replayed: bool
    host_id: str
    boot_id: str
    op_id: str


def docker_run_args(name: str, port: int, host_id: str, cow_root: Path, image: str) -> list[str]:
    """Start a replacement container on the same port and the same Mac directory."""
    return [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--restart",
        "no",
        "-p",
        "127.0.0.1:%s:7700" % port,
        "-e",
        "HOST_ID=%s" % host_id,
        "-e",
        "COW_ROOT=/cow",
        "-e",
        "DUMMY_REPO=/opt/dummy-repo",
        "-e",
        "PORT=7700",
        "-v",
        "%s:/cow" % cow_root.resolve(),
        image,
    ]


def to_openai_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for spec in specs
    ]


class DaemonClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self, endpoint: str, timeout: float) -> Health:
        payload = await self._request("GET", "%s/health" % endpoint, timeout=timeout)
        try:
            return Health(
                host_id=str(payload["host_id"]),
                boot_id=str(payload["boot_id"]),
                base_ready=bool(payload["base_ready"]),
                uptime_s=float(payload["uptime_s"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DaemonError("health payload is incomplete") from exc

    async def ensure_workspace(self, endpoint: str, session_id: str, timeout: float) -> bool:
        payload = await self._request(
            "POST",
            "%s/workspaces" % endpoint,
            timeout=timeout,
            json={"session_id": session_id},
        )
        return bool(payload.get("created"))

    async def list_tools(self, endpoint: str, timeout: float) -> list[dict[str, Any]]:
        payload = await self._rpc(endpoint, "tools/list", {}, timeout)
        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise DaemonError("tools/list returned no tools")
        return tools

    async def call_tool(
        self,
        endpoint: str,
        session_id: str,
        op_id: str,
        name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> DaemonToolResult:
        payload = await self._rpc(
            endpoint,
            "tools/call",
            {"name": name, "arguments": arguments, "_meta": {"session_id": session_id, "op_id": op_id}},
            timeout,
        )
        content = payload.get("content")
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        text = ""
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = str(content[0].get("text", ""))
        return DaemonToolResult(
            text=text,
            is_error=bool(payload.get("isError")),
            replayed=bool(meta.get("replayed")),
            host_id=str(meta.get("host_id", "")),
            boot_id=str(meta.get("boot_id", "")),
            op_id=str(meta.get("op_id", op_id)),
        )

    async def _rpc(self, endpoint: str, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "%s/mcp" % endpoint,
            timeout=timeout,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if "error" in body:
            message = body["error"].get("message", "rpc error") if isinstance(body["error"], dict) else "rpc error"
            raise DaemonError(str(message))
        result = body.get("result")
        if not isinstance(result, dict):
            raise DaemonError("rpc result is missing")
        return result

    async def _request(self, method: str, url: str, timeout: float, json: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, url, json=json, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise DaemonError(str(exc)) from exc
        except ValueError as exc:
            raise DaemonError("response is not json") from exc
        if not isinstance(payload, dict):
            raise DaemonError("response is not an object")
        return payload


class DockerControl:
    def __init__(self, spawner: Any = None) -> None:
        self._spawn = spawner or asyncio.create_subprocess_exec

    async def remove(self, name: str) -> None:
        try:
            await self._run(["docker", "rm", "-f", name])
        except DockerError:
            logger.info("docker rm %s found nothing to remove", name)

    async def kill(self, name: str) -> None:
        await self._run(["docker", "kill", name])

    async def run(self, name: str, port: int, host_id: str, cow_root: Path, image: str) -> None:
        await self._run(docker_run_args(name, port, host_id, cow_root, image))

    async def _run(self, args: list[str]) -> None:
        process = await self._spawn(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout or b"").decode("utf-8", "replace").strip()
            raise DockerError(detail or "exit %s" % process.returncode)
