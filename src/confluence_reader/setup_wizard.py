"""Interactive first-run configuration for Confluence Agent."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from .config import Config, normalize_base_url
from .errors import ConfigError, ConfluenceReaderError
from .service import ConfluenceService

TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "confluence-reader" / "config.json"


def infer_mode(base_url: str) -> str:
    """Infer the Confluence deployment type from the normalized URL."""

    hostname = (urlparse(normalize_base_url(base_url)).hostname or "").lower()
    return "cloud" if hostname.endswith(".atlassian.net") else "data_center"


def build_config_payload(
    *,
    base_url: str,
    mode: str,
    email: str = "",
    token: str = "",
    enable_writes: bool = True,
    verify_ssl: bool = True,
    timeout: float = 60,
) -> dict[str, Any]:
    """Build a validated configuration object without exposing the secret."""

    normalized_url = normalize_base_url(base_url)
    mode = mode.strip().lower()
    if mode not in {"cloud", "data_center"}:
        raise ConfigError("Setup mode must be cloud or data_center")
    if not token.strip():
        raise ConfigError("An API token or personal access token is required")
    if timeout <= 0:
        raise ConfigError("Timeout must be greater than zero")

    payload: dict[str, Any] = {
        "base_url": normalized_url,
        "verify_ssl": verify_ssl,
        "timeout": float(timeout),
        "enable_writes": bool(enable_writes),
    }
    if mode == "cloud":
        if not email.strip():
            raise ConfigError("Confluence Cloud setup requires the account email")
        payload.update(
            {
                "email": email.strip(),
                "api_token": token.strip(),
                "auth": "basic",
            }
        )
    else:
        payload.update(
            {
                "personal_access_token": token.strip(),
                "auth": "bearer",
            }
        )
    return payload


def default_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_path = os.environ.get("CONFLUENCE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def save_config(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write the config atomically and restrict it to the current user."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    _restrict_permissions(path)
    return path.resolve()


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return

    user = getpass.getuser()
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{user}:(R,W)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise ConfigError(
            f"Configuration was written but its Windows permissions could not be restricted: {message}"
        )


def _prompt(
    label: str,
    *,
    default: str = "",
    input_func: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input_func(f"{label}{suffix}: ").strip()
    except EOFError as exc:
        raise ConfigError(
            "Setup needs an interactive terminal. Run `confluence setup` in a terminal."
        ) from exc
    return answer or default


def _secret_prompt(
    label: str,
    *,
    secret_func: Callable[[str], str] = getpass.getpass,
) -> str:
    if secret_func is getpass.getpass and not os.isatty(0):
        raise ConfigError(
            "Setup cannot safely read a token in this environment. "
            "Run `confluence setup` in an interactive terminal."
        )
    try:
        return secret_func(label).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise ConfigError("Setup was cancelled before a token was entered") from exc


def _read_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read the existing config at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"Existing config must be a JSON object: {path}")
    return payload


def _verification_environment(config_path: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CONFLUENCE_")
    }
    environment["CONFLUENCE_CONFIG"] = str(config_path)
    return environment


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mcp_config_snippet(config_path: Path | None = None) -> str:
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / "pyproject.toml").is_file():
        snippet = (
            "[mcp_servers.confluence]\n"
            'command = "uv"\n'
            "args = [\n"
            '  "run",\n'
            '  "--project",\n'
            f"  {_toml_string(str(project_root))},\n"
            '  "confluence-mcp"\n'
            "]\n"
            "startup_timeout_sec = 60\n"
            "tool_timeout_sec = 120"
        )
    else:
        snippet = (
            "[mcp_servers.confluence]\n"
            'command = "confluence-mcp"\n'
            "startup_timeout_sec = 60\n"
            "tool_timeout_sec = 120"
        )
    if config_path is not None:
        snippet += (
            "\n\n[mcp_servers.confluence.env]\n"
            f"CONFLUENCE_CONFIG = {_toml_string(str(config_path))}"
        )
    return snippet


def run_setup(
    args: SimpleNamespace,
    *,
    input_func: Callable[[str], str] = input,
    secret_func: Callable[[str], str] = getpass.getpass,
    service_factory: Callable[[Config], Any] = ConfluenceService,
) -> dict[str, Any]:
    """Collect credentials, save them, and optionally verify the connection."""

    target = default_config_path(getattr(args, "config", None))
    existing = _read_existing(target)
    base_url = getattr(args, "base_url", None) or existing.get("base_url")
    if not base_url:
        base_url = _prompt(
            "Confluence base URL (for example https://company.atlassian.net/wiki)",
            input_func=input_func,
        )
    base_url = normalize_base_url(str(base_url))

    mode = (getattr(args, "mode", "auto") or "auto").lower()
    if mode == "auto":
        mode = infer_mode(base_url)
    if mode not in {"cloud", "data_center"}:
        raise ConfigError("Setup mode must be auto, cloud, or data_center")

    email = getattr(args, "email", None) or existing.get("email") or ""
    if mode == "cloud" and not email:
        email = _prompt("Atlassian account email", input_func=input_func)

    token_label = (
        "Atlassian API token (input hidden): "
        if mode == "cloud"
        else "Confluence personal access token (input hidden): "
    )
    token = _secret_prompt(token_label, secret_func=secret_func)

    writes_option = getattr(args, "enable_writes", None)
    if writes_option is None:
        current = existing.get("enable_writes")
        default_writes = True if current is None else bool(current)
        answer = _prompt(
            "Enable create/update/comment/attachment writes? [Y/n]",
            default="y" if default_writes else "n",
            input_func=input_func,
        ).lower()
        writes_option = answer not in {"n", "no", "false", "0", "off"}

    timeout = float(getattr(args, "timeout", None) or existing.get("timeout") or 60)
    payload = build_config_payload(
        base_url=base_url,
        mode=mode,
        email=email,
        token=token,
        enable_writes=bool(writes_option),
        verify_ssl=not bool(getattr(args, "insecure", False)),
        timeout=timeout,
    )
    saved_path = save_config(target, payload)

    result: dict[str, Any] = {
        "ok": True,
        "config_path": str(saved_path),
        "base_url": payload["base_url"],
        "mode": mode,
        "auth": payload["auth"],
        "writes_enabled": payload["enable_writes"],
        "token_url": TOKEN_URL,
        "mcp_config": _mcp_config_snippet(saved_path),
    }

    if not getattr(args, "no_verify", False):
        verification_env = _verification_environment(saved_path)
        try:
            config = Config.from_cli_args(
                SimpleNamespace(
                    config=str(saved_path),
                    insecure=bool(getattr(args, "insecure", False)),
                ),
                environ=verification_env,
            )
            status = service_factory(config).status(probe=True)
            result["reachable"] = bool(status.get("reachable"))
            result["user"] = status.get("user")
            result["checks"] = status.get("checks", [])
        except ConfluenceReaderError as exc:
            result["reachable"] = False
            result["error"] = exc.to_dict()["error"]
        except Exception as exc:
            result["reachable"] = False
            result["error"] = {
                "code": "CONNECTION_CHECK_FAILED",
                "message": str(exc),
            }
    else:
        result["reachable"] = None

    overrides = sorted(
        key
        for key in os.environ
        if key.startswith("CONFLUENCE_")
        and key not in {"CONFLUENCE_CONFIG", "CONFLUENCE_STATE_DIR"}
    )
    if overrides:
        result["environment_overrides"] = overrides
    return result
