from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_mcp_stdio_initializes_and_lists_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "https://example.atlassian.net/wiki",
                "auth": "none",
                "state_dir": str(tmp_path / "state"),
            }
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["CONFLUENCE_CONFIG"] = str(config_path)

    async def run() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "confluence_reader.mcp_server"],
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "confluence"
                tools = await session.list_tools()
                assert len(tools.tools) == 20
                assert {tool.name for tool in tools.tools} >= {
                    "confluence_status",
                    "search_pages",
                    "update_page",
                    "trash_page",
                }

    asyncio.run(asyncio.wait_for(run(), timeout=30))
