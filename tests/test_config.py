from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from confluence_reader.config import Config, normalize_base_url
from confluence_reader.errors import ConfigError


def test_normalize_cloud_base_url() -> None:
    assert normalize_base_url("example.atlassian.net") == "https://example.atlassian.net/wiki"
    assert normalize_base_url("https://example.atlassian.net/wiki/") == (
        "https://example.atlassian.net/wiki"
    )


def test_base_url_cannot_embed_credentials() -> None:
    with pytest.raises(ConfigError):
        normalize_base_url("https://user:password@example.com/wiki")


def test_environment_precedes_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://file.example.com",
                "email": "file@example.com",
                "api_token": "file-token",
            }
        ),
        encoding="utf-8",
    )
    config = Config.from_cli_args(
        None,
        environ={
            "CONFLUENCE_CONFIG": str(config_path),
            "CONFLUENCE_BASE_URL": "https://env.example.com",
            "CONFLUENCE_EMAIL": "env@example.com",
            "CONFLUENCE_API_TOKEN": "env-token",
            "CONFLUENCE_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert config.base_url == "https://env.example.com"
    assert config.email == "env@example.com"
    assert config.auth == "basic"
    assert config.state_dir == (tmp_path / "state").resolve()


def test_anonymous_is_forced_read_only(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"base_url": "https://example.com", "auth": "none"}),
        encoding="utf-8",
    )
    config = Config.from_cli_args(
        None,
        environ={
            "CONFLUENCE_CONFIG": str(config_path),
            "CONFLUENCE_ENABLE_WRITES": "true",
        },
    )
    assert config.enable_writes is True
    assert config.writes_allowed is False


def test_write_switch_and_file_roots(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.com",
                "personal_access_token": "token",
                "auth": "bearer",
                "file_roots": [str(tmp_path / "allowed")],
            }
        ),
        encoding="utf-8",
    )
    config = Config.from_cli_args(
        None,
        environ={
            "CONFLUENCE_CONFIG": str(config_path),
            "CONFLUENCE_ENABLE_WRITES": "false",
        },
    )
    assert config.writes_allowed is False
    assert config.file_roots == [(tmp_path / "allowed").resolve()]


def test_file_roots_use_the_platform_path_separator(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    config = Config.from_cli_args(
        None,
        environ={
            "CONFLUENCE_BASE_URL": "https://example.com",
            "CONFLUENCE_AUTH": "none",
            "CONFLUENCE_FILE_ROOTS": os.pathsep.join([str(first), str(second)]),
        },
    )
    assert config.file_roots == [first.resolve(), second.resolve()]


def test_invalid_flavor_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Config.from_cli_args(
            None,
            environ={
                "CONFLUENCE_BASE_URL": "https://example.com",
                "CONFLUENCE_FLAVOR": "invalid",
                "CONFLUENCE_CONFIG": str(tmp_path / "missing.json"),
            },
        )
