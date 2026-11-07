from __future__ import annotations

import os
import uuid

import pytest

from confluence_reader.config import Config
from confluence_reader.service import ConfluenceService

pytestmark = pytest.mark.skipif(
    not os.environ.get("CONFLUENCE_TEST_BASE_URL"),
    reason="live Confluence integration is opt-in",
)


def service() -> ConfluenceService:
    environ = dict(os.environ)
    if environ.get("CONFLUENCE_TEST_BASE_URL"):
        environ["CONFLUENCE_BASE_URL"] = environ["CONFLUENCE_TEST_BASE_URL"]
    for suffix in ("EMAIL", "API_TOKEN", "PERSONAL_ACCESS_TOKEN"):
        test_value = environ.get(f"CONFLUENCE_TEST_{suffix}")
        if test_value:
            environ[f"CONFLUENCE_{suffix}"] = test_value
    config = Config.from_env(environ)
    return ConfluenceService(config)


def test_live_search_and_read() -> None:
    svc = service()
    result = svc.search_pages(query="confluence", limit=5)
    assert result["ok"] is True
    if result["results"]:
        page = svc.get_page(result["results"][0]["id"], body_format="markdown")
        assert page["body_markdown"]


@pytest.mark.skipif(
    os.environ.get("CONFLUENCE_TEST_ALLOW_WRITES") != "1",
    reason="destructive integration test requires CONFLUENCE_TEST_ALLOW_WRITES=1",
)
def test_live_write_lifecycle_in_dedicated_space() -> None:
    space = os.environ["CONFLUENCE_TEST_SPACE"]
    svc = service()
    title = f"Confluence Agent Integration {uuid.uuid4().hex[:8]}"
    created = svc.create_page(space_key=space, title=title, body="# Temporary\n\nIntegration test")
    page_id = created["page_id"]
    version = created["version_after"]
    updated = svc.update_page(
        page_id=page_id,
        expected_version=version,
        body="# Temporary\n\nUpdated integration test",
    )
    preview = svc.trash_page(page_id=page_id)
    svc.trash_page(page_id=page_id, confirmation_token=preview["confirmation_token"])
    assert updated["version_after"] > version
