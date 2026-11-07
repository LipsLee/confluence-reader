from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClient, make_config

from confluence_reader.errors import (
    ConfirmationExpiredError,
    ConflictError,
    ValidationError,
    VersionConflictError,
    WritesDisabledError,
)
from confluence_reader.service import ConfluenceService
from confluence_reader.state import StateStore


def service(tmp_path: Path, client: FakeClient, *, writes: bool = True) -> ConfluenceService:
    config = make_config(tmp_path, writes=writes)
    return ConfluenceService(config, client=client, state=StateStore(config.state_dir))


def test_update_requires_matching_version_and_returns_diff(tmp_path: Path) -> None:
    client = FakeClient()
    client.pages["100"]["version"]["number"] = 2
    svc = service(tmp_path, client)
    with pytest.raises(VersionConflictError) as error:
        svc.update_page(
            page_id="100",
            expected_version=1,
            body="New body",
        )
    assert error.value.details["current_version"] == 2
    assert "proposed-v2" in error.value.details["diff"]
    assert client.updated_payloads == []


def test_update_success_creates_audit_entry(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    result = svc.update_page(
        page_id="100",
        expected_version=1,
        title="Updated",
        body="New body",
    )
    assert result["version_after"] == 2
    assert result["audit_id"]
    assert "New body" in client.updated_payloads[0]["body_storage"]
    assert svc.tail_audit()["events"][0]["operation"] == "update_page"


def test_append_requires_expected_version(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    with pytest.raises(VersionConflictError):
        svc.append_page(page_id="100", expected_version=9, body="Tail")


def test_move_preview_then_confirm(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    preview = svc.move_page(page_id="100", new_parent_id="200")
    assert preview["action"] == "preview"
    assert preview["confirmation_required"] is True
    assert client.updated_payloads == []
    moved = svc.move_page(
        page_id="100",
        new_parent_id="200",
        confirmation_token=preview["confirmation_token"],
    )
    assert moved["action"] == "moved"
    assert client.updated_payloads[0]["parent_id"] == "200"


def test_move_rejects_cross_space_and_cycle(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    with pytest.raises(ValidationError):
        svc.move_page(page_id="100", new_parent_id="300")
    with pytest.raises(ValidationError):
        svc.move_page(page_id="100", new_parent_id="120")


def test_trash_preview_does_not_mutate(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    preview = svc.trash_page(page_id="100")
    assert preview["confirmation_required"] is True
    assert client.trashed == []
    result = svc.trash_page(
        page_id="100",
        confirmation_token=preview["confirmation_token"],
    )
    assert result["action"] == "trashed"
    assert client.trashed == ["100"]


def test_confirmation_fails_after_version_change(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    preview = svc.trash_page(page_id="100")
    client.pages["100"]["version"]["number"] = 2
    with pytest.raises(ConfirmationExpiredError):
        svc.trash_page(
            page_id="100",
            confirmation_token=preview["confirmation_token"],
        )


def test_restore_version_preview_and_apply(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    preview = svc.restore_page_version(page_id="100", version_number=1)
    assert preview["summary"]["restore_title"] is True
    result = svc.restore_page_version(
        page_id="100",
        version_number=1,
        confirmation_token=preview["confirmation_token"],
    )
    assert result["action"] == "version_restored"
    assert client.updated_payloads[0]["title"] == "Old title v1"


def test_restore_version_rejects_a_title_already_used_by_another_page(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    original = client.get_page_version

    def version_with_conflicting_title(page_id: str, version_number: int) -> dict:
        page = original(page_id, version_number)
        page["title"] = "Target"
        return page

    client.get_page_version = version_with_conflicting_title  # type: ignore[method-assign]
    svc = service(tmp_path, client)
    with pytest.raises(ConflictError):
        svc.restore_page_version(page_id="100", version_number=1)
    assert client.updated_payloads == []


def test_write_disabled_returns_specific_error(tmp_path: Path) -> None:
    client = FakeClient()
    cfg = make_config(tmp_path, writes=False)
    svc = ConfluenceService(cfg, client=client, state=StateStore(cfg.state_dir))
    with pytest.raises(WritesDisabledError):
        svc.create_page(space_key="DOC", title="Nope", body="Body")


def test_local_path_must_be_inside_configured_root(tmp_path: Path) -> None:
    client = FakeClient()
    svc = service(tmp_path, client)
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValidationError):
        svc.download_attachment(ref="100", destination_path=str(outside), filename="drawing.pdf")
