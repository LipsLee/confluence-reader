from __future__ import annotations

from pathlib import Path

import pytest

from confluence_reader.errors import ConfirmationError, ConfirmationExpiredError
from confluence_reader.state import StateStore


def test_confirmation_is_bound_and_single_use(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    arguments = {"page_id": "100", "new_parent_id": "200"}
    token, expires = store.create_confirmation(
        base_url="https://example.com",
        operation="move_page",
        arguments=arguments,
        page_id="100",
        expected_version=2,
        preview={"effect": "move"},
    )
    assert expires
    with pytest.raises(ConfirmationError):
        store.consume_confirmation(
            token,
            base_url="https://example.com",
            operation="move_page",
            arguments={"page_id": "100", "new_parent_id": "300"},
            page_id="100",
            current_version=2,
        )
    preview = store.consume_confirmation(
        token,
        base_url="https://example.com",
        operation="move_page",
        arguments=arguments,
        page_id="100",
        current_version=2,
    )
    assert preview["effect"] == "move"
    with pytest.raises(ConfirmationExpiredError):
        store.consume_confirmation(
            token,
            base_url="https://example.com",
            operation="move_page",
            arguments=arguments,
            page_id="100",
            current_version=2,
        )


def test_confirmation_expires_when_version_changes(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    token, _ = store.create_confirmation(
        base_url="https://example.com",
        operation="trash_page",
        arguments={"page_id": "100"},
        page_id="100",
        expected_version=1,
        preview={"effect": "trash"},
    )
    with pytest.raises(ConfirmationExpiredError):
        store.consume_confirmation(
            token,
            base_url="https://example.com",
            operation="trash_page",
            arguments={"page_id": "100"},
            page_id="100",
            current_version=2,
        )


def test_audit_contains_no_body_or_token(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.record_audit(
        base_url="https://example.com",
        operation="update_page",
        page_id="100",
        space_key="DOC",
        version_before=1,
        version_after=2,
        outcome="success",
    )
    line = store.audit_path.read_text(encoding="utf-8")
    assert "update_page" in line
    assert "token" not in line.lower()
    assert "body" not in line.lower()
