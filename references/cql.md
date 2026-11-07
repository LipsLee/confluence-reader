# Confluence CQL Examples

Use `search --cql '<CQL>'` when the built-in text/space/label filters are not
enough. CQL strings are evaluated by Confluence, so quote values and escape
embedded double quotes with a backslash.

## Common queries

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

The built-in convenience filters generate:

```text
type = page and text ~ "<query>" and space = "<space>" and label = "<label>"
```

## Useful fields

- `text`: full-text content, normally including the title.
- `title`: exact or partial title matching depending on the operator.
- `space`: space key.
- `label`: page label.
- `ancestor`: id of an ancestor page.
- `creator`: account id on Cloud or username on some Data Center deployments.
- `created`, `lastmodified`: dates and relative expressions such as `now("-7d")`.
- `order by lastmodified desc`: sort search results.

## Practical notes

- Start broad, then narrow with `space`, `label`, or `ancestor`.
- Use `children --recursive` or `tree` when you need the page hierarchy rather
  than a full-text search.
- Search results are permission-filtered by Confluence. An empty result does
  not prove that a page is absent.
- If a title search returns multiple pages, pass `--space`.
- CQL operators are case-sensitive in some deployments; use the exact field
  names shown above.

