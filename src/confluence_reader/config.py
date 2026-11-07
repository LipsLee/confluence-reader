"""Configuration loading with environment and file precedence."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigError


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return value.strip() if value and value.strip() else None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "n"}


def _config_candidates(explicit: str | None, environ: Mapping[str, str]) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    result: list[Path] = []
    env_path = _env(environ, "CONFLUENCE_CONFIG")
    if env_path:
        result.append(Path(env_path).expanduser())
    result.append(Path.home() / ".config" / "confluence-reader" / "config.json")
    appdata = _env(environ, "APPDATA")
    if appdata:
        result.append(Path(appdata) / "confluence-reader" / "config.json")
    result.append(Path.home() / ".confluence-reader.json")
    return result


def _load_config_file(
    explicit: str | None, environ: Mapping[str, str]
) -> tuple[dict[str, Any], Path | None]:
    candidates = _config_candidates(explicit, environ)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Config file must contain a JSON object: {path}")
        return data, path
    if explicit:
        raise ConfigError(f"Config file not found: {Path(explicit).expanduser()}")
    return {}, None


def normalize_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ConfigError("Confluence base URL is empty")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"Invalid Confluence base URL: {value}")
    if parsed.username or parsed.password:
        raise ConfigError(
            "Do not put credentials in the Confluence base URL; "
            "use the token or config fields instead"
        )
    path = parsed.path.rstrip("/")
    if path in {"", "/"} and parsed.hostname and parsed.hostname.endswith(".atlassian.net"):
        path = "/wiki"
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ or os.environ
    if os.name == "nt":
        base = _env(env, "LOCALAPPDATA")
        if base:
            return Path(base) / "confluence-reader"
    return Path.home() / ".local" / "state" / "confluence-reader"


def _split_roots(value: Any, environ: Mapping[str, str]) -> list[Path]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif value is None or str(value).strip() == "":
        items = []
    else:
        separator = re.escape(os.pathsep)
        items = [
            item.strip()
            for item in re.split(rf"[{separator}\n]", str(value))
            if item.strip()
        ]
    result = [Path(item).expanduser().resolve() for item in items]
    return result


@dataclass
class Config:
    base_url: str
    email: str = ""
    api_token: str = ""
    personal_access_token: str = ""
    auth: str = "auto"
    verify_ssl: bool = True
    timeout: float = 30.0
    config_path: Path | None = None
    page_size: int = 100
    flavor: str = "auto"
    enable_writes: bool = True
    state_dir: Path = field(default_factory=default_state_dir)
    file_roots: list[Path] = field(default_factory=list)
    max_attachment_mb: float = 50.0

    @property
    def writes_allowed(self) -> bool:
        return self.enable_writes and self.auth != "none"

    @property
    def max_attachment_bytes(self) -> int:
        return max(1, int(self.max_attachment_mb * 1024 * 1024))

    @classmethod
    def from_cli_args(
        cls,
        args: Any | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> Config:
        env = environ or os.environ
        explicit_config = getattr(args, "config", None) if args is not None else None
        file_config, config_path = _load_config_file(explicit_config, env)

        base_url = (
            (getattr(args, "base_url", None) if args is not None else None)
            or _env(env, "CONFLUENCE_BASE_URL")
            or file_config.get("base_url")
            or file_config.get("CONFLUENCE_BASE_URL")
        )
        email = (
            _env(env, "CONFLUENCE_EMAIL")
            or file_config.get("email")
            or file_config.get("CONFLUENCE_EMAIL")
            or ""
        )
        api_token = (
            _env(env, "CONFLUENCE_API_TOKEN")
            or file_config.get("api_token")
            or file_config.get("CONFLUENCE_API_TOKEN")
            or ""
        )
        pat = (
            _env(env, "CONFLUENCE_PERSONAL_ACCESS_TOKEN")
            or file_config.get("personal_access_token")
            or file_config.get("CONFLUENCE_PERSONAL_ACCESS_TOKEN")
            or ""
        )
        auth = str(
            (getattr(args, "auth", None) if args is not None else None)
            or _env(env, "CONFLUENCE_AUTH")
            or file_config.get("auth")
            or "auto"
        ).strip().lower()
        verify_ssl = _as_bool(
            _env(env, "CONFLUENCE_VERIFY_SSL")
            if _env(env, "CONFLUENCE_VERIFY_SSL") is not None
            else file_config.get("verify_ssl"),
            True,
        )
        if args is not None and getattr(args, "insecure", False):
            verify_ssl = False
        timeout_value = (
            (getattr(args, "timeout", None) if args is not None else None)
            or _env(env, "CONFLUENCE_TIMEOUT")
            or file_config.get("timeout")
            or 30
        )
        try:
            timeout = float(timeout_value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid timeout: {timeout_value}") from exc
        if timeout <= 0:
            raise ConfigError("Timeout must be greater than zero")
        page_size_value = _env(env, "CONFLUENCE_PAGE_SIZE") or file_config.get("page_size") or 100
        try:
            page_size = max(1, min(100, int(page_size_value)))
        except (TypeError, ValueError):
            page_size = 100
        if not base_url:
            raise ConfigError(
                "Confluence is not configured. Set CONFLUENCE_BASE_URL and authentication, "
                "or create a config file. See references/setup.md."
            )
        base_url = normalize_base_url(str(base_url))

        if auth == "auto":
            if pat:
                auth = "bearer"
            elif email and api_token:
                auth = "basic"
            else:
                auth = "none"
        if auth not in {"basic", "bearer", "none"}:
            raise ConfigError("CONFLUENCE_AUTH must be auto, basic, bearer, or none")
        if auth == "basic" and (not email or not api_token):
            raise ConfigError(
                "Basic authentication requires CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN"
            )
        if auth == "bearer" and not (pat or api_token):
            raise ConfigError(
                "Bearer authentication requires CONFLUENCE_PERSONAL_ACCESS_TOKEN "
                "(or CONFLUENCE_API_TOKEN)"
            )

        flavor = str(
            (getattr(args, "flavor", None) if args is not None else None)
            or _env(env, "CONFLUENCE_FLAVOR")
            or file_config.get("flavor")
            or "auto"
        ).strip().lower()
        if flavor not in {"auto", "cloud", "data_center"}:
            raise ConfigError("CONFLUENCE_FLAVOR must be auto, cloud, or data_center")

        enable_writes = _as_bool(
            _env(env, "CONFLUENCE_ENABLE_WRITES")
            if _env(env, "CONFLUENCE_ENABLE_WRITES") is not None
            else file_config.get("enable_writes"),
            True,
        )
        state_value = (
            _env(env, "CONFLUENCE_STATE_DIR")
            or file_config.get("state_dir")
            or default_state_dir(env)
        )
        state_dir = Path(str(state_value)).expanduser().resolve()
        roots_value = _env(env, "CONFLUENCE_FILE_ROOTS")
        if roots_value is None:
            roots_value = file_config.get("file_roots")
        file_roots = _split_roots(roots_value, env)
        max_attachment_value = (
            _env(env, "CONFLUENCE_MAX_ATTACHMENT_MB")
            or file_config.get("max_attachment_mb")
            or 50
        )
        try:
            max_attachment_mb = float(max_attachment_value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid max attachment size: {max_attachment_value}") from exc
        if max_attachment_mb <= 0:
            raise ConfigError("max_attachment_mb must be greater than zero")

        return cls(
            base_url=base_url,
            email=str(email),
            api_token=str(api_token),
            personal_access_token=str(pat),
            auth=auth,
            verify_ssl=verify_ssl,
            timeout=timeout,
            config_path=config_path,
            page_size=page_size,
            flavor=flavor,
            enable_writes=enable_writes,
            state_dir=state_dir,
            file_roots=file_roots,
            max_attachment_mb=max_attachment_mb,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        return cls.from_cli_args(None, environ=environ)

    def public_summary(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "auth": self.auth,
            "flavor": self.flavor,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "config_file": str(self.config_path) if self.config_path else None,
            "writes_enabled": self.writes_allowed,
            "state_dir": str(self.state_dir),
            "file_roots": [str(path) for path in self.file_roots],
            "max_attachment_mb": self.max_attachment_mb,
        }
