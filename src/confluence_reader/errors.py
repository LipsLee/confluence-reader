"""Stable error types shared by the CLI and MCP server."""

from __future__ import annotations

from typing import Any


def _redact(value: Any) -> Any:
    from .transport import redact_secrets

    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in ("token", "password", "authorization", "cookie", "secret")
            ):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class ConfluenceReaderError(RuntimeError):
    """Base class for expected user-facing errors."""

    code = "CONFLUENCE_ERROR"
    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status = status
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": _redact(self.message),
                "retryable": self.retryable,
            },
        }
        if self.status is not None:
            result["error"]["status"] = self.status
        if self.details:
            result["error"]["details"] = _redact(self.details)
        return result


class ConfigError(ConfluenceReaderError):
    code = "CONFIG_ERROR"
    exit_code = 2


class AuthenticationError(ConfluenceReaderError):
    code = "AUTH_FAILED"
    exit_code = 3


class PermissionDeniedError(ConfluenceReaderError):
    code = "PERMISSION_DENIED"
    exit_code = 3


class NotFoundError(ConfluenceReaderError):
    code = "NOT_FOUND"
    exit_code = 4


class ValidationError(ConfluenceReaderError):
    code = "INVALID_INPUT"
    exit_code = 2


class ConflictError(ConfluenceReaderError):
    code = "CONFLICT"
    exit_code = 5


class VersionConflictError(ConflictError):
    code = "VERSION_CONFLICT"


class WritesDisabledError(ConfluenceReaderError):
    code = "WRITES_DISABLED"
    exit_code = 3


class ConfirmationError(ConfluenceReaderError):
    code = "CONFIRMATION_REQUIRED"
    exit_code = 7


class ConfirmationExpiredError(ConfirmationError):
    code = "CONFIRMATION_EXPIRED"


class UnsupportedOperationError(ConfluenceReaderError):
    code = "UNSUPPORTED_ON_SITE"
    exit_code = 6


class RateLimitError(ConfluenceReaderError):
    code = "RATE_LIMITED"
    exit_code = 1
    retryable = True


class OutcomeUnknownError(ConfluenceReaderError):
    code = "OUTCOME_UNKNOWN"
    exit_code = 8


class ApiError(ConfluenceReaderError):
    code = "UPSTREAM_ERROR"
    exit_code = 1
