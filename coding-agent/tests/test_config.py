"""Step 1: settings loading and boot checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import ConfigError, load_settings


def _write_env(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_example_file_loads_live_defaults(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", _example_text())
    settings = load_settings(env_file, environ={})
    assert settings.llm_mode == "live"
    assert settings.cursor_base_url == "https://api.x.ai/v1"
    assert settings.cursor_model == "grok-4"
    assert settings.cursor_api_key == ""
    assert settings.max_steps == 30
    assert settings.lease_ttl_s == 10
    assert settings.migrate_after_s == 20
    assert settings.hosts[0].host_id == "host-a"
    assert settings.hosts[0].port == 7701
    assert settings.hosts[1].port == 7702
    assert settings.cow_root.name == "cow"


def _example_text() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / ".env.example").read_text(encoding="utf-8")


def test_process_env_overrides_file(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / ".env",
        "CURSOR_API_KEY=from-file\nCURSOR_MODEL=file-model\nMAX_STEPS=4\n",
    )
    settings = load_settings(
        env_file,
        environ={"CURSOR_API_KEY": "from-env", "MAX_STEPS": "12"},
    )
    assert settings.cursor_api_key == "from-env"
    assert settings.cursor_model == "file-model"
    assert settings.max_steps == 12


def test_max_rounds_is_accepted_when_max_steps_is_absent(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", "MAX_ROUNDS=7\n")
    settings = load_settings(env_file, environ={})
    assert settings.max_steps == 7


def test_relative_cow_root_resolves_under_project(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", "COW_ROOT=data/cow\n")
    settings = load_settings(env_file, environ={})
    assert settings.cow_root.is_absolute()
    assert settings.cow_root.parts[-2:] == ("data", "cow")


def test_endpoint_without_port_is_rejected(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", "HOST_A_ENDPOINT=http://127.0.0.1\n")
    with pytest.raises(ConfigError):
        load_settings(env_file, environ={})


def test_boot_requires_live_mode_and_a_key(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", "LLM_MODE=live\nCURSOR_API_KEY=\n")
    settings = load_settings(env_file, environ={})
    with pytest.raises(ConfigError, match="CURSOR_API_KEY"):
        settings.validate_for_boot()

    mocked = load_settings(env_file, environ={"LLM_MODE": "mock", "CURSOR_API_KEY": "k"})
    with pytest.raises(ConfigError, match="live"):
        mocked.validate_for_boot()

    ready = load_settings(env_file, environ={"CURSOR_API_KEY": "sk-test"})
    ready.validate_for_boot()


def test_comments_and_quotes_in_env_file(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / ".env",
        "# comment\n\nCURSOR_MODEL=\"grok-custom\"\nNOT_A_PAIR\n",
    )
    settings = load_settings(env_file, environ={})
    assert settings.cursor_model == "grok-custom"
