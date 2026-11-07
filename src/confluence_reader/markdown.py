#!/usr/bin/env python3
"""Convert between Confluence Storage Format and readable Markdown."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import quote_plus

from markdown_it import MarkdownIt

_EMOTICONS = {
    "smile": ":)",
    "laugh": ":D",
    "sad": ":(",
    "wink": ";)",
    "thumbs-up": ":+1:",
    "thumbs-down": ":-1:",
    "info": "(i)",
    "check": "(check)",
    "cross": "(x)",
    "warning": "(!)",
    "plus": "(+)",
    "minus": "(-)",
    "question": "(?)",
}

_MACRO_LABELS = {
    "info": "NOTE",
    "note": "NOTE",
    "tip": "TIP",
    "warning": "WARNING",
    "panel": "NOTE",
    "success": "NOTE",
}

_RAW_MACROS = {"code", "noformat", "view-file"}


class _StorageParser(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url.rstrip("/")
        self.targets: list[dict[str, object]] = [{"kind": "root", "parts": []}]
        self.raw_depth = 0
        self.pre_depth = 0
        self.skip_depth = 0
        self.macros: list[dict[str, object]] = []
        self.links: list[dict[str, object]] = []
        self.images: list[dict[str, object]] = []
        self.tables: list[dict[str, object]] = []
        self.lists: list[dict[str, int | bool]] = []
        self.tasks: list[dict[str, object]] = []
        self.blockquotes: list[list[str]] = []

    def convert(self, value: str) -> str:
        self.feed(value or "")
        self.close()
        return _cleanup("".join(self.targets[0]["parts"]))  # type: ignore[arg-type]

    def _push(self, target: dict[str, object], *, raw: bool = False) -> None:
        self.targets.append(target)
        if raw:
            self.raw_depth += 1

    def _pop(self) -> dict[str, object]:
        target = self.targets.pop()
        if target.get("raw") and self.raw_depth:
            self.raw_depth -= 1
        return target

    def _append(self, value: str, *, raw: bool = False) -> None:
        if not value:
            return
        target = self.targets[-1]
        parts = target["parts"]
        assert isinstance(parts, list)
        is_raw = raw or self.raw_depth > 0 or bool(target.get("raw"))
        if not is_raw:
            value = re.sub(r"\s+", " ", value)
        parts.append(value)

    def _block(self, count: int = 2) -> None:
        self._append("\n" * max(1, count), raw=True)

    def _target_text(self) -> str:
        parts = self.targets[-1]["parts"]
        assert isinstance(parts, list)
        return "".join(str(part) for part in parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): (value or "") for key, value in attrs}

        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if self.pre_depth:
            if tag == "br":
                self._append("\n", raw=True)
            return

        if tag == "pre":
            self._block()
            self._push({"kind": "pre", "parts": [], "raw": True}, raw=True)
            self.pre_depth = 1
            return

        if tag == "ac:structured-macro":
            name = attr.get("ac:name", "").strip()
            self._block()
            macro: dict[str, object] = {
                "name": name,
                "params": {},
                "body": [],
                "raw": name in _RAW_MACROS,
            }
            self.macros.append(macro)
            self._push(
                {"kind": "macro_body", "parts": macro["body"], "raw": macro["raw"]},
                raw=bool(macro["raw"]),
            )
            return

        if tag == "ac:parameter" and self.macros:
            self.macros[-1]["_param_name"] = attr.get("ac:name", "")
            self._push(
                {"kind": "macro_param", "parts": [], "raw": bool(self.macros[-1].get("raw"))},
                raw=bool(self.macros[-1].get("raw")),
            )
            return

        if tag in {"ac:rich-text-body", "ac:plain-text-body"}:
            return

        if tag == "ac:link":
            self.links.append(
                {
                    "title": "",
                    "space": "",
                    "filename": "",
                    "url": "",
                    "user": "",
                    "anchor": attr.get("ac:anchor", ""),
                    "body": [],
                }
            )
            self._push({"kind": "link_body", "parts": self.links[-1]["body"]})
            return

        if tag == "ac:link-body" or tag == "ac:plain-text-link-body":
            return

        if tag == "ac:image":
            self.images.append(
                {
                    "alt": attr.get("ac:alt", ""),
                    "title": attr.get("ac:title", ""),
                    "filename": "",
                    "url": "",
                    "body": [],
                }
            )
            self._push({"kind": "image_body", "parts": self.images[-1]["body"]})
            return

        if tag == "ri:page" and self.links:
            self.links[-1]["title"] = attr.get("ri:content-title", "")
            self.links[-1]["space"] = attr.get("ri:space-key", "")
            return

        if tag == "ri:attachment":
            filename = attr.get("ri:filename", "")
            if self.images:
                self.images[-1]["filename"] = filename
            if self.links:
                self.links[-1]["filename"] = filename
            return

        if tag == "ri:url":
            value = attr.get("ri:value", "")
            if self.images:
                self.images[-1]["url"] = value
            if self.links:
                self.links[-1]["url"] = value
            return

        if tag == "ri:user" and self.links:
            self.links[-1]["user"] = (
                attr.get("ri:username")
                or attr.get("ri:userkey")
                or attr.get("ri:account-id")
                or "user"
            )
            return

        if tag == "a":
            self.links.append(
                {
                    "title": "",
                    "space": "",
                    "filename": "",
                    "url": attr.get("href", ""),
                    "user": "",
                    "anchor": "",
                    "body": [],
                }
            )
            self._push({"kind": "link_body", "parts": self.links[-1]["body"]})
            return

        if tag == "img":
            src = attr.get("src", "")
            if src:
                alt = attr.get("alt", "")
                self._append(f"![{_escape_alt(alt)}]({src})", raw=True)
            return

        if tag == "table":
            self._block()
            self.tables.append({"rows": [], "row": None, "header": False})
            return
        if tag == "tr":
            if self.tables:
                self.tables[-1]["row"] = []
            return
        if tag in {"th", "td"}:
            if self.tables:
                if tag == "th":
                    self.tables[-1]["header"] = True
                self._push({"kind": "table_cell", "parts": [], "align": attr.get("style", "")})
            return

        if tag in {"ul", "ol"}:
            if not self.lists or self.targets[-1]["kind"] != "table_cell":
                self._block(1)
            self.lists.append({"ordered": tag == "ol", "index": 0})
            return

        if tag == "li":
            if self.lists:
                current = self.lists[-1]
                current["index"] = int(current["index"]) + 1
                indent = "  " * (len(self.lists) - 1)
                marker = f"{current['index']}." if current["ordered"] else "-"
                current_text = "".join(self.targets[-1]["parts"])  # type: ignore[arg-type]
                prefix = "" if current_text.endswith("\n") else "\n"
                self._append(f"{prefix}{indent}{marker} ", raw=True)
            return

        if tag == "blockquote":
            self._block()
            self.blockquotes.append([])
            self._push({"kind": "blockquote", "parts": self.blockquotes[-1]})
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._block()
            self._append("#" * int(tag[1]) + " ")
            return

        if tag in {"p", "div", "section", "article", "header", "footer", "main"}:
            self._block()
            return

        if tag == "br":
            self._append("\n", raw=True)
            return
        if tag == "hr":
            self._append("\n\n---\n\n", raw=True)
            return
        if tag in {"strong", "b"}:
            self._append("**", raw=True)
            return
        if tag in {"em", "i"}:
            self._append("*", raw=True)
            return
        if tag in {"s", "strike", "del"}:
            self._append("~~", raw=True)
            return
        if tag == "code":
            self._append("`", raw=True)
            return
        if tag == "sup":
            self._append("<sup>", raw=True)
            return
        if tag == "sub":
            self._append("<sub>", raw=True)
            return

        if tag == "ac:task-list":
            self._block()
            return
        if tag == "ac:task":
            task: dict[str, object] = {"body": [], "status": "incomplete"}
            self.tasks.append(task)
            self._push({"kind": "task_body", "parts": task["body"]})
            return
        if tag == "ac:task-id" and self.tasks:
            self._push({"kind": "ignore", "parts": []})
            return
        if tag == "ac:task-status" and self.tasks:
            self._push({"kind": "task_status", "parts": []})
            return

        if tag == "ac:emoticon":
            name = attr.get("ac:name", "")
            self._append(_EMOTICONS.get(name, f":{name}:" if name else ""), raw=True)
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return

        if self.pre_depth and tag != "pre":
            return

        if tag == "pre":
            if self.pre_depth:
                self.pre_depth = 0
                target = self._pop()
                value = "".join(target["parts"])  # type: ignore[arg-type]
                value = value.strip("\n")
                fence = "`" * max(3, _longest_backtick_run(value) + 1)
                self._append(f"\n\n{fence}\n{value}\n{fence}\n\n", raw=True)
            return

        if tag == "ac:structured-macro" and self.macros:
            target = self._pop()
            macro = self.macros.pop()
            body = "".join(target["parts"])  # type: ignore[arg-type]
            self._append(self._render_macro(macro, body), raw=True)
            return

        if tag == "ac:parameter" and self.macros:
            target = self._pop()
            name = str(self.macros[-1].pop("_param_name", ""))
            params = self.macros[-1]["params"]
            assert isinstance(params, dict)
            params[name] = "".join(target["parts"]).strip()  # type: ignore[arg-type]
            return

        if tag == "ac:link" and self.links:
            target = self._pop()
            link = self.links.pop()
            text = "".join(target["parts"]).strip() or str(link.get("title") or link.get("filename") or "")
            href = self._link_href(link)
            if text and href:
                self._append(f"[{text}]({href})", raw=True)
            elif text:
                self._append(str(text), raw=True)
            return

        if tag == "a" and self.links:
            target = self._pop()
            link = self.links.pop()
            text = "".join(target["parts"]).strip() or str(link.get("url") or "")
            href = self._link_href(link)
            if text and href:
                self._append(f"[{text}]({href})", raw=True)
            elif text:
                self._append(str(text), raw=True)
            return

        if tag == "ac:image" and self.images:
            target = self._pop()
            image = self.images.pop()
            alt = str(image.get("alt") or image.get("title") or "").strip()
            src = str(image.get("url") or "")
            if not src and image.get("filename"):
                src = f"attachment://{quote_plus(str(image['filename']))}"
            if src:
                self._append(f"![{_escape_alt(alt)}]({src})", raw=True)
            else:
                self._append("".join(target["parts"]), raw=True)  # type: ignore[arg-type]
            return

        if tag in {"th", "td"} and self.tables:
            target = self._pop()
            table = self.tables[-1]
            row = table.get("row")
            if isinstance(row, list):
                row.append("".join(target["parts"]).strip())  # type: ignore[arg-type]
            return

        if tag == "tr" and self.tables:
            table = self.tables[-1]
            row = table.get("row")
            rows = table.get("rows")
            if isinstance(row, list) and isinstance(rows, list):
                rows.append(row)
            table["row"] = None
            return

        if tag == "table" and self.tables:
            table = self.tables.pop()
            rows = table.get("rows")
            if isinstance(rows, list) and rows:
                self._append(_render_table(rows), raw=True)
                self._block()
            return

        if tag == "li":
            self._append("\n", raw=True)
            return
        if tag in {"ul", "ol"}:
            if self.lists:
                self.lists.pop()
            self._append("\n", raw=True)
            return

        if tag == "blockquote" and self.blockquotes:
            target = self._pop()
            value = "".join(target["parts"]).strip()  # type: ignore[arg-type]
            quoted = "\n".join(f"> {line}" if line else ">" for line in value.splitlines())
            self.blockquotes.pop()
            self._append(f"{quoted}\n\n", raw=True)
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._block()
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "main"}:
            self._block()
            return
        if tag in {"strong", "b"}:
            self._append("**", raw=True)
            return
        if tag in {"em", "i"}:
            self._append("*", raw=True)
            return
        if tag in {"s", "strike", "del"}:
            self._append("~~", raw=True)
            return
        if tag == "code":
            self._append("`", raw=True)
            return
        if tag == "sup":
            self._append("</sup>", raw=True)
            return
        if tag == "sub":
            self._append("</sub>", raw=True)
            return

        if tag == "ac:task-id" and self.tasks:
            self._pop()
            return
        if tag == "ac:task-status" and self.tasks:
            target = self._pop()
            self.tasks[-1]["status"] = "".join(target["parts"]).strip().lower()  # type: ignore[arg-type]
            return
        if tag == "ac:task" and self.tasks:
            self._pop()
            task = self.tasks.pop()
            status = str(task.get("status", "")).lower()
            checked = "x" if status in {"complete", "completed", "done", "true"} else " "
            body = "".join(task.get("body", [])).strip()  # type: ignore[arg-type]
            body = re.sub(r"\s*\n\s*", " ", body)
            self._append(f"- [{checked}] {body}\n", raw=True)
            return
        if tag == "ac:task-list":
            self._block()
            return

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data:
            return
        self._append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def unknown_decl(self, data: str) -> None:
        if data.startswith("CDATA["):
            self.handle_data(data[6:-2] if data.endswith("]]") else data[6:])

    def _link_href(self, link: dict[str, object]) -> str:
        if link.get("url"):
            return str(link["url"])
        if link.get("filename"):
            return f"attachment://{quote_plus(str(link['filename']))}"
        if link.get("title"):
            title = quote_plus(str(link["title"]))
            space = quote_plus(str(link.get("space") or ""))
            if self.base_url:
                if space:
                    return f"{self.base_url}/display/{space}/{title}"
                return f"{self.base_url}/pages/viewpage.action?pageTitle={title}"
            return f"confluence://{space}/{title}" if space else f"confluence://{title}"
        if link.get("user"):
            return f"mention://{link['user']}"
        if link.get("anchor"):
            return f"#{link['anchor']}"
        return ""

    def _render_macro(self, macro: dict[str, object], body: str) -> str:
        name = str(macro.get("name", ""))
        params = macro.get("params")
        assert isinstance(params, dict)
        title = str(params.get("title") or "").strip()
        body = body.strip("\n")

        if name in {"code", "noformat"}:
            language = str(params.get("language") or "").strip()
            fence = "`" * max(3, _longest_backtick_run(body) + 1)
            info = language if name == "code" else ""
            return f"\n\n{fence}{info}\n{body}\n{fence}\n\n"

        if name == "view-file":
            filename = str(params.get("name") or title or "file")
            return f"\n\n[Attachment: {filename}]\n\n"

        if name in _MACRO_LABELS:
            label = _MACRO_LABELS[name]
            header = f"> [!{label}]" + (f" {title}" if title else "")
            lines = body.splitlines() or [""]
            quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
            return f"\n\n{header}\n{quoted}\n\n"

        if name == "expand":
            summary = title or "Details"
            return f"\n\n<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n\n"

        if name == "toc":
            return "\n\n<!-- Table of contents -->\n\n"

        if name == "status":
            label = title or str(params.get("colour") or "").strip() or "status"
            return f" **[{label}]** "

        if name == "jira":
            key = str(params.get("key") or params.get("jqlQuery") or "").strip()
            return f" `{key}` " if key else body

        if name == "include":
            page = str(params.get("page") or params.get("pageTitle") or "").strip()
            return f"\n\n<!-- Included page: {page or 'unknown'} -->\n\n"

        if name == "anchor":
            anchor = str(params.get("") or title or "").strip()
            return f'<a id="{anchor}"></a>' if anchor else ""

        return f"\n\n{body}\n\n" if body else ""


def _escape_alt(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def _longest_backtick_run(value: str) -> int:
    runs = re.findall(r"`+", value)
    return max((len(run) for run in runs), default=0)


def _render_table(rows: list[list[str]]) -> str:
    cleaned: list[list[str]] = []
    for row in rows:
        cleaned.append(
            [
                re.sub(r"\s+", " ", cell.replace("\n", "<br>")).replace("|", "\\|").strip()
                for cell in row
            ]
        )
    width = max((len(row) for row in cleaned), default=0)
    if not width:
        return ""
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    header = cleaned[0]
    body = cleaned[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n\n" + "\n".join(lines) + "\n\n"


def _cleanup(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"(?m)^[ \t]+$", "", value)
    value = re.sub(r"([^\n])\n(#{1,6} )", r"\1\n\n\2", value)
    return value.strip() + "\n"


def storage_to_markdown(value: str, base_url: str = "") -> str:
    """Convert Confluence storage-format XHTML to Markdown."""

    return _StorageParser(base_url=base_url).convert(value)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def strip_html(value: str) -> str:
    """Return whitespace-normalized plain text from an HTML fragment."""

    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return parser.text()


_ATTACHMENT_IMAGE = re.compile(
    r'<img\s+src="attachment://([^"]+)"(?:\s+alt="([^"]*)")?\s*/?>',
    re.IGNORECASE,
)


def _escape_xml_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def markdown_to_storage(value: str) -> str:
    """Convert safe Markdown to a Confluence Storage Format fragment.

    Raw HTML is disabled for agent-supplied Markdown. Callers that need
    Confluence macros must explicitly use the storage body format.
    """

    if not value or not value.strip():
        return ""
    renderer = MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
            "breaks": False,
        },
    )
    renderer.enable(["table", "strikethrough"])
    rendered = renderer.render(value).strip()
    rendered = rendered.replace("<br>", "<br />").replace("<hr>", "<hr />")

    def attachment_image(match: re.Match[str]) -> str:
        filename = match.group(1)
        alt = match.group(2) or ""
        alt_attr = f' ac:alt="{_escape_xml_attr(alt)}"' if alt else ""
        return (
            f"<ac:image{alt_attr}>"
            f'<ri:attachment ri:filename="{_escape_xml_attr(filename)}" />'
            "</ac:image>"
        )

    return _ATTACHMENT_IMAGE.sub(attachment_image, rendered)


def body_to_storage(value: str, body_format: str = "markdown") -> str:
    if body_format == "storage":
        return value or ""
    if body_format == "markdown":
        return markdown_to_storage(value or "")
    raise ValueError("body_format must be markdown or storage")
