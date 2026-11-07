"""HTTP transport with origin isolation, redaction, and retry policy."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import ssl
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import USER_AGENT
from .config import Config
from .errors import (
    ApiError,
    AuthenticationError,
    ConfluenceReaderError,
    NotFoundError,
    OutcomeUnknownError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
    VersionConflictError,
)


def origin_of(value: str) -> str:
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


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Refuse to forward authenticated requests to another origin."""

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
        if origin_of(target) != self.origin:
            raise HTTPError(
                target,
                code,
                "Refusing to forward Confluence credentials to another origin",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def redact_secrets(value: str) -> str:
    result = value or ""
    result = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]+",
        "Authorization: [REDACTED]",
        result,
    )
    result = re.sub(
        r"(?i)\b(api[_-]?token|personal[_-]?access[_-]?token|password)"
        r"\s*[:=]\s*([^\s,;]+)",
        r"\1: [REDACTED]",
        result,
    )
    result = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", result)
    result = re.sub(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", result)
    return result


def _error_message(body: bytes, status: int, fallback: str = "") -> str:
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
        message = body.decode("utf-8", errors="replace").strip()[:800]
    message = redact_secrets(message or fallback)
    if status == 401:
        return f"Authentication failed: {message or 'check the configured credentials'}"
    if status == 403:
        return f"Permission denied: {message or 'the account cannot perform this operation'}"
    if status == 404:
        return message or "Confluence resource not found"
    if status == 429:
        return message or "Confluence rate limit reached"
    return message or f"HTTP {status}"


def error_for_status(
    status: int,
    message: str,
    *,
    url: str = "",
    details: dict[str, Any] | None = None,
) -> ConfluenceReaderError:
    payload = {"url": url, **(details or {})} if url else dict(details or {})
    if status == 400:
        return ValidationError(message, status=status, details=payload)
    if status == 401:
        return AuthenticationError(message, status=status, details=payload)
    if status == 403:
        return PermissionDeniedError(message, status=status, details=payload)
    if status == 404:
        return NotFoundError(message, status=status, details=payload)
    if status == 409:
        return VersionConflictError(message, status=status, details=payload)
    if status == 429:
        return RateLimitError(message, status=status, retryable=True, details=payload)
    return ApiError(message, status=status, details=payload)


@dataclass
class TransportResponse:
    data: bytes
    headers: dict[str, str]
    status: int
    url: str

    def json(self) -> Any:
        if not self.data:
            return {}
        try:
            return json.loads(self.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ApiError("Expected JSON from Confluence", details={"url": self.url}) from exc


def multipart_body(
    fields: Mapping[str, str],
    files: Mapping[str, Path | tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----confluence-reader-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, file_value in files.items():
        if isinstance(file_value, Path):
            path = file_value
            filename = path.name
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            content = path.read_bytes()
        else:
            filename, content, content_type = file_value
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ConfluenceTransport:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.origin = origin_of(self.base_url)
        self.ssl_context = ssl.create_default_context()
        if not config.verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        self.headers: dict[str, str] = {
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if config.auth == "basic":
            credentials = f"{config.email}:{config.api_token}".encode()
            encoded = base64.b64encode(credentials).decode("ascii")
            self.headers["Authorization"] = f"Basic {encoded}"
        elif config.auth == "bearer":
            token = config.personal_access_token or config.api_token
            self.headers["Authorization"] = f"Bearer {token}"
        self.opener = build_opener(
            SameOriginRedirectHandler(self.origin),
            HTTPSHandler(context=self.ssl_context),
        )

    def api_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            target = path_or_url
        else:
            target = f"{self.base_url}/{path_or_url.lstrip('/')}"
        if origin_of(target) != self.origin:
            raise ValidationError("Refusing to access a URL outside the configured Confluence site")
        return target

    def with_query(self, url: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return url
        query = urlencode(
            [(key, value) for key, value in params.items() if value is not None],
            doseq=True,
        )
        if not query:
            return url
        return f"{url}{'&' if '?' in url else '?'}{query}"

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
        retry: bool | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        method = method.upper()
        retry_safe = method in {"GET", "HEAD"} if retry is None else retry
        url = self.with_query(self.api_url(path_or_url), params)
        headers = dict(self.headers)
        headers["Accept"] = accept
        if extra_headers:
            headers.update(extra_headers)
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type

        attempts = 3 if retry_safe else 1
        delay = 1.0
        for attempt in range(1, attempts + 1):
            request = Request(url, headers=headers, data=body, method=method)
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    return TransportResponse(
                        data=response.read(),
                        headers={key: value for key, value in response.headers.items()},
                        status=int(response.status),
                        url=response.geturl(),
                    )
            except HTTPError as exc:
                error_body = exc.read()
                message = _error_message(error_body, exc.code, fallback=exc.msg)
                if (
                    retry_safe
                    and exc.code in {429, 500, 502, 503, 504}
                    and attempt < attempts
                ):
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        sleep_for = max(delay, float(retry_after)) if retry_after else delay
                    except (TypeError, ValueError):
                        sleep_for = delay
                    time.sleep(min(sleep_for, 10.0))
                    delay *= 2
                    continue
                raise error_for_status(exc.code, message, url=url) from exc
            except URLError as exc:
                if retry_safe and attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                reason = redact_secrets(str(getattr(exc, "reason", exc)))
                if retry_safe:
                    raise ApiError(f"Cannot reach Confluence at {self.base_url}: {reason}") from exc
                raise OutcomeUnknownError(
                    "The connection failed after a write may have been sent. "
                    "Check the current Confluence state before retrying.",
                    details={"reason": reason, "url": url},
                ) from exc
            except TimeoutError as exc:
                if retry_safe and attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if retry_safe:
                    raise ApiError(f"Confluence request timed out: {url}") from exc
                raise OutcomeUnknownError(
                    "The write timed out and its outcome is unknown. "
                    "Check the current Confluence state before retrying.",
                    details={"url": url},
                ) from exc
            except ssl.SSLError as exc:
                raise ApiError(f"TLS error while contacting {self.base_url}: {exc}") from exc
        raise ApiError(f"Confluence request failed after retries: {url}")

    def download(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        overwrite: bool = False,
    ) -> int:
        target_url = self.api_url(url)
        if destination.exists() and not overwrite:
            raise ValidationError(f"Destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = dict(self.headers)
        headers["Accept"] = "*/*"
        request = Request(target_url, headers=headers, method="GET")
        written = 0
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise ValidationError(
                                f"Attachment is larger than the configured "
                                f"{max_bytes // (1024 * 1024)} MB limit"
                            )
                    except ValueError:
                        pass
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            output.close()
                            destination.unlink(missing_ok=True)
                            raise ValidationError(
                                f"Attachment exceeds the configured "
                                f"{max_bytes // (1024 * 1024)} MB limit"
                            )
                        output.write(chunk)
        except HTTPError as exc:
            destination.unlink(missing_ok=True)
            message = _error_message(exc.read(), exc.code, fallback=exc.msg)
            raise error_for_status(exc.code, message, url=target_url) from exc
        except (URLError, TimeoutError, ssl.SSLError) as exc:
            destination.unlink(missing_ok=True)
            raise ApiError(f"Cannot download attachment: {exc}") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return written
