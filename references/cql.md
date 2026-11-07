# Search and CQL

`search_pages` and the CLI `search` command accept either a plain text query or
raw CQL. They are mutually exclusive; the convenience filters are combined with
AND.

## Plain text and filters

```text
search_pages(query="PLC commissioning", space_keys=["ENG"], labels=["plc"], limit=25)
search_pages(query="Siemens S7", last_modified='now("-30d")')
```

CLI:

```powershell
uv run --project $skill confluence search "PLC commissioning" `
  --space ENG --label plc --last-modified 'now("-30d")' --json
```

The generated CQL is:

```text
type = page and text ~ "PLC commissioning" and space = "ENG"
  and label = "plc" and lastmodified >= now("-30d")
  order by lastmodified desc
```

Use `--space A,B` or `space_keys=["A", "B"]` for multiple spaces. Multiple
labels are emitted as multiple `label =` predicates. An ISO date is quoted
automatically; a date expression containing parentheses, such as
`now("-30d")`, is passed through as an expression.

## Raw CQL

Use raw CQL when the convenience filters are insufficient. Escape embedded
double quotes with a backslash.

```text
type = page and text ~ "deployment"
type = page and space = "DOC" and text ~ "deployment"
type = page and title = "Release checklist"
type = page and label = "runbook" and space = "OPS"
type = page and ancestor = 123456
type = page and creator = "557058:ab12..."
type = page and lastmodified >= now("-30d")
type = page and created >= "2026-01-01"
type = page and space in ("DOC", "OPS") order by lastmodified desc
```

CLI:

```powershell
uv run --project $skill confluence search --cql 'type = page and text ~ "Siemens"' --json
```

## Useful fields

- `text`: full-text content, normally including the title.
- `title`: title matching.
- `space`: space key.
- `label`: page label.
- `ancestor`: id of an ancestor page; useful for a page tree.
- `creator`: account id on Cloud or username on some Data Center deployments.
- `created`, `lastmodified`: dates and relative expressions such as
  `now("-7d")`.
- `order by lastmodified desc`: sort newest first.

## PLC and engineering research pattern

1. Search product names, model numbers, standards, error codes, and likely
   labels across all visible spaces.
2. Narrow by `space`, `label`, or `ancestor` after inspecting the first results.
3. Resolve the strongest candidate and read its page tree and version history.
4. Use `get_page` with `body_format="storage"` when a Jira, Draw.io, Include, or
   generated table macro carries meaning that Markdown loses.
5. Cite the page title, space, version, update time, and canonical URL.

An empty result does not prove that a page is absent. It may be unindexed,
outside the account's permissions, or represented only by an attachment.
