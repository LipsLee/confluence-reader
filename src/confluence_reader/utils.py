"""Shared formatting, parsing, and filesystem helpers."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .errors import ValidationError
from .markdown import storage_to_markdown

PAGE_EXPAND = "space,version,ancestors,metadata.labels,body.storage"


def parse_page_ref(ref: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(page_id, space_key, title)`` parsed from a URL or reference."""

    ref = (ref or "").strip()
    if ref.isdigit():
        return ref, None, None
    if ref.startswith(("http://", "https://")):
        parsed = urlparse(ref)
        query = parse_qs(parsed.query)
        page_id = (query.get("pageId") or [None])[0]
        if page_id and str(page_id).isdigit():
            return str(page_id), None, None
        patterns = (
            r"/pages/(\d+)(?:/|$)",
            r"/content/(\d+)(?:/|$)",
            r"/pageId=(\d+)(?:/|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, parsed.path)
            if match:
                return match.group(1), None, None
        match = re.search(r"/spaces/([^/]+)/pages/(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(2), match.group(1), None
        match = re.search(r"/display/([^/]+)/(.+)$", parsed.path)
        if match:
            return None, match.group(1), unquote(match.group(2).replace("+", " "))
        if parsed.path.rstrip("/").endswith("/viewpage.action"):
            title = (query.get("title") or query.get("pageTitle") or [None])[0]
            if title:
                return None, None, title
        return None, None, None
    if "/" in ref:
        space, title = ref.split("/", 1)
        if space.strip() and title.strip():
            return None, space.strip(), title.strip()
    return None, None, ref


def cql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_cql(
    *,
    query: str | None,
    cql: str | None,
    space_keys: Iterable[str] | None,
    labels: Iterable[str] | None,
    last_modified: str | None = None,
) -> str:
    if cql and query:
        raise ValidationError("Pass either a text query or cql, not both")
    if cql:
        return cql.strip()
    parts = ["type = page"]
    if query:
        parts.append(f'text ~ "{cql_escape(query)}"')
    spaces = [item.strip() for item in (space_keys or []) if item.strip()]
    if len(spaces) == 1:
        parts.append(f'space = "{cql_escape(spaces[0])}"')
    elif len(spaces) > 1:
        quoted = ", ".join(f'"{cql_escape(item)}"' for item in spaces)
        parts.append(f"space in ({quoted})")
    for label in labels or []:
        if label.strip():
            parts.append(f'label = "{cql_escape(label.strip())}"')
    if last_modified:
        value = last_modified.strip()
        if "(" in value or value.startswith('"'):
            date_expression = value
        else:
            date_expression = f'"{cql_escape(value)}"'
        parts.append(f"lastmodified >= {date_expression}")
    parts.append("order by lastmodified desc")
    return " and ".join(parts[:-1]) + " " + parts[-1]


def encode_cursor(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> str:
    raw = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor") from exc


def slugify(value: str, *, fallback: str = "page", max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char.isalnum() or category.startswith(("L", "N")):
            chars.append(char)
        elif char in {"-", "_"}:
            chars.append(char)
        else:
            chars.append("-")
    result = re.sub(r"-{2,}", "-", "".join(chars)).strip(" .-_")[:max_length].rstrip(" .")
    result = result or fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    return f"_{result}" if result.upper() in reserved else result


def safe_filename(value: str, *, fallback: str = "attachment") -> str:
    value = Path(value or "").name
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    value = (value or fallback)[:180].rstrip(" .") or fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    return f"_{value}" if value.upper() in reserved else value


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = directory / f"{path.stem}-{index}{path.suffix}"
        if not candidate.exists():
            return candidate
    raise ValidationError(f"Too many files with the same name: {filename}")


def format_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def page_breadcrumb(page: dict[str, Any]) -> str:
    space = page.get("space") or {}
    bits = [space.get("name") or space.get("key") or ""]
    bits.extend(str(item.get("title") or "") for item in page.get("ancestors") or [])
    bits.append(str(page.get("title") or ""))
    return " / ".join(bit for bit in bits if bit)


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def page_markdown(page: dict[str, Any], base_url: str) -> str:
    labels = page.get("labels") or []
    space = page.get("space") or {}
    version = page.get("version") or {}
    frontmatter = [
        "---",
        f'confluence_id: "{yaml_escape(str(page.get("id", "")))}"',
        f'title: "{yaml_escape(str(page.get("title", "")))}"',
        f'space_key: "{yaml_escape(str(space.get("key", "")))}"',
        f'space_name: "{yaml_escape(str(space.get("name", "")))}"',
        f'version: {version.get("number") if version.get("number") is not None else "null"}',
        f'updated: "{yaml_escape(str(page.get("updated", "")))}"',
        f'author: "{yaml_escape(str(page.get("author", "")))}"',
        f'url: "{yaml_escape(str(page.get("url", "")))}"',
    ]
    if page.get("parent_id"):
        frontmatter.append(f'parent_id: "{yaml_escape(str(page["parent_id"]))}"')
    if labels:
        frontmatter.append("labels:")
        frontmatter.extend(f'  - "{yaml_escape(str(label))}"' for label in labels)
    else:
        frontmatter.append("labels: []")
    frontmatter.append("---")
    title = str(page.get("title") or "Untitled")
    body = storage_to_markdown(str(page.get("body_storage") or ""), base_url)
    source = f"[{space.get('key') or 'Confluence'}] {page_breadcrumb(page)}"
    if page.get("url"):
        source = f"{source}\n\nSource: {page['url']}"
    return "\n".join(frontmatter) + f"\n\n# {title}\n\n{source}\n\n{body}".rstrip() + "\n"


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def attachment_download_name(item: dict[str, Any]) -> str:
    return safe_filename(str(item.get("filename") or item.get("id") or "attachment"))


def excerpt_from_search(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def quote_path(value: str) -> str:
    return quote_plus(value)
