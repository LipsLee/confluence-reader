"""Local confirmation tokens and append-only audit records."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import ConfirmationError, ConfirmationExpiredError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, state_dir: Path, *, confirmation_ttl: int = 600) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.db_path = self.state_dir / "state.db"
        self.audit_path = self.state_dir / "audit.jsonl"
        self.confirmation_ttl = confirmation_ttl
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_actions (
                    token TEXT PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    page_id TEXT,
                    expected_version INTEGER,
                    preview_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                )
                """
            )
            connection.commit()
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pending_actions WHERE expires_at < ? OR used_at IS NOT NULL",
                (now,),
            )
            connection.commit()

    def create_confirmation(
        self,
        *,
        base_url: str,
        operation: str,
        arguments: dict[str, Any],
        page_id: str | None,
        expected_version: int | None,
        preview: dict[str, Any],
    ) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        created = utc_now()
        expires = created + timedelta(seconds=self.confirmation_ttl)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_actions (
                    token, base_url, operation, args_hash, page_id,
                    expected_version, preview_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    base_url,
                    operation,
                    canonical_hash(arguments),
                    page_id,
                    expected_version,
                    json.dumps(preview, ensure_ascii=False),
                    created.isoformat(),
                    expires.isoformat(),
                ),
            )
            connection.commit()
        return token, expires.isoformat()

    def consume_confirmation(
        self,
        token: str,
        *,
        base_url: str,
        operation: str,
        arguments: dict[str, Any],
        page_id: str | None,
        current_version: int | None,
    ) -> dict[str, Any]:
        if not token:
            raise ConfirmationError("A confirmation token is required")
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise ConfirmationExpiredError(
                    "This confirmation token is invalid or has already been used"
                )
            if row["used_at"]:
                raise ConfirmationExpiredError("This confirmation token has already been used")
            if datetime.fromisoformat(row["expires_at"]) < now:
                connection.execute(
                    "UPDATE pending_actions SET used_at = ? WHERE token = ?",
                    (now.isoformat(), token),
                )
                connection.commit()
                raise ConfirmationExpiredError("This confirmation token has expired")
            if row["base_url"] != base_url or row["operation"] != operation:
                raise ConfirmationError(
                    "The confirmation token does not match this site or operation"
                )
            if row["args_hash"] != canonical_hash(arguments):
                raise ConfirmationError(
                    "The confirmation token does not match the requested arguments"
                )
            if (row["page_id"] or None) != (page_id or None):
                raise ConfirmationError("The confirmation token does not match this page")
            if row["expected_version"] != current_version:
                raise ConfirmationExpiredError(
                    "The page changed after the preview. Generate a new preview before confirming."
                )
            connection.execute(
                "UPDATE pending_actions SET used_at = ? WHERE token = ?",
                (now.isoformat(), token),
            )
            connection.commit()
            return json.loads(row["preview_json"])

    def record_audit(
        self,
        *,
        base_url: str,
        operation: str,
        page_id: str | None,
        space_key: str | None,
        version_before: int | None,
        version_after: int | None,
        outcome: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        audit_id = uuid.uuid4().hex
        record = {
            "audit_id": audit_id,
            "timestamp": utc_now().isoformat(),
            "base_url": base_url,
            "operation": operation,
            "page_id": page_id,
            "space_key": space_key,
            "version_before": version_before,
            "version_after": version_after,
            "outcome": outcome,
            "error_code": error_code,
        }
        if details:
            record["details"] = details
        with self._lock:
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        return audit_id

    def tail_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result
