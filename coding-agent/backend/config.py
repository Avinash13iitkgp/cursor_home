"""Environment loading. Process environment overrides the .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

_HOSTS = (
    ("host-a", "HOST_A_ENDPOINT", "http://127.0.0.1:7701", "coding-agent-host-a"),
    ("host-b", "HOST_B_ENDPOINT", "http://127.0.0.1:7702", "coding-agent-host-b"),
)


class ConfigError(Exception):
    """The process cannot boot with this configuration."""


@dataclass(frozen=True)
class HostSpec:
    host_id: str
    endpoint: str
    container_name: str
    port: int


@dataclass(frozen=True)
class Settings:
    root: Path
    cursor_base_url: str
    cursor_api_key: str
    cursor_model: str
    llm_mode: str
    hosts: tuple[HostSpec, HostSpec]
    daemon_image: str
    backend_port: int
    supervisor_port: int
    max_steps: int
    lease_ttl_s: float
    lease_renew_s: float
    migrate_after_s: float
    health_interval_s: float
    health_timeout_s: float
    health_fail_threshold: int
    tool_timeout_s: float
    cow_root: Path
    db_path: Path
    runner_poll_s: float
    max_concurrent: int

    def validate_for_boot(self) -> None:
        if self.llm_mode != "live":
            raise ConfigError("LLM_MODE must be live")
        if not self.cursor_api_key.strip():
            raise ConfigError("CURSOR_API_KEY is required")


def load_settings(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    values: dict[str, str] = {}
    path = ROOT / ".env" if env_file is None else env_file
    if path.is_file():
        values.update(_parse_env(path.read_text(encoding="utf-8")))
    overlay = os.environ if environ is None else environ
    for key, raw in overlay.items():
        if raw.strip() != "":
            values[key] = raw.strip()

    hosts = tuple(_host(values, *spec) for spec in _HOSTS)
    if len(hosts) != 2:
        raise ConfigError("expected host-a and host-b")
    cow_root = _path(values, "COW_ROOT", ROOT / "data" / "cow")
    return Settings(
        root=ROOT if env_file is None else path.parent,
        cursor_base_url=_pick(values, "CURSOR_BASE_URL", "https://api.x.ai/v1").rstrip("/"),
        cursor_api_key=_pick(values, "CURSOR_API_KEY", ""),
        cursor_model=_pick(values, "CURSOR_MODEL", "grok-4"),
        llm_mode=_pick(values, "LLM_MODE", "live"),
        hosts=(hosts[0], hosts[1]),
        daemon_image=_pick(values, "DAEMON_IMAGE", "coding-agent-daemon:local"),
        backend_port=_integer(values, "BACKEND_PORT", 8080),
        supervisor_port=_integer(values, "SUPERVISOR_PORT", 8090),
        max_steps=_integer(values, "MAX_STEPS", _integer(values, "MAX_ROUNDS", 30)),
        lease_ttl_s=_number(values, "LEASE_TTL_S", 10),
        lease_renew_s=_number(values, "LEASE_RENEW_S", 3),
        migrate_after_s=_number(values, "MIGRATE_AFTER_S", 20),
        health_interval_s=_number(values, "HEALTH_INTERVAL_S", 2),
        health_timeout_s=_number(values, "HEALTH_TIMEOUT_S", 1.5),
        health_fail_threshold=_integer(values, "HEALTH_FAIL_THRESHOLD", 3),
        tool_timeout_s=_number(values, "TOOL_TIMEOUT_S", 60),
        cow_root=cow_root,
        db_path=_path(values, "DB_PATH", ROOT / "data" / "agent.db"),
        runner_poll_s=_number(values, "RUNNER_POLL_S", 1),
        max_concurrent=_integer(values, "MAX_CONCURRENT", 4),
    )


def _host(values: dict[str, str], host_id: str, env_key: str, default: str, container: str) -> HostSpec:
    endpoint = _pick(values, env_key, default)
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None or parsed.port is None:
        raise ConfigError("endpoint %s=%s needs an http URL with a port" % (env_key, endpoint))
    return HostSpec(host_id=host_id, endpoint=endpoint.rstrip("/"), container_name=container, port=parsed.port)


def _pick(values: dict[str, str], key: str, default: str) -> str:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().strip('"').strip("'")


def _integer(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError("%s must be an integer" % key) from exc


def _number(values: dict[str, str], key: str, default: float) -> float:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError("%s must be a number" % key) from exc


def _path(values: dict[str, str], key: str, default: Path) -> Path:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values
