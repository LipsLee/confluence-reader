---
name: confluence-reader
description: "Read, search, inspect, and export Confluence pages via the REST API. Use when a user supplies a Confluence URL, page id, space/title, or asks to search Confluence, read page hierarchy, comments, attachments, or export documentation. Read-only: does not create, edit, move, or delete pages."
---

# Confluence Reader

Use this skill for read-only Confluence work. It supports Confluence Cloud and
Data Center/Server, converts storage-format pages to Markdown, and writes
Markdown exports when requested.

## Check configuration first

Set the skill directory once:

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
```

Then verify access:

```powershell
python "$skill\scripts\confluence.py" doctor
```

If this reports a configuration error, read [references/setup.md](references/setup.md).
Ask the user only for missing non-secret facts such as the base URL or space key.
Never ask the user to paste an API token or password into chat; ask them to set
the environment variable or create the config file locally.

The base URL normally ends in `/wiki` on Cloud, for example
`https://your-site.atlassian.net/wiki`. On Data Center it may include a context
path such as `/confluence`.

## Choose the command

The main entry point is `scripts/confluence.py`. Use `--json` whenever another
step needs to parse the result. Default page output is Markdown with a YAML
metadata header.

```powershell
# Read one page by URL, id, SPACE/Title, or exact title
python "$skill\scripts\confluence.py" page "<URL-or-reference>" --json

# Resolve a page reference without reading the whole body
python "$skill\scripts\confluence.py" resolve "<URL-or-reference>" --json

# Search with text, space, and labels
python "$skill\scripts\confluence.py" search "release process" --space DOC --json

# Use raw CQL for ancestor, date, title, or compound queries
python "$skill\scripts\confluence.py" search --cql 'type = page and space = "DOC" and text ~ "release"' --json

# Inspect hierarchy
python "$skill\scripts\confluence.py" children "<URL-or-reference>" --recursive --json
python "$skill\scripts\confluence.py" tree "<URL-or-reference>" --max-depth 4 --json

# Read comments or list/download attachments
python "$skill\scripts\confluence.py" comments "<URL-or-reference>" --json
python "$skill\scripts\confluence.py" attachments "<URL-or-reference>" --json
python "$skill\scripts\confluence.py" attachments "<URL-or-reference>" --download-dir "C:\path\to\attachments"

# Export a page and all descendants to Markdown
python "$skill\scripts\confluence.py" export "<URL-or-reference>" --descendants --output-dir "C:\path\to\export"

# Write one page to a file
python "$skill\scripts\confluence.py" page "<URL-or-reference>" --output "C:\path\to\page.md"
```

For detailed CQL examples and operator notes, read
[references/cql.md](references/cql.md) only when the search needs them.

## Reading workflow

1. Prefer a page URL or id. `SPACE/Title` is also accepted when the title is
   unambiguous; use `--space` if a bare title matches multiple pages.
2. Use `page --json` to obtain `body_markdown`, metadata, labels, ancestors,
   and the canonical URL in one call.
3. If a page contains complex macros and the Markdown version loses meaning,
   request `--body-format storage` or `--body-format both`. Treat storage output
   as exact source, not as normal prose.
4. For a documentation area, search or resolve the root page first, then use
   `tree` to scope the work. Use `export --descendants` when many pages must be
   read consistently.
5. When summarizing, include the page title, space, version or updated date,
   and canonical URL. Preserve uncertainty when a macro or attachment could not
   be rendered.

## Exports and attachments

`export` writes one Markdown file per page plus `manifest.json`. The manifest
records ids, hierarchy, source URLs, and downloaded attachment paths. Exports
are incremental by page count but do not delete stale files already in the
destination.

Attachment metadata is read by default. Add `--download-dir` or
`--attachments` to download bytes. Use `--max-size-mb` or
`--max-attachment-mb` to keep unexpected large files bounded. The default limit
is 50 MB per file.

## Safety and failure handling

- The skill is strictly read-only. Do not use it to create, update, move, or
  delete Confluence content.
- Never print `Authorization`, API tokens, passwords, or the raw config file.
- A `401` means authentication failed; a `403` means the account lacks
  permission; a `404` can also mean a restricted page is hidden.
- If a short URL cannot be resolved, ask for the canonical `/pages/<id>/` URL.
- Keep `--max-pages` and hierarchy limits in place unless the user explicitly
  requests a larger export.
