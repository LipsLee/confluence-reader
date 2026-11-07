#!/usr/bin/env python3
"""Read-only command-line client for Confluence Cloud and Data Center."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

try:
    from storage_markdown import storage_to_markdown, strip_html
except ImportError:  # pragma: no cover - direct execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from storage_markdown import storage_to_markdown, strip_html


VERSION = "1.0.0"
USER_AGENT = f"codex-confluence-reader/{VERSION}"
PAGE_EXPAND = "space,version,ancestors,metadata.labels,body.storage"

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


class CliError(RuntimeError):
    """An expected user-facing error."""


class ConfigError(CliError):
    """Invalid or missing configuration."""


class ConfluenceError(CliError):
    """An HTTP or API error returned by Confluence."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def _origin_of(value: str) -> str:
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return f"{scheme}://{netloc}"


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Keep authenticated redirects on the configured Confluence origin."""

    def __init__(self, origin: str) -> None:
        self.origin = origin.rstrip("/")

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        target = urljoin(req.full_url, newurl)
        if _origin_of(target) != self.origin:
            raise HTTPError(
                target,
                code,
                "Refusing to forward Confluence credentials to another origin",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "n"}


def _config_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    result: list[Path] = []
    env_path = _env("CONFLUENCE_CONFIG")
    if env_path:
        result.append(Path(env_path).expanduser())
    result.append(Path.home() / ".config" / "confluence-reader" / "config.json")
    appdata = _env("APPDATA")
    if appdata:
        result.append(Path(appdata) / "confluence-reader" / "config.json")
    result.append(Path.home() / ".confluence-reader.json")
    return result


def _load_config_file(explicit: str | None) -> tuple[dict[str, Any], Path | None]:
    candidates = _config_candidates(explicit)
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
    path = parsed.path.rstrip("/")
    if path in {"", "/"} and parsed.hostname and parsed.hostname.endswith(".atlassian.net"):
        path = "/wiki"
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{parsed.scheme}://{parsed.netloc}{path}"


@dataclass
class Config:
    base_url: str
    email: str
    api_token: str
    personal_access_token: str
    auth: str
    verify_ssl: bool
    timeout: float
    config_path: Path | None
    page_size: int

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        file_config, config_path = _load_config_file(getattr(args, "config", None))

        base_url = (
            getattr(args, "base_url", None)
            or _env("CONFLUENCE_BASE_URL")
            or file_config.get("base_url")
            or file_config.get("CONFLUENCE_BASE_URL")
        )
        email = (
            _env("CONFLUENCE_EMAIL")
            or file_config.get("email")
            or file_config.get("CONFLUENCE_EMAIL")
            or ""
        )
        api_token = (
            _env("CONFLUENCE_API_TOKEN")
            or file_config.get("api_token")
            or file_config.get("CONFLUENCE_API_TOKEN")
            or ""
        )
        pat = (
            _env("CONFLUENCE_PERSONAL_ACCESS_TOKEN")
            or file_config.get("personal_access_token")
            or file_config.get("CONFLUENCE_PERSONAL_ACCESS_TOKEN")
            or ""
        )
        auth = str(
            getattr(args, "auth", None)
            or _env("CONFLUENCE_AUTH")
            or file_config.get("auth")
            or "auto"
        ).strip().lower()
        verify_ssl = _as_bool(
            _env("CONFLUENCE_VERIFY_SSL")
            if _env("CONFLUENCE_VERIFY_SSL") is not None
            else file_config.get("verify_ssl"),
            True,
        )
        if getattr(args, "insecure", False):
            verify_ssl = False
        timeout_value = (
            getattr(args, "timeout", None)
            or _env("CONFLUENCE_TIMEOUT")
            or file_config.get("timeout")
            or 30
        )
        try:
            timeout = float(timeout_value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid timeout: {timeout_value}") from exc
        if timeout <= 0:
            raise ConfigError("Timeout must be greater than zero")
        page_size_value = _env("CONFLUENCE_PAGE_SIZE") or file_config.get("page_size") or 100
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
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "auth": self.auth,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "config_file": str(self.config_path) if self.config_path else None,
        }


class ConfluenceClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = config.base_url
        self.origin = _origin_of(self.base_url)
        self.ssl_context = ssl.create_default_context()
        if not config.verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        self.headers = {
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if config.auth == "basic":
            credentials = f"{config.email}:{config.api_token}".encode("utf-8")
            encoded = base64.b64encode(credentials).decode("ascii")
            self.headers["Authorization"] = f"Basic {encoded}"
        elif config.auth == "bearer":
            token = config.personal_access_token or config.api_token
            self.headers["Authorization"] = f"Bearer {token}"
        self.opener = build_opener(
            _SameOriginRedirectHandler(self.origin),
            HTTPSHandler(context=self.ssl_context),
        )

    def api_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.base_url}{path_or_url}"

    def web_url(self, link: str | None) -> str:
        if not link:
            return ""
        if link.startswith(("http://", "https://")):
            return link
        base_path = urlparse(self.base_url).path.rstrip("/")
        if link.startswith("/"):
            if base_path and link.startswith(f"{base_path}/"):
                return f"{self.origin}{link}"
            if link.startswith("/wiki/") and base_path != "/wiki":
                return f"{self.origin}{link}"
            if base_path and link.startswith(("/api/", "/rest/", "/spaces/", "/pages/", "/download/")):
                return f"{self.base_url}{link}"
            return f"{self.origin}{link}"
        return urljoin(self.base_url + "/", link)

    def _resolve_next(self, next_link: str, current_url: str) -> str:
        if next_link.startswith(("http://", "https://")):
            return next_link
        if next_link.startswith("/"):
            base_path = urlparse(self.base_url).path.rstrip("/")
            if base_path and next_link.startswith(("/api/", "/rest/", "/download/", "/spaces/", "/pages/")):
                return f"{self.base_url}{next_link}"
            return f"{self.origin}{next_link}"
        return urljoin(current_url, next_link)

    def _request(
        self,
        url: str,
        *,
        accept: str = "application/json",
        attempts: int = 3,
    ) -> tuple[bytes, dict[str, str]]:
        headers = dict(self.headers)
        headers["Accept"] = accept
        request = Request(url, headers=headers, method="GET")
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    return response.read(), dict(response.headers.items())
            except HTTPError as exc:
                body = exc.read()
                status = exc.code
                if status in {429, 500, 502, 503, 504} and attempt < attempts:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        sleep_for = max(delay, float(retry_after)) if retry_after else delay
                    except ValueError:
                        sleep_for = delay
                    time.sleep(min(sleep_for, 10.0))
                    delay *= 2
                    continue
                message = self._error_message(body, status, fallback=exc.msg)
                raise ConfluenceError(message, status=status, url=url) from exc
            except URLError as exc:
                if attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                reason = getattr(exc, "reason", exc)
                raise ConfluenceError(f"Cannot reach Confluence at {self.base_url}: {reason}") from exc
            except TimeoutError as exc:
                if attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ConfluenceError(f"Confluence request timed out: {url}") from exc
            except ssl.SSLError as exc:
                raise ConfluenceError(f"TLS error while contacting {self.base_url}: {exc}") from exc
        raise ConfluenceError(f"Confluence request failed after retries: {url}")

    @staticmethod
    def _error_message(body: bytes, status: int, *, fallback: str = "") -> str:
        message = ""
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                message = str(
                    data.get("message")
                    or data.get("error")
                    or " ".join(str(item) for item in data.get("errorMessages", []) or [])
                )
        except (json.JSONDecodeError, UnicodeError):
            message = body.decode("utf-8", errors="replace").strip()[:500]
        if not message:
            message = (fallback or "").strip()
        if status == 401:
            message = message or "Authentication failed"
            message += " (check email/API token or personal access token)"
        elif status == 403:
            message = message or "Permission denied"
            message += " (the account cannot view this Confluence content)"
        elif status == 404:
            message = message or "Confluence resource not found"
        elif status == 429:
            message = message or "Confluence rate limit reached"
        return f"HTTP {status}: {message}".strip()

    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        url = self.api_url(path_or_url)
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params, doseq=True)}"
        body, _ = self._request(url)
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ConfluenceError(f"Expected JSON from {url}") from exc

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        query = dict(params or {})
        page_size = min(self.config.page_size, max(1, int(limit)), 100)
        query["limit"] = page_size
        url = self.api_url(path)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(query, doseq=True)}"
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                break
            seen.add(url)
            data = self._page_json(url)
            batch = data.get("results") or []
            if not isinstance(batch, list):
                break
            for item in batch:
                if isinstance(item, dict):
                    results.append(item)
                    if max_items is not None and len(results) >= max_items:
                        return results
            next_link = (data.get("_links") or {}).get("next")
            url = self._resolve_next(str(next_link), url) if next_link else ""
        return results

    def _page_json(self, url: str) -> dict[str, Any]:
        body, _ = self._request(url)
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ConfluenceError(f"Expected JSON from {url}") from exc
        return data if isinstance(data, dict) else {}

    def doctor(self) -> dict[str, Any]:
        result = self.config.public_summary()
        checks: list[dict[str, Any]] = []
        user: Any = None
        try:
            user = self.get_json("/rest/api/user/current")
            checks.append({"name": "current_user", "ok": True})
        except ConfluenceError as exc:
            checks.append({"name": "current_user", "ok": False, "error": str(exc)})
            result["reachable"] = exc.status is not None
            result["checks"] = checks
            return result
        try:
            spaces = self.paginate("/rest/api/space", limit=1, max_items=1)
            checks.append({"name": "space_list", "ok": True, "sample_space_key": spaces[0].get("key") if spaces else None})
        except ConfluenceError as exc:
            checks.append({"name": "space_list", "ok": False, "error": str(exc)})
        user_ok = bool(checks and checks[0].get("ok"))
        result.update(
            {
                "reachable": user_ok,
                "api": "v1",
                "user": {
                    "displayName": user.get("displayName") if isinstance(user, dict) else None,
                    "username": user.get("username") if isinstance(user, dict) else None,
                    "accountId": user.get("accountId") if isinstance(user, dict) else None,
                },
                "checks": checks,
            }
        )
        return result

    def get_page_by_id(self, page_id: str) -> dict[str, Any]:
        page_id = str(page_id).strip()
        if not page_id.isdigit():
            raise CliError(f"Invalid Confluence page id: {page_id}")
        data = self.get_json(
            f"/rest/api/content/{quote(page_id)}",
            {"expand": PAGE_EXPAND},
        )
        return self.normalize_page(data)

    def get_page_by_title(self, space: str, title: str) -> dict[str, Any]:
        data = self.get_json(
            "/rest/api/content",
            {
                "spaceKey": space,
                "title": title,
                "type": "page",
                "limit": 1,
                "expand": PAGE_EXPAND,
            },
        )
        results = data.get("results") or []
        if not results:
            raise ConfluenceError(
                f"Page not found in space {space}: {title}",
                status=404,
            )
        return self.normalize_page(results[0])

    def follow_redirects(self, url: str) -> str:
        if _origin_of(url) != self.origin:
            raise CliError(
                "Refusing to open a URL outside the configured Confluence origin: "
                f"{url}"
            )
        headers = dict(self.headers)
        headers["Accept"] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
        request = Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                return response.geturl()
        except HTTPError as exc:
            raise ConfluenceError(
                self._error_message(exc.read(), exc.code, fallback=exc.msg),
                status=exc.code,
                url=url,
            ) from exc
        except URLError as exc:
            raise ConfluenceError(f"Cannot open Confluence URL: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ConfluenceError(f"Confluence URL timed out: {url}") from exc
        except ssl.SSLError as exc:
            raise ConfluenceError(f"TLS error while opening {url}: {exc}") from exc

    def resolve_page(self, ref: str, space: str | None = None) -> dict[str, Any]:
        ref = ref.strip()
        if not ref:
            raise CliError("Page reference is empty")
        page_id, space_from_url, title_from_url = parse_page_ref(ref)
        if page_id:
            return self.get_page_by_id(page_id)
        if space_from_url:
            space = space_from_url
        if title_from_url:
            title = title_from_url
        elif ref.startswith(("http://", "https://")):
            final_url = self.follow_redirects(ref)
            final_id, final_space, final_title = parse_page_ref(final_url)
            if final_id:
                return self.get_page_by_id(final_id)
            if final_space:
                space = space or final_space
            if final_title:
                title = final_title
            else:
                raise CliError(
                    "Could not extract a page id from this Confluence URL. "
                    "Open the canonical page and copy its /pages/<id>/ URL."
                )
        elif "/" in ref and not ref.startswith(("http://", "https://")):
            space_candidate, title = ref.split("/", 1)
            space = space or space_candidate.strip()
            title = title.strip()
        else:
            title = ref
        if space:
            return self.get_page_by_title(space, title)

        results = self.search(cql=f'title = "{cql_escape(title)}" and type = page', max_items=20)
        if not results:
            raise ConfluenceError(f"Page not found by title: {title}", status=404)
        if len(results) > 1:
            candidates = ", ".join(
                f"{item.get('title')} ({item.get('id')}, space {item.get('space', {}).get('key', '?')})"
                for item in results[:8]
            )
            raise CliError(
                f"Multiple pages match '{title}'. Pass --space. Candidates: {candidates}"
            )
        return self.get_page_by_id(str(results[0]["id"]))

    def search(
        self,
        *,
        cql: str,
        max_items: int = 25,
    ) -> list[dict[str, Any]]:
        items = self.paginate(
            "/rest/api/search",
            {
                "cql": cql,
                "expand": "content.space,content.version",
            },
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        return [self.normalize_search_result(item) for item in items]

    def get_children(
        self,
        page_id: str,
        *,
        recursive: bool = False,
        max_items: int = 500,
    ) -> list[dict[str, Any]]:
        suffix = "descendant/page" if recursive else "child/page"
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/{suffix}",
            {"expand": "space,version,ancestors"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        children = [self.normalize_summary(item) for item in items]
        if recursive:
            children.sort(
                key=lambda item: (
                    len(item.get("ancestors") or []),
                    str(item.get("title") or "").casefold(),
                )
            )
        return children

    def get_attachments(self, page_id: str, *, max_items: int = 500) -> list[dict[str, Any]]:
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/child/attachment",
            {"expand": "version"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        return [self.normalize_attachment(item) for item in items]

    def get_comments(self, page_id: str, *, max_items: int = 500) -> list[dict[str, Any]]:
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/child/comment",
            {"expand": "body.storage,version,history"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        comments: list[dict[str, Any]] = []
        for item in items:
            version = item.get("version") or {}
            history = item.get("history") or {}
            author = (
                (version.get("by") or {}).get("displayName")
                or (history.get("createdBy") or {}).get("displayName")
                or ""
            )
            body = ((item.get("body") or {}).get("storage") or {}).get("value") or ""
            comments.append(
                {
                    "id": str(item.get("id", "")),
                    "title": item.get("title", ""),
                    "author": author,
                    "created": history.get("createdDate") or version.get("when"),
                    "body_storage": body,
                    "body_markdown": storage_to_markdown(body, self.base_url),
                    "url": self.web_url((item.get("_links") or {}).get("webui")),
                }
            )
        return comments

    def download_attachment(
        self,
        attachment: dict[str, Any],
        destination: Path,
        *,
        max_bytes: int,
    ) -> int:
        url = str(attachment.get("download_url") or "")
        if not url:
            raise CliError(f"Attachment has no download URL: {attachment.get('filename')}")
        headers = dict(self.headers)
        headers["Accept"] = "*/*"
        request = Request(url, headers=headers, method="GET")
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response, destination.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        output.close()
                        destination.unlink(missing_ok=True)
                        raise CliError(
                            f"Attachment exceeds limit ({format_size(max_bytes)}): "
                            f"{attachment.get('filename')}"
                        )
                    output.write(chunk)
        except HTTPError as exc:
            raise ConfluenceError(
                self._error_message(exc.read(), exc.code, fallback=exc.msg),
                status=exc.code,
                url=url,
            ) from exc
        except URLError as exc:
            raise ConfluenceError(f"Cannot download attachment: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ConfluenceError(
                f"Attachment download timed out: {attachment.get('filename')}"
            ) from exc
        except ssl.SSLError as exc:
            raise ConfluenceError(
                f"TLS error while downloading attachment: {exc}"
            ) from exc
        return written

    def normalize_page(self, data: dict[str, Any]) -> dict[str, Any]:
        space = data.get("space") or {}
        version = data.get("version") or {}
        labels_container = (data.get("metadata") or {}).get("labels") or {}
        labels = [
            str(label.get("name"))
            for label in labels_container.get("results") or []
            if isinstance(label, dict) and label.get("name")
        ]
        ancestors = [self.normalize_summary(item) for item in data.get("ancestors") or []]
        body = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        return {
            "id": str(data.get("id", "")),
            "type": data.get("type", "page"),
            "status": data.get("status", ""),
            "title": data.get("title", ""),
            "space": {
                "id": str(space.get("id", "")),
                "key": space.get("key", ""),
                "name": space.get("name", ""),
            },
            "version": {
                "number": version.get("number"),
                "message": version.get("message", ""),
                "when": version.get("when", ""),
                "by": (version.get("by") or {}).get("displayName", ""),
            },
            "updated": version.get("when", ""),
            "author": (version.get("by") or {}).get("displayName", ""),
            "ancestors": ancestors,
            "parent_id": ancestors[-1]["id"] if ancestors else None,
            "labels": labels,
            "url": self.web_url((data.get("_links") or {}).get("webui")),
            "body_storage": body,
        }

    def normalize_summary(self, data: dict[str, Any]) -> dict[str, Any]:
        space = data.get("space") or {}
        version = data.get("version") or {}
        ancestors = [
            {
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
            }
            for item in data.get("ancestors") or []
            if isinstance(item, dict)
        ]
        return {
            "id": str(data.get("id", "")),
            "type": data.get("type", "page"),
            "title": data.get("title", ""),
            "space": {
                "id": str(space.get("id", "")),
                "key": space.get("key", ""),
                "name": space.get("name", ""),
            },
            "version": version.get("number"),
            "updated": version.get("when", ""),
            "ancestors": ancestors,
            "parent_id": ancestors[-1]["id"] if ancestors else None,
            "url": self.web_url((data.get("_links") or {}).get("webui")),
        }

    def normalize_search_result(self, item: dict[str, Any]) -> dict[str, Any]:
        content = item.get("content") or {}
        space = content.get("space") or item.get("resultGlobalContainer") or {}
        version = content.get("version") or {}
        content_id = content.get("id") or item.get("contentId")
        return {
            "id": str(content_id or ""),
            "type": content.get("type") or item.get("entityType") or "page",
            "title": content.get("title") or item.get("title") or "",
            "space": {
                "key": space.get("key", ""),
                "name": space.get("name") or space.get("title") or "",
            },
            "excerpt": strip_html(item.get("excerpt") or ""),
            "updated": version.get("when") or item.get("lastModified") or "",
            "url": self.web_url(
                (content.get("_links") or {}).get("webui")
                or item.get("url")
                or ""
            ),
        }

    def normalize_attachment(self, item: dict[str, Any]) -> dict[str, Any]:
        extensions = item.get("extensions") or {}
        metadata = item.get("metadata") or {}
        links = item.get("_links") or {}
        return {
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "filename": item.get("title", ""),
            "media_type": extensions.get("mediaType") or metadata.get("mediaType") or "",
            "size": int(extensions.get("fileSize") or 0),
            "version": (item.get("version") or {}).get("number"),
            "download_url": self.web_url(links.get("download") or ""),
            "url": self.web_url(links.get("webui") or ""),
        }


def parse_page_ref(ref: str) -> tuple[str | None, str | None, str | None]:
    """Return (page_id, space_key, title) parsed from a URL or page reference."""

    ref = ref.strip()
    if ref.isdigit():
        return ref, None, None
    if ref.startswith(("http://", "https://")):
        parsed = urlparse(ref)
        query = parse_qs(parsed.query)
        page_id = (query.get("pageId") or [None])[0]
        if page_id and str(page_id).isdigit():
            return str(page_id), None, None
        patterns = (
            r"/pages/(\d+)(?:/|$)",
            r"/content/(\d+)(?:/|$)",
            r"/pageId=(\d+)(?:/|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, parsed.path)
            if match:
                return match.group(1), None, None
        match = re.search(r"/spaces/([^/]+)/pages/(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(2), match.group(1), None
        match = re.search(r"/display/([^/]+)/(.+)$", parsed.path)
        if match:
            return None, match.group(1), _unquote_path(match.group(2).replace("+", " "))
        if parsed.path.rstrip("/").endswith("/viewpage.action"):
            title = (query.get("title") or query.get("pageTitle") or [None])[0]
            if title:
                return None, None, title
        return None, None, None
    if "/" in ref:
        space, title = ref.split("/", 1)
        if space.strip() and title.strip():
            return None, space.strip(), title.strip()
    return None, None, ref


def _unquote_path(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def cql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_cql(
    *,
    query: str | None,
    cql: str | None,
    space: str | None,
    labels: Iterable[str],
) -> str:
    if cql and query:
        raise CliError("Pass either a text query or --cql, not both")
    if cql:
        return cql.strip()
    parts = ["type = page"]
    if query:
        parts.append(f'text ~ "{cql_escape(query)}"')
    if space:
        spaces = [item.strip() for item in space.split(",") if item.strip()]
        if len(spaces) == 1:
            parts.append(f'space = "{cql_escape(spaces[0])}"')
        elif len(spaces) > 1:
            quoted = ", ".join(f'"{cql_escape(item)}"' for item in spaces)
            parts.append(f"space in ({quoted})")
    for label in labels:
        if label.strip():
            parts.append(f'label = "{cql_escape(label.strip())}"')
    return " and ".join(parts)


def slugify(value: str, *, fallback: str = "page", max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char.isalnum() or category.startswith(("L", "N")):
            chars.append(char)
        elif char in {"-", "_"}:
            chars.append(char)
        else:
            chars.append("-")
    result = re.sub(r"-{2,}", "-", "".join(chars)).strip(" .-_")
    result = result[:max_length].rstrip(" .")
    if not result:
        result = fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if result.upper() in reserved:
        result = f"_{result}"
    return result


def safe_filename(value: str, *, fallback: str = "attachment") -> str:
    value = Path(value or "").name
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    if not value:
        value = fallback
    value = value[:180].rstrip(" .")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper() in reserved:
        value = f"_{value}"
    return value


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise CliError(f"Too many files with the same name: {filename}")


def format_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def page_breadcrumb(page: dict[str, Any]) -> str:
    space = page.get("space") or {}
    bits = [space.get("name") or space.get("key") or ""]
    bits.extend(str(item.get("title") or "") for item in page.get("ancestors") or [])
    bits.append(str(page.get("title") or ""))
    return " / ".join(bit for bit in bits if bit)


def page_markdown(page: dict[str, Any], base_url: str) -> str:
    ancestors = page.get("ancestors") or []
    labels = page.get("labels") or []
    space = page.get("space") or {}
    version = page.get("version") or {}
    frontmatter = [
        "---",
        f'confluence_id: "{_yaml_escape(str(page.get("id", "")))}"',
        f'title: "{_yaml_escape(str(page.get("title", "")))}"',
        f'space_key: "{_yaml_escape(str(space.get("key", "")))}"',
        f'space_name: "{_yaml_escape(str(space.get("name", "")))}"',
        f'version: {version.get("number") if version.get("number") is not None else "null"}',
        f'updated: "{_yaml_escape(str(page.get("updated", "")))}"',
        f'author: "{_yaml_escape(str(page.get("author", "")))}"',
        f'url: "{_yaml_escape(str(page.get("url", "")))}"',
    ]
    if page.get("parent_id"):
        frontmatter.append(f'parent_id: "{_yaml_escape(str(page["parent_id"]))}"')
    if labels:
        frontmatter.append("labels:")
        frontmatter.extend(f'  - "{_yaml_escape(str(label))}"' for label in labels)
    else:
        frontmatter.append("labels: []")
    frontmatter.append("---")

    title = str(page.get("title") or "Untitled")
    body = storage_to_markdown(str(page.get("body_storage") or ""), base_url)
    source = f"[{space.get('key') or 'Confluence'}] {page_breadcrumb(page)}"
    if page.get("url"):
        source = f"{source}\n\nSource: {page['url']}"
    return "\n".join(frontmatter) + f"\n\n# {title}\n\n{source}\n\n{body}".rstrip() + "\n"


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def format_search_human(results: list[dict[str, Any]], cql: str) -> str:
    lines = [f"CQL: {cql}", f"Found {len(results)} result(s)."]
    for item in results:
        space = (item.get("space") or {}).get("key") or "?"
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        lines.append(f"\n- [{title}]({url})" if url else f"\n- {title}")
        lines.append(f"  id={item.get('id')} · space={space} · updated={item.get('updated') or '-'}")
        excerpt = item.get("excerpt")
        if excerpt:
            lines.append(f"  {excerpt[:500]}")
    return "\n".join(lines)


def format_children_human(children: list[dict[str, Any]]) -> str:
    lines = [f"{len(children)} child page(s)."]
    for item in children:
        depth = len(item.get("ancestors") or [])
        indent = "  " * max(0, depth - 1)
        lines.append(f"{indent}- {item.get('title')} (id={item.get('id')})")
    return "\n".join(lines)


def format_attachments_human(attachments: list[dict[str, Any]]) -> str:
    lines = [f"{len(attachments)} attachment(s)."]
    for item in attachments:
        lines.append(
            f"- {item.get('filename')} · {item.get('media_type') or 'unknown'} · "
            f"{format_size(int(item.get('size') or 0))} · id={item.get('id')}"
        )
        if item.get("download_url"):
            lines.append(f"  {item['download_url']}")
    return "\n".join(lines)


def format_comments_human(comments: list[dict[str, Any]]) -> str:
    lines = [f"## Comments ({len(comments)})"]
    for item in comments:
        author = item.get("author") or "Unknown"
        created = item.get("created") or ""
        lines.append(f"\n### {author}" + (f" · {created}" if created else ""))
        lines.append(str(item.get("body_markdown") or "").strip())
    return "\n".join(lines)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def cmd_doctor(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    result = client.doctor()
    if args.json:
        print_json(result)
    else:
        print(f"Confluence: {result['base_url']}")
        print(f"Auth: {result['auth']}")
        print(f"Reachable: {'yes' if result.get('reachable') else 'no'}")
        user = result.get("user") or {}
        if user.get("displayName") or user.get("username") or user.get("accountId"):
            print(
                "User: "
                + (user.get("displayName") or user.get("username") or user.get("accountId") or "")
            )
        for check in result.get("checks") or []:
            status = "OK" if check.get("ok") else "FAILED"
            suffix = f" — {check.get('error')}" if check.get("error") else ""
            print(f"  [{status}] {check.get('name')}{suffix}")
    return 0 if result.get("reachable") else 1


def cmd_resolve(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    page = client.resolve_page(args.ref, args.space)
    payload = {
        key: page.get(key)
        for key in ("id", "title", "space", "version", "updated", "url", "ancestors", "labels")
    }
    payload["breadcrumb"] = page_breadcrumb(page)
    if args.json:
        print_json(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    cql = build_cql(
        query=args.query.strip() if args.query else None,
        cql=args.cql,
        space=args.space,
        labels=args.label or [],
    )
    results = client.search(cql=cql, max_items=max(1, args.limit))
    if args.json:
        print_json({"cql": cql, "count": len(results), "results": results})
    else:
        print(format_search_human(results, cql))
    return 0


def _page_body_output(page: dict[str, Any], body_format: str, base_url: str) -> str:
    if body_format == "storage":
        return str(page.get("body_storage") or "")
    if body_format == "both":
        return (
            "# Markdown\n\n"
            + storage_to_markdown(str(page.get("body_storage") or ""), base_url)
            + "\n# Storage format\n\n```xml\n"
            + str(page.get("body_storage") or "")
            + "\n```\n"
        )
    return page_markdown(page, base_url)


def cmd_page(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    page = client.resolve_page(args.ref, args.space)
    if args.comments:
        page["comments"] = client.get_comments(page["id"])
    if args.attachments:
        page["attachments"] = client.get_attachments(page["id"])

    if args.output:
        output_path = Path(args.output).expanduser()
        value = _page_body_output(page, args.body_format, client.base_url)
        if args.comments and page.get("comments"):
            value += "\n" + format_comments_human(page["comments"]) + "\n"
        if args.attachments:
            value += "\n## Attachments\n\n" + format_attachments_human(page["attachments"]) + "\n"
        _write_text(output_path, value)
        if not args.json:
            print(str(output_path.resolve()))
        if not args.json:
            return 0

    if args.json:
        payload = dict(page)
        payload["body_markdown"] = storage_to_markdown(
            str(page.get("body_storage") or ""), client.base_url
        )
        if args.body_format == "storage":
            payload.pop("body_markdown", None)
        elif args.body_format == "markdown":
            payload.pop("body_storage", None)
        if args.output:
            payload["output"] = str(Path(args.output).expanduser().resolve())
        print_json(payload)
        return 0

    body = _page_body_output(page, args.body_format, client.base_url)
    if args.comments and page.get("comments"):
        body += "\n" + format_comments_human(page["comments"]) + "\n"
    if args.attachments:
        body += "\n## Attachments\n\n" + format_attachments_human(page["attachments"]) + "\n"
    print(body.rstrip())
    return 0


def cmd_children(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    page = client.resolve_page(args.ref, args.space)
    children = client.get_children(
        page["id"],
        recursive=args.recursive,
        max_items=max(1, args.max_items),
    )
    if args.json:
        print_json(
            {
                "parent": {"id": page["id"], "title": page["title"]},
                "recursive": bool(args.recursive),
                "count": len(children),
                "children": children,
            }
        )
    else:
        print(format_children_human(children))
    return 0


def _walk_tree(
    client: ConfluenceClient,
    page: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
    remaining: list[int],
    seen: set[str],
) -> list[tuple[int, dict[str, Any]]]:
    if depth > max_depth or remaining[0] <= 0 or page["id"] in seen:
        return []
    seen.add(page["id"])
    remaining[0] -= 1
    result = [(depth, page)]
    if depth >= max_depth or remaining[0] <= 0:
        return result
    try:
        children = client.get_children(
            page["id"],
            recursive=False,
            max_items=min(100, remaining[0]),
        )
    except ConfluenceError:
        raise
    for child in children:
        if remaining[0] <= 0:
            break
        result.extend(
            _walk_tree(
                client,
                child,
                depth=depth + 1,
                max_depth=max_depth,
                remaining=remaining,
                seen=seen,
            )
        )
    return result


def cmd_tree(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    root = client.resolve_page(args.ref, args.space)
    nodes = _walk_tree(
        client,
        root,
        depth=0,
        max_depth=max(0, args.max_depth),
        remaining=[max(1, args.max_items)],
        seen=set(),
    )
    if args.json:
        print_json(
            {
                "root": {"id": root["id"], "title": root["title"]},
                "count": len(nodes),
                "tree": [
                    {"depth": depth, "id": page["id"], "title": page["title"], "url": page.get("url")}
                    for depth, page in nodes
                ],
            }
        )
    else:
        for depth, page in nodes:
            prefix = "  " * depth + ("- " if depth else "")
            print(f"{prefix}{page.get('title')} (id={page.get('id')})")
    return 0


def cmd_attachments(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    page = client.resolve_page(args.ref, args.space)
    attachments = client.get_attachments(page["id"], max_items=max(1, args.limit))
    downloaded: list[dict[str, Any]] = []
    if args.download_dir:
        directory = Path(args.download_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        max_bytes = max(1, int(args.max_size_mb * 1024 * 1024))
        for attachment in attachments:
            filename = safe_filename(str(attachment.get("filename") or attachment.get("id")))
            destination = unique_path(directory / page["id"], filename)
            size = client.download_attachment(attachment, destination, max_bytes=max_bytes)
            downloaded.append(
                {
                    "id": attachment.get("id"),
                    "filename": attachment.get("filename"),
                    "path": str(destination.resolve()),
                    "bytes": size,
                }
            )
    if args.json:
        print_json(
            {
                "page": {"id": page["id"], "title": page["title"]},
                "count": len(attachments),
                "attachments": attachments,
                "downloaded": downloaded,
            }
        )
    else:
        print(format_attachments_human(attachments))
        if downloaded:
            print("\nDownloaded:")
            for item in downloaded:
                print(f"- {item['path']} ({format_size(item['bytes'])})")
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    page = client.resolve_page(args.ref, args.space)
    comments = client.get_comments(page["id"], max_items=max(1, args.limit))
    if args.json:
        print_json(
            {
                "page": {"id": page["id"], "title": page["title"]},
                "count": len(comments),
                "comments": comments,
            }
        )
    else:
        print(format_comments_human(comments))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    client = ConfluenceClient(Config.from_args(args))
    root = client.resolve_page(args.ref, args.space)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    max_pages = max(1, args.max_pages)
    pages = [root]
    if args.descendants and max_pages > 1:
        pages.extend(
            client.get_children(
                root["id"],
                recursive=True,
                max_items=max_pages - 1,
            )
        )

    manifest: dict[str, Any] = {
        "root": {"id": root["id"], "title": root["title"], "url": root.get("url")},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "base_url": client.base_url,
        "page_count": 0,
        "pages": [],
    }
    max_attachment_bytes = max(1, int(args.max_attachment_mb * 1024 * 1024))
    for index, page_or_summary in enumerate(pages, start=1):
        page = (
            page_or_summary
            if page_or_summary.get("body_storage") is not None
            else client.get_page_by_id(str(page_or_summary["id"]))
        )
        filename = f"{index:04d}-{slugify(str(page.get('title') or 'page'))}-{page['id']}.md"
        path = output_dir / filename
        _write_text(path, page_markdown(page, client.base_url))
        record: dict[str, Any] = {
            "id": page["id"],
            "title": page["title"],
            "parent_id": page.get("parent_id"),
            "url": page.get("url"),
            "file": filename,
            "attachments": [],
        }
        if args.attachments:
            attachments = client.get_attachments(page["id"])
            attachment_dir = output_dir / "attachments" / page["id"]
            for attachment in attachments:
                original_name = str(
                    attachment.get("filename") or attachment.get("id") or "attachment"
                )
                destination = unique_path(
                    attachment_dir,
                    safe_filename(original_name),
                )
                size = client.download_attachment(
                    attachment,
                    destination,
                    max_bytes=max_attachment_bytes,
                )
                record["attachments"].append(
                    {
                        "id": attachment.get("id"),
                        "filename": original_name,
                        "path": str(destination.relative_to(output_dir)),
                        "bytes": size,
                    }
                )
        manifest["pages"].append(record)
        manifest["page_count"] = len(manifest["pages"])

    _write_text(output_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.json:
        print_json(manifest)
    else:
        print(f"Exported {manifest['page_count']} page(s) to {output_dir}")
        print(f"Manifest: {output_dir / 'manifest.json'}")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", help="Confluence base URL, e.g. https://example.atlassian.net/wiki")
    parser.add_argument("--config", help="Path to a JSON configuration file")
    parser.add_argument("--auth", choices=("auto", "basic", "bearer", "none"), help="Authentication mode")
    parser.add_argument("--timeout", type=float, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--verbose", action="store_true", help="Show traceback on failure")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="confluence.py",
        description="Read-only Confluence client for Codex.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check configuration and authentication")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    resolve = subparsers.add_parser("resolve", help="Resolve a URL, page id, or title")
    add_common(resolve)
    resolve.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    resolve.add_argument("--space", help="Space key used when resolving by title")
    resolve.set_defaults(func=cmd_resolve)

    search = subparsers.add_parser("search", help="Search pages with CQL or plain text")
    add_common(search)
    search.add_argument("query", nargs="?", help="Plain-text search query")
    search.add_argument("--cql", help="Raw CQL; mutually exclusive with the text query")
    search.add_argument("--space", help="Restrict to one or more comma-separated space keys")
    search.add_argument("--label", action="append", help="Restrict to a label; may be repeated")
    search.add_argument("--limit", type=int, default=25, help="Maximum results (default: 25)")
    search.set_defaults(func=cmd_search)

    page = subparsers.add_parser("page", help="Read one page")
    add_common(page)
    page.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    page.add_argument("--space", help="Space key used when resolving by title")
    page.add_argument(
        "--body-format",
        choices=("markdown", "storage", "both"),
        default="markdown",
        help="Body representation (default: markdown)",
    )
    page.add_argument("--comments", action="store_true", help="Include comments")
    page.add_argument("--attachments", action="store_true", help="Include attachment metadata")
    page.add_argument("--output", help="Write the result to this file")
    page.set_defaults(func=cmd_page)

    children = subparsers.add_parser("children", help="List child or descendant pages")
    add_common(children)
    children.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    children.add_argument("--space", help="Space key used when resolving by title")
    children.add_argument("--recursive", action="store_true", help="Include all descendants")
    children.add_argument("--max-items", type=int, default=500, help="Maximum pages (default: 500)")
    children.set_defaults(func=cmd_children)

    tree = subparsers.add_parser("tree", help="Show a page hierarchy")
    add_common(tree)
    tree.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    tree.add_argument("--space", help="Space key used when resolving by title")
    tree.add_argument("--max-depth", type=int, default=10, help="Maximum depth (default: 10)")
    tree.add_argument("--max-items", type=int, default=500, help="Maximum pages (default: 500)")
    tree.set_defaults(func=cmd_tree)

    attachments = subparsers.add_parser("attachments", help="List or download attachments")
    add_common(attachments)
    attachments.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    attachments.add_argument("--space", help="Space key used when resolving by title")
    attachments.add_argument("--limit", type=int, default=500, help="Maximum attachments")
    attachments.add_argument("--download-dir", help="Download attachments into this directory")
    attachments.add_argument(
        "--max-size-mb",
        type=float,
        default=50,
        help="Maximum size per downloaded attachment in MB (default: 50)",
    )
    attachments.set_defaults(func=cmd_attachments)

    comments = subparsers.add_parser("comments", help="Read page comments")
    add_common(comments)
    comments.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    comments.add_argument("--space", help="Space key used when resolving by title")
    comments.add_argument("--limit", type=int, default=500, help="Maximum comments")
    comments.set_defaults(func=cmd_comments)

    export = subparsers.add_parser("export", help="Export a page tree to Markdown")
    add_common(export)
    export.add_argument("ref", help="Page URL, numeric id, SPACE/Title, or exact title")
    export.add_argument("--space", help="Space key used when resolving by title")
    export.add_argument("--output-dir", required=True, help="Destination directory")
    export.add_argument("--descendants", action="store_true", help="Include descendant pages")
    export.add_argument("--max-pages", type=int, default=100, help="Maximum pages (default: 100)")
    export.add_argument("--attachments", action="store_true", help="Download attachments")
    export.add_argument(
        "--max-attachment-mb",
        type=float,
        default=50,
        help="Maximum size per attachment in MB (default: 50)",
    )
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except ConfluenceError as exc:
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            print(f"Confluence error: {exc}", file=sys.stderr)
        return 1
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
