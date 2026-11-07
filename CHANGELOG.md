# Changelog

## 2.0.0

- Added a local `stdio` MCP server with 20 read, write, and confirmed
  destructive tools.
- Added Confluence Cloud and Data Center/Server support through a shared client.
- Added Markdown-to-Storage and Storage-to-Markdown conversion.
- Added version-checked `update_page` and `append_page`.
- Added two-stage confirmation for move, trash, restore, and historical version
  restore.
- Added local audit records and redacted, origin-isolated HTTP handling.
- Kept the existing read-only CLI commands and `scripts/confluence.py` entry
  point compatible.
- Added PLC/technical research workflow guidance and MCP client documentation.

Writes are enabled by default for authenticated configurations. Set
`CONFLUENCE_ENABLE_WRITES=false` to restore v1's read-only behavior. Anonymous
access remains read-only.

## 1.0.0

- Initial read-only Confluence Reader Codex skill.
