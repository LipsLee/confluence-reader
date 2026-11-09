# Changelog

## 2.1.0

- Added `confluence setup`, an interactive wizard that validates Cloud and Data
  Center settings, reads tokens with hidden input, writes a user-only config,
  tests access, and prints an optional MCP configuration.
- Added a three-step quick start for distribution and first-run guidance that
  keeps credentials out of agent chat and does not reuse browser cookies.
- Preserved manual environment-variable and JSON configuration for automation.

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
