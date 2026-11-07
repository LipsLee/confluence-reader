# MCP and CLI reference

The MCP server is named `confluence`. It uses local `stdio` and exposes the same
operations as the `confluence` CLI. Page ids are strings, page versions are
integers, and timestamps returned by Confluence are kept in their API
representation (normally ISO 8601 UTC).

## MCP tools

### Read-only tools

These tools have `readOnlyHint=true`.

| Tool | Main arguments | Result |
|---|---|---|
| `confluence_status` | `probe=true` | Configuration, authentication probe, detected flavor, capabilities |
| `resolve_page` | `ref`, `space_key?` | Canonical id, title, space, version, URL, ancestors |
| `search_pages` | `query?`, `cql?`, `space_keys?`, `labels?`, `last_modified?`, `limit`, `cursor?` | Permission-filtered results and `next_cursor` |
| `get_page` | `ref`, `space_key?`, `body_format`, `include_comments?`, `include_attachments?` | Page body and metadata |
| `list_children` | `ref`, `space_key?`, `recursive?`, `limit`, `cursor?` | Child or descendant page summaries |
| `list_page_versions` | `ref`, `space_key?`, `limit`, `cursor?` | Historical version metadata |
| `get_comments` | `ref`, `space_key?`, `limit`, `cursor?` | Footer comments with Markdown and Storage bodies |
| `list_attachments` | `ref`, `space_key?`, `limit`, `cursor?` | Attachment metadata and download URLs |

`body_format` for reads is one of `markdown` (default), `storage`, or `both`.
Use `storage` when a page relies on Jira, Draw.io, Include, or another complex
macro whose rendered meaning is not represented in Markdown.

### Local-file tools

These tools read Confluence but write to the local filesystem, so they have
`readOnlyHint=false` and `openWorldHint=false`.

| Tool | Main arguments | Result |
|---|---|---|
| `download_attachment` | `ref`, `destination_path`, `attachment_id?` or `filename?`, `overwrite?`, `space_key?` | Written path and byte count |
| `export_page_tree` | `ref`, `output_dir`, `space_key?`, `descendants?`, `max_pages?`, `include_attachments?` | Markdown files and `manifest.json` |

Existing attachment files are not overwritten unless `overwrite=true`.
`CONFLUENCE_FILE_ROOTS`, when configured, is a hard boundary for these paths.

### Normal write tools

These tools have `readOnlyHint=false`, `destructiveHint=false`, and
`idempotentHint=false`.

| Tool | Main arguments | Notes |
|---|---|---|
| `create_page` | `space_key`, `title`, `body`, `parent_id?`, `body_format`, `version_message?` | Normal page creation |
| `update_page` | `page_id`, `expected_version`, `title?`, `body?`, `body_format`, `version_message?` | `expected_version` is mandatory; a mismatch returns a diff and never overwrites |
| `append_page` | `page_id`, `expected_version`, `body`, `body_format`, `version_message?` | Re-reads the page and appends converted content to the existing Storage body |
| `add_comment` | `page_id`, `body`, `body_format` | Adds a footer comment |
| `add_labels` | `page_id`, `labels` | Adds global labels |
| `upload_attachment` | `page_id`, `file_path`, `replace_existing?`, `expected_attachment_version?`, `comment?` | Replacing an existing filename requires the current attachment version |

Markdown is the default body format. Raw HTML in Markdown is escaped. For a
macro-bearing body, pass `body_format="storage"` with exact Storage XML.
An update is a full-body replacement, not a patch.

### Confirmed destructive tools

These tools have `readOnlyHint=false`, `destructiveHint=true`, and
`idempotentHint=false`.

| Tool | Main arguments | Effect after confirmation |
|---|---|---|
| `move_page` | `page_id`, `new_parent_id`, `confirmation_token?` | Moves within the current space only |
| `trash_page` | `page_id`, `confirmation_token?` | Moves to the Confluence trash; never purges |
| `restore_page` | `page_id`, `confirmation_token?` | Restores a trashed page where supported |
| `restore_page_version` | `page_id`, `version_number`, `confirmation_token?` | Restores historical title and body as a new current version |

First call without `confirmation_token` returns a preview and a one-time token.
Show the preview to the user, obtain explicit approval, then repeat the same call
with identical arguments and the token. Tokens expire after 10 minutes and are
bound to the site, operation, arguments, page, and current version.

## CLI commands

All commands accept the common options `--base-url`, `--config`, `--auth`,
`--flavor`, `--timeout`, `--insecure`, `--json`, and `--verbose` where
applicable. Prefer `--json` for agent parsing.

| Command | Equivalent capability |
|---|---|
| `doctor` | `confluence_status` with a probe |
| `status` | `confluence_status` without a network probe by default |
| `resolve REF` | `resolve_page` |
| `search [QUERY]` | `search_pages` |
| `page REF` | `get_page` |
| `children REF` | `list_children` |
| `tree REF` | Recursive hierarchy display |
| `versions REF` | `list_page_versions` |
| `comments REF` | `get_comments` |
| `attachments REF` | `list_attachments`; `--download-dir` downloads them |
| `export REF --output-dir DIR` | `export_page_tree` |
| `create --space KEY --title TITLE` | `create_page` |
| `update PAGE --expected-version N` | `update_page` |
| `append PAGE --expected-version N` | `append_page` |
| `comment PAGE` | `add_comment` |
| `label PAGE --label NAME` | `add_labels` |
| `upload PAGE --file PATH` | `upload_attachment` |
| `download PAGE --output PATH` | `download_attachment` |
| `move PAGE --parent-id ID` | `move_page` preview/confirm |
| `trash PAGE` | `trash_page` preview/confirm |
| `restore PAGE` | `restore_page` preview/confirm |
| `restore-version PAGE --version N` | `restore_page_version` preview/confirm |
| `audit` | Local audit events |

Body input for CLI writes is `--body TEXT` or `--body-file PATH` (UTF-8), plus
`--body-format markdown|storage`. Destructive CLI commands use
`--confirm-token TOKEN`.

## Pagination

Search, child, version, comment, and attachment lists return an opaque
`next_cursor`. Pass it back unchanged as `cursor` or `--cursor`; do not construct
or decode it as a public API. Cursors are validated against the configured
Confluence origin.

## Stable errors

| Code | Meaning |
|---|---|
| `CONFIG_ERROR` | Missing or invalid local configuration |
| `AUTH_FAILED` | Credentials, token, or auth mode rejected |
| `PERMISSION_DENIED` | Account lacks the required Confluence permission |
| `NOT_FOUND` | Resource is absent or not visible to this account |
| `VERSION_CONFLICT` | The page or attachment changed after it was read |
| `CONFIRMATION_REQUIRED` | A destructive operation needs a valid preview token |
| `CONFIRMATION_EXPIRED` | The token expired, was used, or the page changed |
| `WRITES_DISABLED` | `CONFLUENCE_ENABLE_WRITES=false` or anonymous access |
| `UNSUPPORTED_ON_SITE` | The site cannot perform the operation safely |
| `OUTCOME_UNKNOWN` | A write timed out or lost its connection; read the current state before retrying |

Successful writes include `page_id`, `version_before`, `version_after`,
canonical `url`, and a local `audit_id` where applicable.

## Operation examples

```text
resolve_page(ref="ENG/PLC commissioning")
search_pages(query="Siemens S7", space_keys=["ENG"], labels=["plc"])
get_page(ref="123456", body_format="both")
list_page_versions(ref="123456", limit=10)
update_page(page_id="123456", expected_version=7, body="# Updated")
trash_page(page_id="123456")                 # preview only
trash_page(page_id="123456", confirmation_token="...")  # after explicit approval
```

## Boundaries

There is intentionally no MCP tool for cross-space moves, permanent purge,
page archival, comment editing/deletion, or attachment deletion. A request for
those operations should be refused or handled manually in Confluence by the
user.
