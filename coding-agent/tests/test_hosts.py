"""Step 5: daemon HTTP client and the docker run command."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx

from backend.hosts import DaemonClient, DaemonError, DockerControl, DockerError, docker_run_args, to_openai_tools
from daemon.server import DaemonState, make_server

DUMMY = Path(__file__).resolve().parents[1] / "dummy-repo"


class _Proc:
    def __init__(self, code: int, err: bytes = b"") -> None:
        self.returncode = code
        self._err = err

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._err


def test_docker_run_args_bind_the_mac_directory(tmp_path: Path) -> None:
    args = docker_run_args("coding-agent-host-a", 7701, "host-a", tmp_path / "cow", "coding-agent-daemon:local")
    assert args[:4] == ["docker", "run", "-d", "--name"]
    assert "coding-agent-host-a" in args
    assert "127.0.0.1:7701:7700" in args
    assert "HOST_ID=host-a" in args
    assert "%s:/cow" % (tmp_path / "cow").resolve() in args
    assert args[-1] == "coding-agent-daemon:local"


def test_docker_remove_ignores_a_missing_container() -> None:
    async def scenario() -> None:
        seen: list[tuple[str, ...]] = []

        async def spawner(*args: str, stdout: int | None = None, stderr: int | None = None) -> _Proc:
            del stdout, stderr
            seen.append(args)
            return _Proc(1, b"No such container")

        docker = DockerControl(spawner)
        await docker.remove("coding-agent-host-a")
        assert seen[0][1:3] == ("rm", "-f")

    asyncio.run(scenario())


def test_docker_run_failure_raises() -> None:
    async def scenario() -> None:
        async def spawner(*args: str, stdout: int | None = None, stderr: int | None = None) -> _Proc:
            del args, stdout, stderr
            return _Proc(1, b"port in use")

        docker = DockerControl(spawner)
        try:
            await docker.run("coding-agent-host-a", 7701, "host-a", Path("/tmp/cow"), "image")
        except DockerError as exc:
            assert "port in use" in str(exc)
        else:
            raise AssertionError("expected DockerError")

    asyncio.run(scenario())


def test_openai_tool_shape() -> None:
    tools = to_openai_tools(
        [{"name": "read_file", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}]
    )
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "read_file"


def test_client_talks_to_the_daemon(tmp_path: Path) -> None:
    state = DaemonState("host-a", tmp_path / "cow", DUMMY)
    server = make_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "http://127.0.0.1:%s" % server.server_address[1]

    async def scenario() -> None:
        client = DaemonClient()
        try:
            health = await client.health(endpoint, timeout=2)
            assert health.host_id == "host-a"
            assert health.base_ready is True
            created = await client.ensure_workspace(endpoint, "s_client", timeout=2)
            assert created is True
            again = await client.ensure_workspace(endpoint, "s_client", timeout=2)
            assert again is False
            specs = await client.list_tools(endpoint, timeout=2)
            assert "write_file" in {spec["name"] for spec in specs}
            first = await client.call_tool(
                endpoint, "s_client", "s_client.0.0", "write_file", {"path": "note.txt", "content": "hello"}, 2
            )
            second = await client.call_tool(
                endpoint, "s_client", "s_client.0.0", "write_file", {"path": "note.txt", "content": "hello"}, 2
            )
            assert first.replayed is False
            assert second.replayed is True
            assert second.text == first.text
        finally:
            await client.aclose()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()


def test_client_turns_connection_errors_into_daemon_error() -> None:
    async def scenario() -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = DaemonClient(httpx.AsyncClient(transport=httpx.MockTransport(fail)))
        try:
            await client.health("http://127.0.0.1:9", timeout=1)
        except DaemonError as exc:
            assert "refused" in str(exc)
        else:
            raise AssertionError("expected DaemonError")
        finally:
            await client.aclose()

    asyncio.run(scenario())
