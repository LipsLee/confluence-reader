from __future__ import annotations

import asyncio

from confluence_reader.mcp_server import server


def test_expected_tools_and_hints() -> None:
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {
        "confluence_status",
        "resolve_page",
        "search_pages",
        "get_page",
        "list_children",
        "list_page_versions",
        "get_comments",
        "list_attachments",
        "download_attachment",
        "export_page_tree",
        "create_page",
        "update_page",
        "append_page",
        "add_comment",
        "add_labels",
        "upload_attachment",
        "move_page",
        "trash_page",
        "restore_page",
        "restore_page_version",
    }
    for name in {
        "confluence_status",
        "resolve_page",
        "search_pages",
        "get_page",
        "list_children",
        "list_page_versions",
        "get_comments",
        "list_attachments",
    }:
        assert by_name[name].annotations.read_only_hint is True
    for name in {"move_page", "trash_page", "restore_page", "restore_page_version"}:
        assert by_name[name].annotations.destructive_hint is True
    assert not any("purge" in name for name in by_name)
    assert not any("archive" in name for name in by_name)


def test_status_tool_returns_structured_error_without_config(monkeypatch) -> None:
    for key in (
        "CONFLUENCE_BASE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_API_TOKEN",
        "CONFLUENCE_PERSONAL_ACCESS_TOKEN",
        "CONFLUENCE_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("confluence_reader.mcp_server._service_instance", None)
    result = asyncio.run(server.call_tool("confluence_status", {}))
    assert result.is_error is False
    assert "structured_content" in result.__dict__
