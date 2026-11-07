from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from confluence_reader.config import Config
from confluence_reader.state import StateStore


def make_config(tmp_path: Path, *, writes: bool = True, auth: str = "bearer") -> Config:
    return Config(
        base_url="https://example.atlassian.net/wiki",
        personal_access_token="test-token",
        auth=auth,
        enable_writes=writes,
        state_dir=tmp_path / "state",
        file_roots=[tmp_path],
        flavor="cloud",
    )


def make_page(
    page_id: str = "100",
    *,
    title: str = "Page",
    space_key: str = "DOC",
    version: int = 1,
    body: str = "<p>Hello</p>",
    parent_id: str | None = None,
    ancestors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": page_id,
        "type": "page",
        "status": "current",
        "title": title,
        "space": {"id": "1", "key": space_key, "name": space_key},
        "version": {"number": version, "message": "", "when": "", "by": "Tester"},
        "updated": "",
        "author": "Tester",
        "ancestors": ancestors or [],
        "parent_id": parent_id,
        "labels": [],
        "url": f"https://example.atlassian.net/wiki/pages/{page_id}",
        "body_storage": body,
    }


class FakeClient:
    base_url = "https://example.atlassian.net/wiki"
    flavor = "cloud"

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {
            "100": make_page("100", title="Source", parent_id="10"),
            "200": make_page("200", title="Target", parent_id="10"),
            "300": make_page("300", title="Other space", space_key="OTHER", parent_id="30"),
            "120": make_page("120", title="Child", parent_id="100", ancestors=[{"id": "100", "title": "Source"}]),
            "10": make_page("10", title="Parent", parent_id=None),
        }
        self.updated_payloads: list[dict[str, Any]] = []
        self.created_payloads: list[dict[str, Any]] = []
        self.trashed: list[str] = []

    def resolve_page(self, ref: str, space: str | None = None) -> dict[str, Any]:
        page = self.pages[str(ref)]
        return copy.deepcopy(page)

    def get_page_by_id(self, page_id: str, status: str = "current") -> dict[str, Any]:
        page = copy.deepcopy(self.pages[str(page_id)])
        page["status"] = status
        return page

    def get_page_by_title(self, space: str, title: str) -> dict[str, Any]:
        for page in self.pages.values():
            if page["space"]["key"] == space and page["title"] == title:
                return copy.deepcopy(page)
        from confluence_reader.errors import NotFoundError

        raise NotFoundError(f"Page not found in space {space}: {title}")

    def get_trashed_page(self, page_id: str) -> dict[str, Any]:
        page = copy.deepcopy(self.pages[str(page_id)])
        page["status"] = "trashed"
        return page

    def _same_space_parent(self, page: dict[str, Any], parent_id: str) -> dict[str, Any]:
        parent = self.get_page_by_id(parent_id)
        if page["space"]["key"] != parent["space"]["key"]:
            from confluence_reader.errors import ValidationError

            raise ValidationError("Cross-space page moves are not supported")
        if str(page["id"]) == parent_id:
            from confluence_reader.errors import ValidationError

            raise ValidationError("A page cannot be moved under itself")
        if any(str(item["id"]) == str(page["id"]) for item in parent.get("ancestors") or []):
            from confluence_reader.errors import ValidationError

            raise ValidationError("A page cannot be moved under one of its descendants")
        return parent

    def create_page(
        self,
        *,
        space_key: str,
        title: str,
        body_storage: str,
        parent_id: str | None = None,
        version_message: str = "",
    ) -> dict[str, Any]:
        page_id = str(1000 + len(self.pages))
        self.pages[page_id] = make_page(
            page_id,
            title=title,
            space_key=space_key,
            body=body_storage,
            parent_id=parent_id,
        )
        self.created_payloads.append(
            {
                "space_key": space_key,
                "title": title,
                "body_storage": body_storage,
                "parent_id": parent_id,
            }
        )
        return copy.deepcopy(self.pages[page_id])

    def update_page(
        self,
        page_id: str,
        *,
        expected_version: int,
        title: str,
        body_storage: str,
        version_message: str = "",
        parent_id: str | None = None,
        status: str = "current",
        restore_only: bool = False,
    ) -> dict[str, Any]:
        page = self.pages[str(page_id)]
        if int(page["version"]["number"]) != expected_version:
            from confluence_reader.errors import VersionConflictError

            raise VersionConflictError("conflict")
        page["title"] = title
        page["body_storage"] = body_storage
        page["version"]["number"] = expected_version + 1
        if parent_id:
            page["parent_id"] = parent_id
        self.updated_payloads.append(
            {
                "page_id": str(page_id),
                "expected_version": expected_version,
                "title": title,
                "body_storage": body_storage,
                "parent_id": parent_id,
            }
        )
        return copy.deepcopy(page)

    def get_page_version(self, page_id: str, version_number: int) -> dict[str, Any]:
        page = copy.deepcopy(self.pages[str(page_id)])
        page["title"] = f"Old title v{version_number}"
        page["body_storage"] = f"<p>Old body v{version_number}</p>"
        page["version"]["number"] = version_number
        return page

    def supports_trash(self, space_key: str) -> bool:
        return True

    def trash_page(self, page_id: str) -> dict[str, Any]:
        self.trashed.append(str(page_id))
        page = self.pages[str(page_id)]
        return {
            "ok": True,
            "page_id": str(page_id),
            "title": page["title"],
            "space_key": page["space"]["key"],
            "version_before": page["version"]["number"],
            "version_after": None,
            "url": page["url"],
            "action": "trashed",
        }

    def restore_page(self, page_id: str) -> dict[str, Any]:
        page = self.pages[str(page_id)]
        return {
            "ok": True,
            "page_id": str(page_id),
            "title": page["title"],
            "space_key": page["space"]["key"],
            "version_before": page["version"]["number"],
            "version_after": page["version"]["number"] + 1,
            "url": page["url"],
            "action": "restored",
        }

    def add_comment(self, page_id: str, body_storage: str) -> dict[str, Any]:
        return {"ok": True, "comment_id": "900", "page_id": str(page_id), "url": ""}

    def add_labels(self, page_id: str, labels: list[str]) -> dict[str, Any]:
        return {"ok": True, "page_id": str(page_id), "labels": list(labels)}

    def get_attachments(self, page_id: str, max_items: int = 500) -> list[dict[str, Any]]:
        return [
            {
                "id": "5",
                "filename": "drawing.pdf",
                "version": 1,
                "download_url": "https://example.atlassian.net/wiki/download/5",
            }
        ]

    def download_attachment(
        self,
        attachment: dict[str, Any],
        destination: Path,
        *,
        overwrite: bool = False,
        max_bytes: int | None = None,
    ) -> int:
        if destination.exists() and not overwrite:
            from confluence_reader.errors import ValidationError

            raise ValidationError("Destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"pdf")
        return 3

    def upload_attachment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "attachment": {"id": "6", "filename": "new.txt", "version": 1},
            "action": "created",
        }

    def get_children(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.pages["120"])]

    def get_children_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "children": [copy.deepcopy(self.pages["120"])], "next_cursor": None}

    def get_comments_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"count": 0, "comments": [], "next_cursor": None}

    def get_attachments_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"count": 0, "attachments": [], "next_cursor": None}

    def list_versions_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "versions": [{"number": 1, "message": "", "created": ""}], "next_cursor": None}

    def search_page(self, *, cql: str, limit: int, cursor: str | None) -> dict[str, Any]:
        return {"cql": cql, "count": 1, "results": [], "next_cursor": None}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def state(config: Config) -> StateStore:
    return StateStore(config.state_dir)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
