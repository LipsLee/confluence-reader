"""High-level operations shared by the CLI and MCP server."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import ConfluenceClient
from .config import Config
from .errors import (
    ConflictError,
    ConfluenceReaderError,
    NotFoundError,
    UnsupportedOperationError,
    ValidationError,
    VersionConflictError,
    WritesDisabledError,
)
from .markdown import body_to_storage, storage_to_markdown
from .state import StateStore
from .utils import (
    attachment_download_name,
    build_cql,
    page_markdown,
    safe_filename,
    slugify,
    unique_path,
    write_text,
)


class ConfluenceService:
    def __init__(
        self,
        config: Config,
        *,
        client: ConfluenceClient | None = None,
        state: StateStore | None = None,
    ) -> None:
        self.config = config
        self.client = client or ConfluenceClient(config)
        self.state = state or StateStore(config.state_dir)

    @classmethod
    def from_config(cls, config: Config) -> ConfluenceService:
        return cls(config)

    def _ensure_writes(self) -> None:
        if not self.config.writes_allowed:
            if self.config.auth == "none":
                raise WritesDisabledError("Anonymous Confluence access is read-only")
            raise WritesDisabledError(
                "Writes are disabled. Set CONFLUENCE_ENABLE_WRITES=true to enable them."
            )

    def _resolve(self, ref: str, space_key: str | None = None) -> dict[str, Any]:
        return self.client.resolve_page(ref, space_key)

    def _convert_body(self, body: str, body_format: str) -> str:
        if body_format not in {"markdown", "storage"}:
            raise ValidationError("body_format must be markdown or storage")
        try:
            return body_to_storage(body, body_format)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _validate_local_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser().resolve()
        if self.config.file_roots and not any(
            path == root or path.is_relative_to(root) for root in self.config.file_roots
        ):
            raise ValidationError(
                "The path is outside the configured CONFLUENCE_FILE_ROOTS boundaries"
            )
        return path

    def _audit(
        self,
        *,
        operation: str,
        page_id: str | None,
        space_key: str | None,
        version_before: int | None,
        version_after: int | None,
        outcome: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        return self.state.record_audit(
            base_url=self.config.base_url,
            operation=operation,
            page_id=page_id,
            space_key=space_key,
            version_before=version_before,
            version_after=version_after,
            outcome=outcome,
            error_code=error_code,
            details=details,
        )

    def _audit_failure(
        self,
        *,
        operation: str,
        page_id: str | None,
        space_key: str | None,
        version_before: int | None,
        exc: Exception,
    ) -> None:
        code = exc.code if isinstance(exc, ConfluenceReaderError) else "UNEXPECTED_ERROR"
        self._audit(
            operation=operation,
            page_id=page_id,
            space_key=space_key,
            version_before=version_before,
            version_after=None,
            outcome="failed",
            error_code=code,
        )

    def _mutation(
        self,
        *,
        operation: str,
        page: dict[str, Any],
        version_before: int | None,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        version_after = (page.get("version") or {}).get("number")
        audit_id = self._audit(
            operation=operation,
            page_id=str(page.get("id") or ""),
            space_key=(page.get("space") or {}).get("key"),
            version_before=version_before,
            version_after=version_after,
            outcome="success",
        )
        result = {
            "ok": True,
            "action": action,
            "page_id": str(page.get("id") or ""),
            "title": page.get("title"),
            "space": page.get("space"),
            "version_before": version_before,
            "version_after": version_after,
            "url": page.get("url"),
            "audit_id": audit_id,
        }
        if extra:
            result.update(extra)
        return result

    def status(self, *, probe: bool = True) -> dict[str, Any]:
        if probe:
            result = self.client.doctor()
        else:
            result = {
                **self.config.public_summary(),
                "flavor_detected": self.client.flavor,
                "capabilities": self.client.capabilities(),
            }
        result["ok"] = True
        result["state_dir"] = str(self.config.state_dir)
        return result

    def resolve_page(self, ref: str, space_key: str | None = None) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        return {
            "ok": True,
            "id": page["id"],
            "title": page["title"],
            "space": page.get("space"),
            "version": page.get("version"),
            "updated": page.get("updated"),
            "url": page.get("url"),
            "ancestors": page.get("ancestors"),
            "parent_id": page.get("parent_id"),
            "labels": page.get("labels"),
        }

    def _page_payload(
        self,
        page: dict[str, Any],
        *,
        body_format: str,
    ) -> dict[str, Any]:
        if body_format not in {"markdown", "storage", "both"}:
            raise ValidationError("body_format must be markdown, storage, or both")
        payload = dict(page)
        if body_format in {"markdown", "both"}:
            payload["body_markdown"] = storage_to_markdown(
                str(page.get("body_storage") or ""),
                self.client.base_url,
            )
        if body_format == "markdown":
            payload.pop("body_storage", None)
        elif body_format == "storage":
            payload.pop("body_markdown", None)
        payload["ok"] = True
        return payload

    def get_page(
        self,
        ref: str,
        *,
        space_key: str | None = None,
        body_format: str = "markdown",
        include_comments: bool = False,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        payload = self._page_payload(page, body_format=body_format)
        if include_comments:
            payload["comments"] = self.get_comments(page["id"])["comments"]
        if include_attachments:
            payload["attachments"] = self.list_attachments(page["id"])["attachments"]
        return payload

    def search_pages(
        self,
        *,
        query: str | None = None,
        cql: str | None = None,
        space_keys: Iterable[str] | None = None,
        labels: Iterable[str] | None = None,
        last_modified: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        cql_value = build_cql(
            query=query,
            cql=cql,
            space_keys=space_keys,
            labels=labels,
            last_modified=last_modified,
        )
        result = self.client.search_page(cql=cql_value, limit=limit, cursor=cursor)
        result["ok"] = True
        return result

    def list_children(
        self,
        ref: str,
        *,
        space_key: str | None = None,
        recursive: bool = False,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        result = self.client.get_children_page(
            page["id"],
            recursive=recursive,
            limit=limit,
            cursor=cursor,
        )
        result.update(
            {
                "ok": True,
                "parent": {"id": page["id"], "title": page["title"]},
                "recursive": recursive,
            }
        )
        return result

    def list_page_versions(
        self,
        ref: str,
        *,
        space_key: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        result = self.client.list_versions_page(page["id"], limit=limit, cursor=cursor)
        result.update(
            {
                "ok": True,
                "page": {"id": page["id"], "title": page["title"]},
            }
        )
        return result

    def get_comments(
        self,
        ref: str,
        *,
        space_key: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        result = self.client.get_comments_page(page["id"], limit=limit, cursor=cursor)
        for comment in result["comments"]:
            comment["body_markdown"] = storage_to_markdown(
                str(comment.get("body_storage") or ""),
                self.client.base_url,
            )
        result.update(
            {
                "ok": True,
                "page": {"id": page["id"], "title": page["title"]},
            }
        )
        return result

    def list_attachments(
        self,
        ref: str,
        *,
        space_key: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        result = self.client.get_attachments_page(page["id"], limit=limit, cursor=cursor)
        result.update(
            {
                "ok": True,
                "page": {"id": page["id"], "title": page["title"]},
            }
        )
        return result

    def download_attachment(
        self,
        *,
        ref: str,
        destination_path: str,
        attachment_id: str | None = None,
        filename: str | None = None,
        overwrite: bool = False,
        space_key: str | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        page = self._resolve(ref, space_key)
        attachments = self.client.get_attachments(page["id"], max_items=500)
        match = None
        for item in attachments:
            if attachment_id and str(item.get("id")) == str(attachment_id):
                match = item
                break
            if filename and item.get("filename") == filename:
                match = item
                break
        if match is None:
            raise ValidationError("Attachment not found on the page")
        destination = self._validate_local_path(destination_path)
        if destination.is_dir() or destination_path.endswith(("/", "\\")):
            destination = unique_path(destination, attachment_download_name(match))
        written = self.client.download_attachment(
            match,
            destination,
            overwrite=overwrite,
            max_bytes=max_bytes,
        )
        return {
            "ok": True,
            "page_id": page["id"],
            "attachment": match,
            "path": str(destination),
            "bytes": written,
        }

    def create_page(
        self,
        *,
        space_key: str,
        title: str,
        body: str,
        body_format: str = "markdown",
        parent_id: str | None = None,
        version_message: str = "Created by Confluence Agent",
    ) -> dict[str, Any]:
        self._ensure_writes()
        storage = self._convert_body(body, body_format)
        page: dict[str, Any] | None = None
        try:
            page = self.client.create_page(
                space_key=space_key,
                title=title,
                body_storage=storage,
                parent_id=parent_id,
                version_message=version_message,
            )
            return self._mutation(
                operation="create_page",
                page=page,
                version_before=None,
                action="created",
            )
        except Exception as exc:
            self._audit_failure(
                operation="create_page",
                page_id=str(page.get("id")) if page else None,
                space_key=space_key,
                version_before=None,
                exc=exc,
            )
            raise

    def _conflict(
        self,
        *,
        page: dict[str, Any],
        expected_version: int,
        proposed_body: str | None = None,
        proposed_title: str | None = None,
    ) -> VersionConflictError:
        current_version = int((page.get("version") or {}).get("number") or 0)
        current_markdown = storage_to_markdown(
            str(page.get("body_storage") or ""),
            self.client.base_url,
        )
        proposed_markdown = current_markdown
        if proposed_body is not None:
            proposed_markdown = proposed_body
        diff = "\n".join(
            difflib.unified_diff(
                current_markdown.splitlines(),
                proposed_markdown.splitlines(),
                fromfile=f"current-v{current_version}",
                tofile=f"proposed-v{expected_version + 1}",
                lineterm="",
            )
        )
        if len(diff) > 12000:
            diff = diff[:12000] + "\n... diff truncated ..."
        return VersionConflictError(
            "The page version does not match expected_version",
            status=409,
            details={
                "page_id": page.get("id"),
                "expected_version": expected_version,
                "current_version": current_version,
                "current_title": page.get("title"),
                "proposed_title": proposed_title,
                "diff": diff,
                "next_step": "Re-read the page, resolve the changes, and retry with the current version.",
            },
        )

    def _assert_restore_title_available(
        self,
        *,
        current: dict[str, Any],
        restored_title: str,
    ) -> None:
        """Avoid a partial restore when the historical title is now taken."""

        if not restored_title or restored_title == current.get("title"):
            return
        space_key = str((current.get("space") or {}).get("key") or "")
        if not space_key:
            return
        try:
            existing = self.client.get_page_by_title(space_key, restored_title)
        except NotFoundError:
            return
        if str(existing.get("id")) != str(current.get("id")):
            raise ConflictError(
                "The historical title already belongs to another page in this space",
                details={
                    "page_id": current.get("id"),
                    "space_key": space_key,
                    "restored_title": restored_title,
                    "conflicting_page_id": existing.get("id"),
                    "conflicting_page_url": existing.get("url"),
                },
            )

    def update_page(
        self,
        *,
        page_id: str,
        expected_version: int,
        title: str | None = None,
        body: str | None = None,
        body_format: str = "markdown",
        version_message: str = "Updated by Confluence Agent",
    ) -> dict[str, Any]:
        self._ensure_writes()
        if expected_version < 1:
            raise ValidationError("expected_version must be positive")
        if title is None and body is None:
            raise ValidationError("Provide title, body, or both")
        current = self._resolve(page_id)
        current_version = int((current.get("version") or {}).get("number") or 0)
        proposed_markdown = None
        if body is not None:
            proposed_markdown = (
                body
                if body_format == "markdown"
                else storage_to_markdown(body, self.client.base_url)
            )
        if current_version != expected_version:
            raise self._conflict(
                page=current,
                expected_version=expected_version,
                proposed_body=proposed_markdown,
                proposed_title=title,
            )
        storage = (
            self._convert_body(body, body_format)
            if body is not None
            else str(current.get("body_storage") or "")
        )
        try:
            updated = self.client.update_page(
                current["id"],
                expected_version=expected_version,
                title=title if title is not None else str(current.get("title") or ""),
                body_storage=storage,
                version_message=version_message,
            )
            return self._mutation(
                operation="update_page",
                page=updated,
                version_before=expected_version,
                action="updated",
            )
        except Exception as exc:
            self._audit_failure(
                operation="update_page",
                page_id=str(current["id"]),
                space_key=(current.get("space") or {}).get("key"),
                version_before=expected_version,
                exc=exc,
            )
            raise

    def append_page(
        self,
        *,
        page_id: str,
        expected_version: int,
        body: str,
        body_format: str = "markdown",
        version_message: str = "Appended by Confluence Agent",
    ) -> dict[str, Any]:
        self._ensure_writes()
        if expected_version < 1:
            raise ValidationError("expected_version must be positive")
        current = self._resolve(page_id)
        current_version = int((current.get("version") or {}).get("number") or 0)
        storage = self._convert_body(body, body_format)
        proposed = str(current.get("body_storage") or "").rstrip() + "\n\n" + storage
        if current_version != expected_version:
            raise self._conflict(
                page=current,
                expected_version=expected_version,
                proposed_body=storage_to_markdown(proposed, self.client.base_url),
            )
        try:
            updated = self.client.update_page(
                current["id"],
                expected_version=expected_version,
                title=str(current.get("title") or ""),
                body_storage=proposed,
                version_message=version_message,
            )
            return self._mutation(
                operation="append_page",
                page=updated,
                version_before=expected_version,
                action="appended",
            )
        except Exception as exc:
            self._audit_failure(
                operation="append_page",
                page_id=str(current["id"]),
                space_key=(current.get("space") or {}).get("key"),
                version_before=expected_version,
                exc=exc,
            )
            raise

    def add_comment(
        self,
        *,
        page_id: str,
        body: str,
        body_format: str = "markdown",
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self._resolve(page_id)
        storage = self._convert_body(body, body_format)
        try:
            result = self.client.add_comment(page["id"], storage)
            result["audit_id"] = self._audit(
                operation="add_comment",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                version_after=None,
                outcome="success",
            )
            return result
        except Exception as exc:
            self._audit_failure(
                operation="add_comment",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                exc=exc,
            )
            raise

    def add_labels(
        self,
        *,
        page_id: str,
        labels: Iterable[str],
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self._resolve(page_id)
        try:
            result = self.client.add_labels(page["id"], labels)
            result["audit_id"] = self._audit(
                operation="add_labels",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                version_after=None,
                outcome="success",
                details={"labels": result.get("labels")},
            )
            return result
        except Exception as exc:
            self._audit_failure(
                operation="add_labels",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                exc=exc,
            )
            raise

    def upload_attachment(
        self,
        *,
        page_id: str,
        file_path: str,
        replace_existing: bool = False,
        expected_attachment_version: int | None = None,
        comment: str = "",
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self._resolve(page_id)
        path = self._validate_local_path(file_path)
        try:
            result = self.client.upload_attachment(
                page["id"],
                path,
                replace_existing=replace_existing,
                expected_attachment_version=expected_attachment_version,
                comment=comment,
            )
            result["audit_id"] = self._audit(
                operation="upload_attachment",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                version_after=None,
                outcome="success",
                details={
                    "filename": path.name,
                    "action": result.get("action"),
                },
            )
            return result
        except Exception as exc:
            self._audit_failure(
                operation="upload_attachment",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=(page.get("version") or {}).get("number"),
                exc=exc,
            )
            raise

    def _preview(
        self,
        *,
        operation: str,
        page: dict[str, Any],
        arguments: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        version = (page.get("version") or {}).get("number")
        token, expires_at = self.state.create_confirmation(
            base_url=self.config.base_url,
            operation=operation,
            arguments=arguments,
            page_id=str(page.get("id") or ""),
            expected_version=int(version) if version is not None else None,
            preview=summary,
        )
        return {
            "ok": True,
            "action": "preview",
            "confirmation_required": True,
            "confirmation_token": token,
            "expires_at": expires_at,
            "page_id": str(page.get("id") or ""),
            "page_title": page.get("title"),
            "space": page.get("space"),
            "current_version": version,
            "summary": summary,
            "instructions": (
                "Show this preview to the user and obtain explicit approval. "
                "Then call the same tool with the same arguments and confirmation_token."
            ),
        }

    def move_page(
        self,
        *,
        page_id: str,
        new_parent_id: str,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self._resolve(page_id)
        parent = self.client._same_space_parent(page, new_parent_id)
        arguments = {"page_id": page["id"], "new_parent_id": new_parent_id}
        if not confirmation_token:
            return self._preview(
                operation="move_page",
                page=page,
                arguments=arguments,
                summary={
                    "effect": "Move the page within its current space",
                    "from_parent_id": page.get("parent_id"),
                    "to_parent_id": new_parent_id,
                    "to_parent_title": parent.get("title"),
                    "cross_space": False,
                },
            )
        current_version = int((page.get("version") or {}).get("number") or 0)
        self.state.consume_confirmation(
            confirmation_token,
            base_url=self.config.base_url,
            operation="move_page",
            arguments=arguments,
            page_id=str(page["id"]),
            current_version=current_version,
        )
        try:
            updated = self.client.update_page(
                page["id"],
                expected_version=current_version,
                title=str(page.get("title") or ""),
                body_storage=str(page.get("body_storage") or ""),
                version_message="Moved within space by Confluence Agent",
                parent_id=new_parent_id,
            )
            return self._mutation(
                operation="move_page",
                page=updated,
                version_before=current_version,
                action="moved",
                extra={"new_parent_id": new_parent_id},
            )
        except Exception as exc:
            self._audit_failure(
                operation="move_page",
                page_id=str(page["id"]),
                space_key=(page.get("space") or {}).get("key"),
                version_before=current_version,
                exc=exc,
            )
            raise

    def trash_page(
        self,
        *,
        page_id: str,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self._resolve(page_id)
        space_key = str((page.get("space") or {}).get("key") or "")
        if not self.client.supports_trash(space_key):
            raise UnsupportedOperationError(
                "A safe trash operation is not available for this space",
                details={"page_id": page.get("id"), "space_key": space_key},
            )
        arguments = {"page_id": page["id"]}
        if not confirmation_token:
            return self._preview(
                operation="trash_page",
                page=page,
                arguments=arguments,
                summary={
                    "effect": "Move the page to the Confluence trash",
                    "permanent_delete": False,
                    "restore_available": True,
                },
            )
        current_version = int((page.get("version") or {}).get("number") or 0)
        self.state.consume_confirmation(
            confirmation_token,
            base_url=self.config.base_url,
            operation="trash_page",
            arguments=arguments,
            page_id=str(page["id"]),
            current_version=current_version,
        )
        try:
            result = self.client.trash_page(page["id"])
            result["audit_id"] = self._audit(
                operation="trash_page",
                page_id=str(page["id"]),
                space_key=space_key,
                version_before=current_version,
                version_after=None,
                outcome="success",
            )
            result["ok"] = True
            return result
        except Exception as exc:
            self._audit_failure(
                operation="trash_page",
                page_id=str(page["id"]),
                space_key=space_key,
                version_before=current_version,
                exc=exc,
            )
            raise

    def restore_page(
        self,
        *,
        page_id: str,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writes()
        page = self.client.get_trashed_page(page_id)
        space_key = str((page.get("space") or {}).get("key") or "")
        if not self.client.supports_trash(space_key):
            raise UnsupportedOperationError(
                "A safe restore operation is not available for this space",
                details={"page_id": page.get("id"), "space_key": space_key},
            )
        arguments = {"page_id": page["id"]}
        if not confirmation_token:
            return self._preview(
                operation="restore_page",
                page=page,
                arguments=arguments,
                summary={
                    "effect": "Restore the page from the Confluence trash",
                    "permanent_delete": False,
                },
            )
        current_version = int((page.get("version") or {}).get("number") or 0)
        self.state.consume_confirmation(
            confirmation_token,
            base_url=self.config.base_url,
            operation="restore_page",
            arguments=arguments,
            page_id=str(page["id"]),
            current_version=current_version,
        )
        try:
            result = self.client.restore_page(page["id"])
            result["audit_id"] = self._audit(
                operation="restore_page",
                page_id=str(page["id"]),
                space_key=space_key,
                version_before=current_version,
                version_after=result.get("version_after"),
                outcome="success",
            )
            result["ok"] = True
            return result
        except Exception as exc:
            self._audit_failure(
                operation="restore_page",
                page_id=str(page["id"]),
                space_key=space_key,
                version_before=current_version,
                exc=exc,
            )
            raise

    def restore_page_version(
        self,
        *,
        page_id: str,
        version_number: int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writes()
        current = self._resolve(page_id)
        target = self.client.get_page_version(current["id"], version_number)
        restored_title = str(target.get("title") or current.get("title") or "")
        self._assert_restore_title_available(
            current=current,
            restored_title=restored_title,
        )
        arguments = {"page_id": current["id"], "version_number": version_number}
        current_version = int((current.get("version") or {}).get("number") or 0)
        if not confirmation_token:
            return self._preview(
                operation="restore_page_version",
                page=current,
                arguments=arguments,
                summary={
                    "effect": (
                        "Restore the selected version's title and body as a new current version. "
                        "The parent page, labels, and space remain unchanged."
                    ),
                    "current_title": current.get("title"),
                    "restored_title": restored_title,
                    "target_version": version_number,
                    "new_version": current_version + 1,
                    "restore_body": True,
                    "restore_title": True,
                },
            )
        self.state.consume_confirmation(
            confirmation_token,
            base_url=self.config.base_url,
            operation="restore_page_version",
            arguments=arguments,
            page_id=str(current["id"]),
            current_version=current_version,
        )
        try:
            updated = self.client.update_page(
                current["id"],
                expected_version=current_version,
                title=restored_title,
                body_storage=str(target.get("body_storage") or ""),
                version_message=f"Restored version {version_number} by Confluence Agent",
            )
            return self._mutation(
                operation="restore_page_version",
                page=updated,
                version_before=current_version,
                action="version_restored",
                extra={"restored_version": version_number},
            )
        except Exception as exc:
            self._audit_failure(
                operation="restore_page_version",
                page_id=str(current["id"]),
                space_key=(current.get("space") or {}).get("key"),
                version_before=current_version,
                exc=exc,
            )
            raise

    def export_page_tree(
        self,
        *,
        ref: str,
        output_dir: str,
        space_key: str | None = None,
        descendants: bool = True,
        max_pages: int = 100,
        include_attachments: bool = False,
        max_attachment_mb: float | None = None,
    ) -> dict[str, Any]:
        root = self._resolve(ref, space_key)
        directory = self._validate_local_path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        max_pages = max(1, min(1000, int(max_pages)))
        pages: list[dict[str, Any]] = [root]
        if descendants and max_pages > 1:
            summaries = self.client.get_children(
                root["id"],
                recursive=True,
                max_items=max_pages - 1,
            )
            for summary in summaries:
                if len(pages) >= max_pages:
                    break
                pages.append(self.client.get_page_by_id(summary["id"]))
        manifest: dict[str, Any] = {
            "ok": True,
            "root": {"id": root["id"], "title": root["title"], "url": root.get("url")},
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "base_url": self.client.base_url,
            "page_count": 0,
            "pages": [],
        }
        for index, page in enumerate(pages, start=1):
            filename = f"{index:04d}-{slugify(str(page.get('title') or 'page'))}-{page['id']}.md"
            target = directory / filename
            write_text(target, page_markdown(page, self.client.base_url))
            record: dict[str, Any] = {
                "id": page["id"],
                "title": page["title"],
                "parent_id": page.get("parent_id"),
                "url": page.get("url"),
                "version": (page.get("version") or {}).get("number"),
                "file": filename,
                "attachments": [],
            }
            if include_attachments:
                attachment_dir = directory / "attachments" / page["id"]
                for attachment in self.client.get_attachments(page["id"]):
                    destination = unique_path(
                        attachment_dir,
                        safe_filename(str(attachment.get("filename") or "attachment")),
                    )
                    written = self.client.download_attachment(
                        attachment,
                        destination,
                        max_bytes=(
                            int(max_attachment_mb * 1024 * 1024)
                            if max_attachment_mb
                            else None
                        ),
                    )
                    record["attachments"].append(
                        {
                            "id": attachment.get("id"),
                            "filename": attachment.get("filename"),
                            "path": str(destination.relative_to(directory)),
                            "bytes": written,
                        }
                    )
            manifest["pages"].append(record)
            manifest["page_count"] = len(manifest["pages"])
        manifest_path = directory / "manifest.json"
        write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        manifest["output_dir"] = str(directory)
        manifest["manifest_path"] = str(manifest_path)
        return manifest

    def tail_audit(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "ok": True,
            "audit_path": str(self.state.audit_path),
            "events": self.state.tail_audit(limit),
        }
