from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from confluence_reader.errors import ConfigError
from confluence_reader.setup_wizard import (
    build_config_payload,
    infer_mode,
    run_setup,
    save_config,
)


def test_infer_cloud_and_data_center() -> None:
    assert infer_mode("company.atlassian.net") == "cloud"
    assert infer_mode("https://company.atlassian.net/wiki") == "cloud"
    assert infer_mode("https://confluence.example.com/confluence") == "data_center"


def test_cloud_payload_uses_basic_auth() -> None:
    payload = build_config_payload(
        base_url="https://company.atlassian.net/wiki",
        mode="cloud",
        email="user@example.com",
        token="test-token",
        enable_writes=False,
    )
    assert payload["auth"] == "basic"
    assert payload["email"] == "user@example.com"
    assert payload["api_token"] == "test-token"
    assert payload["enable_writes"] is False


def test_data_center_payload_uses_bearer_auth() -> None:
    payload = build_config_payload(
        base_url="https://confluence.example.com/confluence",
        mode="data_center",
        token="test-pat",
    )
    assert payload["auth"] == "bearer"
    assert payload["personal_access_token"] == "test-pat"
    assert "email" not in payload


def test_cloud_payload_requires_email_and_token() -> None:
    with pytest.raises(ConfigError):
        build_config_payload(
            base_url="https://company.atlassian.net/wiki",
            mode="cloud",
            email="",
            token="test-token",
        )
    with pytest.raises(ConfigError):
        build_config_payload(
            base_url="https://company.atlassian.net/wiki",
            mode="cloud",
            email="user@example.com",
            token="",
        )


def test_save_config_is_private_and_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    saved = save_config(path, {"base_url": "https://example.com", "api_token": "secret"})
    assert saved == path.resolve()
    assert json.loads(path.read_text(encoding="utf-8"))["api_token"] == "secret"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_run_setup_saves_without_exposing_token(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "config.json"
    args = SimpleNamespace(
        config=str(config_path),
        base_url="https://company.atlassian.net/wiki",
        email="user@example.com",
        mode="auto",
        enable_writes=False,
        timeout=45,
        insecure=False,
        no_verify=False,
    )

    class FakeService:
        def __init__(self, config: object) -> None:
            self.config = config

        def status(self, *, probe: bool) -> dict[str, object]:
            assert probe is True
            return {
                "reachable": True,
                "user": {"displayName": "Test User"},
                "checks": [],
            }

    result = run_setup(
        args,
        input_func=lambda _: "",
        secret_func=lambda _: "secret-token",
        service_factory=FakeService,
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["api_token"] == "secret-token"
    assert payload["enable_writes"] is False
    assert result["reachable"] is True
    assert result["user"]["displayName"] == "Test User"
    assert "secret-token" not in json.dumps(result)
    assert "mcp_servers.confluence" in result["mcp_config"]
    assert "CONFLUENCE_CONFIG" in result["mcp_config"]


def test_run_setup_prompts_for_enable_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    args = SimpleNamespace(
        config=str(config_path),
        base_url="https://company.atlassian.net/wiki",
        email="user@example.com",
        mode="auto",
        enable_writes=None,
        timeout=30,
        insecure=False,
        no_verify=True,
    )
    answers = iter(["n"])
    result = run_setup(
        args,
        input_func=lambda _: next(answers),
        secret_func=lambda _: "secret-token",
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["enable_writes"] is False
    assert result["reachable"] is None
