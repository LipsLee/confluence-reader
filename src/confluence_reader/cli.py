"""Command-line interface for Confluence Agent."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .errors import ConfluenceReaderError, ValidationError
from .markdown import storage_to_markdown
from .service import ConfluenceService
from .utils import (
    format_size,
    page_breadcrumb,
    page_markdown,
    safe_filename,
    unique_path,
    write_text,
)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _service(args: argparse.Namespace) -> ConfluenceService:
    return ConfluenceService(Config.from_cli_args(args))


def _read_body(args: argparse.Namespace) -> str:
    body = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    if body and body_file:
        raise ValidationError("Pass either --body or --body-file, not both")
    if body_file:
        path = Path(body_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"Cannot read body file {path}: {exc}") from exc
    if body is None:
        raise ValidationError("A body is required via --body or --body-file")
    return str(body)


def cmd_doctor(args: argparse.Namespace) -> int:
    result = _service(args).status(probe=True)
    if args.json:
        print_json(result)
    else:
        print(f"Confluence: {result['base_url']}")
        print(f"Auth: {result['auth']}")
        print(f"Type: {result.get('flavor_detected') or result.get('flavor')}")
        print(f"Reachable: {'yes' if result.get('reachable') else 'no'}")
        print(f"Writes enabled: {'yes' if result.get('writes_enabled') else 'no'}")
        user = result.get("user") or {}
        name = user.get("displayName") or user.get("username") or user.get("accountId")
        if name:
            print(f"User: {name}")
        for check in result.get("checks") or []:
            status = "OK" if check.get("ok") else "FAILED"
            suffix = f" - {check.get('error')}" if check.get("error") else ""
            print(f"  [{status}] {check.get('name')}{suffix}")
    return 0 if result.get("reachable") else 1


def cmd_status(args: argparse.Namespace) -> int:
    result = _service(args).status(probe=not args.no_probe)
    if args.json:
        print_json(result)
    else:
        print(
            f"{result.get('base_url')} | {result.get('flavor_detected')} | "
            f"writes={'yes' if result.get('writes_enabled') else 'no'}"
        )
        print(
            "Capabilities: same-space move, trash/restore where supported, version restore; "
            "no cross-space move, permanent purge, or archive."
        )
        print(f"State: {result.get('state_dir')}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    result = _service(args).resolve_page(args.ref, args.space)
    result["breadcrumb"] = page_breadcrumb(result)
    if args.json:
        print_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _format_search(result: dict[str, Any]) -> str:
    lines = [f"CQL: {result.get('cql')}", f"Found {result.get('count', 0)} result(s)."]
    for item in result.get("results") or []:
        space = (item.get("space") or {}).get("key") or "?"
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        lines.append(f"\n- [{title}]({url})" if url else f"\n- {title}")
        lines.append(
            f"  id={item.get('id')} | space={space} | updated={item.get('updated') or '-'}"
        )
        if item.get("excerpt"):
            lines.append(f"  {str(item['excerpt'])[:500]}")
    if result.get("next_cursor"):
        lines.append(f"\nNext cursor: {result['next_cursor']}")
    return "\n".join(lines)


def cmd_search(args: argparse.Namespace) -> int:
    service = _service(args)
    spaces = [item.strip() for item in (args.space or "").split(",") if item.strip()]
    result = service.search_pages(
        query=args.query,
        cql=args.cql,
        space_keys=spaces,
        labels=args.label or [],
        last_modified=args.last_modified,
        limit=args.limit,
        cursor=args.cursor,
    )
    if args.json:
        print_json(result)
    else:
        print(_format_search(result))
    return 0


def _page_output(page: dict[str, Any], body_format: str, base_url: str) -> str:
    if body_format == "storage":
        return str(page.get("body_storage") or "")
    if body_format == "both":
        return (
            "# Markdown\n\n"
            + storage_to_markdown(str(page.get("body_storage") or ""), base_url)
            + "\n# Storage format\n\n```xml\n"
            + str(page.get("body_storage") or "")
            + "\n```\n"
        )
    return page_markdown(page, base_url)


def cmd_page(args: argparse.Namespace) -> int:
    service = _service(args)
    page = service.get_page(
        args.ref,
        space_key=args.space,
        body_format="both",
        include_comments=args.comments,
        include_attachments=args.attachments,
    )
    if args.output:
        output_path = service._validate_local_path(args.output)
        value = _page_output(page, args.body_format, service.client.base_url)
        if args.comments and page.get("comments"):
            value += "\n## Comments\n\n" + json.dumps(
                page["comments"], ensure_ascii=False, indent=2
            )
        if args.attachments and page.get("attachments"):
            value += "\n\n## Attachments\n\n" + json.dumps(
                page["attachments"], ensure_ascii=False, indent=2
            )
        write_text(output_path, value)
        if not args.json:
            print(str(output_path))
    payload = dict(page)
    if args.body_format == "markdown":
        payload.pop("body_storage", None)
    elif args.body_format == "storage":
        payload.pop("body_markdown", None)
    if args.output:
        payload["output"] = str(Path(args.output).expanduser().resolve())
    if args.json:
        print_json(payload)
    elif not args.output:
        print(_page_output(page, args.body_format, service.client.base_url).rstrip())
    return 0


def cmd_children(args: argparse.Namespace) -> int:
    result = _service(args).list_children(
        args.ref,
        space_key=args.space,
        recursive=args.recursive,
        limit=args.limit,
        cursor=args.cursor,
    )
    if args.json:
        print_json(result)
    else:
        for item in result.get("children") or []:
            depth = len(item.get("ancestors") or [])
            print(f"{'  ' * max(0, depth - 1)}- {item.get('title')} (id={item.get('id')})")
        if result.get("next_cursor"):
            print(f"Next cursor: {result['next_cursor']}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    service = _service(args)
    root = service.resolve_page(args.ref, args.space)
    pages = service.client.get_children(
        root["id"],
        recursive=True,
        max_items=max(1, args.max_items),
    )
    nodes = [(0, root)] + [
        (max(1, len(item.get("ancestors") or []) - len(root.get("ancestors") or [])), item)
        for item in pages
    ]
    nodes = [item for item in nodes if item[0] <= args.max_depth]
    if args.json:
        print_json(
            {
                "ok": True,
                "root": {"id": root["id"], "title": root["title"]},
                "count": len(nodes),
                "tree": [
                    {
                        "depth": depth,
                        "id": page["id"],
                        "title": page["title"],
                        "url": page.get("url"),
                    }
                    for depth, page in nodes
                ],
            }
        )
    else:
        for depth, page in nodes:
            prefix = "  " * depth + ("- " if depth else "")
            print(f"{prefix}{page.get('title')} (id={page.get('id')})")
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    result = _service(args).list_page_versions(
        args.ref,
        space_key=args.space,
        limit=args.limit,
        cursor=args.cursor,
    )
    if args.json:
        print_json(result)
    else:
        for version in result.get("versions") or []:
            print(
                f"- v{version.get('number')} | {version.get('created') or '-'} | "
                f"{version.get('author') or 'unknown'} | {version.get('message') or ''}"
            )
        if result.get("next_cursor"):
            print(f"Next cursor: {result['next_cursor']}")
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    result = _service(args).get_comments(
        args.ref,
        space_key=args.space,
        limit=args.limit,
        cursor=args.cursor,
    )
    if args.json:
        print_json(result)
    else:
        for item in result.get("comments") or []:
            print(f"\n## {item.get('author') or 'Unknown'} - {item.get('created') or ''}")
            print(str(item.get("body_markdown") or "").strip())
        if result.get("next_cursor"):
            print(f"\nNext cursor: {result['next_cursor']}")
    return 0


def cmd_attachments(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.list_attachments(
        args.ref,
        space_key=args.space,
        limit=args.limit,
        cursor=args.cursor,
    )
    downloaded: list[dict[str, Any]] = []
    if args.download_dir:
        page = service.resolve_page(args.ref, args.space)
        directory = service._validate_local_path(args.download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        max_bytes = int(args.max_size_mb * 1024 * 1024)
        for attachment in result.get("attachments") or []:
            destination = unique_path(
                directory / page["id"],
                safe_filename(str(attachment.get("filename") or attachment.get("id"))),
            )
            written = service.client.download_attachment(
                attachment,
                destination,
                max_bytes=max_bytes,
            )
            downloaded.append(
                {
                    "id": attachment.get("id"),
                    "filename": attachment.get("filename"),
                    "path": str(destination.resolve()),
                    "bytes": written,
                }
            )
    result["downloaded"] = downloaded
    if args.json:
        print_json(result)
    else:
        for item in result.get("attachments") or []:
            print(
                f"- {item.get('filename')} | {item.get('media_type') or 'unknown'} | "
                f"{format_size(int(item.get('size') or 0))} | id={item.get('id')}"
            )
        for item in downloaded:
            print(f"Downloaded: {item['path']} ({format_size(item['bytes'])})")
        if result.get("next_cursor"):
            print(f"Next cursor: {result['next_cursor']}")
    return 0


def _print_mutation(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print_json(result)
    else:
        print(
            f"{result.get('action')}: {result.get('title') or result.get('page_id')} "
            f"(v{result.get('version_before')} -> v{result.get('version_after')})"
        )
        if result.get("url"):
            print(result["url"])
        if result.get("audit_id"):
            print(f"Audit: {result['audit_id']}")


def cmd_create(args: argparse.Namespace) -> int:
    result = _service(args).create_page(
        space_key=args.space,
        title=args.title,
        body=_read_body(args),
        body_format=args.body_format,
        parent_id=args.parent_id,
        version_message=args.version_message,
    )
    _print_mutation(result, args.json)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    body = _read_body(args) if args.body is not None or args.body_file else None
    result = _service(args).update_page(
        page_id=args.page,
        expected_version=args.expected_version,
        title=args.title,
        body=body,
        body_format=args.body_format,
        version_message=args.version_message,
    )
    _print_mutation(result, args.json)
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    result = _service(args).append_page(
        page_id=args.page,
        expected_version=args.expected_version,
        body=_read_body(args),
        body_format=args.body_format,
        version_message=args.version_message,
    )
    _print_mutation(result, args.json)
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    result = _service(args).add_comment(
        page_id=args.page,
        body=_read_body(args),
        body_format=args.body_format,
    )
    if args.json:
        print_json(result)
    else:
        print(f"Comment added: id={result.get('comment_id')} {result.get('url') or ''}")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    result = _service(args).add_labels(page_id=args.page, labels=args.label)
    if args.json:
        print_json(result)
    else:
        print("Labels: " + ", ".join(result.get("labels") or []))
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    result = _service(args).upload_attachment(
        page_id=args.page,
        file_path=args.file,
        replace_existing=args.replace,
        expected_attachment_version=args.expected_attachment_version,
        comment=args.comment or "",
    )
    if args.json:
        print_json(result)
    else:
        attachment = result.get("attachment") or {}
        print(
            f"{result.get('action')}: {attachment.get('filename')} "
            f"(v{attachment.get('version')})"
        )
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    result = _service(args).download_attachment(
        ref=args.page,
        destination_path=args.output,
        attachment_id=args.attachment_id,
        filename=args.filename,
        overwrite=args.overwrite,
        space_key=args.space,
        max_bytes=int(args.max_size_mb * 1024 * 1024),
    )
    if args.json:
        print_json(result)
    else:
        print(f"{result['path']} ({format_size(result['bytes'])})")
    return 0


def _destructive(
    result: dict[str, Any],
    *,
    as_json: bool,
) -> None:
    if as_json:
        print_json(result)
    elif result.get("confirmation_required"):
        print("Preview only. No changes were made.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_mutation(result, False)


def cmd_move(args: argparse.Namespace) -> int:
    result = _service(args).move_page(
        page_id=args.page,
        new_parent_id=args.parent_id,
        confirmation_token=args.confirm_token,
    )
    _destructive(result, as_json=args.json)
    return 0


def cmd_trash(args: argparse.Namespace) -> int:
    result = _service(args).trash_page(
        page_id=args.page,
        confirmation_token=args.confirm_token,
    )
    _destructive(result, as_json=args.json)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    result = _service(args).restore_page(
        page_id=args.page,
        confirmation_token=args.confirm_token,
    )
    _destructive(result, as_json=args.json)
    return 0


def cmd_restore_version(args: argparse.Namespace) -> int:
    result = _service(args).restore_page_version(
        page_id=args.page,
        version_number=args.version,
        confirmation_token=args.confirm_token,
    )
    _destructive(result, as_json=args.json)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    result = _service(args).export_page_tree(
        ref=args.ref,
        output_dir=args.output_dir,
        space_key=args.space,
        descendants=args.descendants,
        max_pages=args.max_pages,
        include_attachments=args.attachments,
        max_attachment_mb=args.max_attachment_mb,
    )
    if args.json:
        print_json(result)
    else:
        print(f"Exported {result['page_count']} page(s) to {result['output_dir']}")
        print(f"Manifest: {result['manifest_path']}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    result = _service(args).tail_audit(limit=args.limit)
    if args.json:
        print_json(result)
    else:
        print(f"Audit file: {result['audit_path']}")
        for item in result["events"]:
            print(
                f"- {item.get('timestamp')} {item.get('operation')} "
                f"page={item.get('page_id')} {item.get('outcome')}"
            )
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", help="Confluence base URL")
    parser.add_argument("--config", help="Path to a JSON configuration file")
    parser.add_argument("--auth", choices=("auto", "basic", "bearer", "none"))
    parser.add_argument(
        "--flavor",
        choices=("auto", "cloud", "data_center"),
        help="Confluence platform; auto detects common Cloud hosts",
    )
    parser.add_argument("--timeout", type=float, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--verbose", action="store_true", help="Show traceback on failure")


def _body_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--body", help="Body text")
    group.add_argument("--body-file", help="Read body text from a UTF-8 file")
    parser.add_argument(
        "--body-format",
        choices=("markdown", "storage"),
        default="markdown",
        help="Body representation (default: markdown)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="confluence",
        description="Read and write Confluence Cloud and Data Center.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    add_common(common)

    doctor = subparsers.add_parser(
        "doctor", parents=[common], help="Check configuration and authentication"
    )
    doctor.set_defaults(func=cmd_doctor)

    status = subparsers.add_parser(
        "status", parents=[common], help="Show site capabilities without probing by default"
    )
    status.add_argument("--probe", dest="no_probe", action="store_false")
    status.set_defaults(func=cmd_status, no_probe=True)

    resolve = subparsers.add_parser(
        "resolve", parents=[common], help="Resolve a URL, id, or title"
    )
    resolve.add_argument("ref")
    resolve.add_argument("--space")
    resolve.set_defaults(func=cmd_resolve)

    search = subparsers.add_parser("search", parents=[common], help="Search pages with CQL")
    search.add_argument("query", nargs="?")
    search.add_argument("--cql")
    search.add_argument("--space", help="Comma-separated space keys")
    search.add_argument("--label", action="append")
    search.add_argument("--last-modified", help="ISO date or Confluence date expression")
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--cursor")
    search.set_defaults(func=cmd_search)

    page = subparsers.add_parser("page", parents=[common], help="Read one page")
    page.add_argument("ref")
    page.add_argument("--space")
    page.add_argument(
        "--body-format", choices=("markdown", "storage", "both"), default="markdown"
    )
    page.add_argument("--comments", action="store_true")
    page.add_argument("--attachments", action="store_true")
    page.add_argument("--output")
    page.set_defaults(func=cmd_page)

    children = subparsers.add_parser("children", parents=[common], help="List child pages")
    children.add_argument("ref")
    children.add_argument("--space")
    children.add_argument("--recursive", action="store_true")
    children.add_argument("--limit", type=int, default=25)
    children.add_argument(
        "--max-items",
        dest="limit",
        type=int,
        help="Backward-compatible alias for --limit",
    )
    children.add_argument("--cursor")
    children.set_defaults(func=cmd_children)

    tree = subparsers.add_parser("tree", parents=[common], help="Show a page hierarchy")
    tree.add_argument("ref")
    tree.add_argument("--space")
    tree.add_argument("--max-depth", type=int, default=10)
    tree.add_argument("--max-items", type=int, default=500)
    tree.set_defaults(func=cmd_tree)

    versions = subparsers.add_parser("versions", parents=[common], help="List page versions")
    versions.add_argument("ref")
    versions.add_argument("--space")
    versions.add_argument("--limit", type=int, default=25)
    versions.add_argument("--cursor")
    versions.set_defaults(func=cmd_versions)

    comments = subparsers.add_parser("comments", parents=[common], help="Read page comments")
    comments.add_argument("ref")
    comments.add_argument("--space")
    comments.add_argument("--limit", type=int, default=25)
    comments.add_argument(
        "--max-items",
        dest="limit",
        type=int,
        help="Backward-compatible alias for --limit",
    )
    comments.add_argument("--cursor")
    comments.set_defaults(func=cmd_comments)

    attachments = subparsers.add_parser(
        "attachments", parents=[common], help="List or download attachments"
    )
    attachments.add_argument("ref")
    attachments.add_argument("--space")
    attachments.add_argument("--limit", type=int, default=25)
    attachments.add_argument(
        "--max-items",
        dest="limit",
        type=int,
        help="Backward-compatible alias for --limit",
    )
    attachments.add_argument("--cursor")
    attachments.add_argument("--download-dir")
    attachments.add_argument(
        "--max-size-mb",
        type=float,
        default=50,
        help="Maximum size per downloaded attachment in MB (default: 50)",
    )
    attachments.set_defaults(func=cmd_attachments)

    export = subparsers.add_parser(
        "export", parents=[common], help="Export a page tree to Markdown"
    )
    export.add_argument("ref")
    export.add_argument("--space")
    export.add_argument("--output-dir", required=True)
    export.add_argument("--descendants", action="store_true")
    export.add_argument("--max-pages", type=int, default=100)
    export.add_argument("--attachments", action="store_true")
    export.add_argument(
        "--max-attachment-mb",
        type=float,
        default=50,
        help="Maximum size per downloaded attachment in MB (default: 50)",
    )
    export.set_defaults(func=cmd_export)

    create = subparsers.add_parser("create", parents=[common], help="Create a page")
    create.add_argument("--space", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--parent-id")
    create.add_argument("--version-message", default="Created by Confluence Agent")
    _body_options(create, required=True)
    create.set_defaults(func=cmd_create)

    update = subparsers.add_parser("update", parents=[common], help="Update a page")
    update.add_argument("page")
    update.add_argument("--expected-version", type=int, required=True)
    update.add_argument("--title")
    update.add_argument("--version-message", default="Updated by Confluence Agent")
    _body_options(update, required=False)
    update.set_defaults(func=cmd_update)

    append = subparsers.add_parser("append", parents=[common], help="Append to a page")
    append.add_argument("page")
    append.add_argument("--expected-version", type=int, required=True)
    append.add_argument("--version-message", default="Appended by Confluence Agent")
    _body_options(append, required=True)
    append.set_defaults(func=cmd_append)

    comment = subparsers.add_parser("comment", parents=[common], help="Add a page comment")
    comment.add_argument("page")
    _body_options(comment, required=True)
    comment.set_defaults(func=cmd_comment)

    label = subparsers.add_parser("label", parents=[common], help="Add page labels")
    label.add_argument("page")
    label.add_argument("--label", action="append", required=True)
    label.set_defaults(func=cmd_label)

    upload = subparsers.add_parser(
        "upload", parents=[common], help="Upload or replace an attachment"
    )
    upload.add_argument("page")
    upload.add_argument("--file", required=True)
    upload.add_argument("--replace", action="store_true")
    upload.add_argument("--expected-attachment-version", type=int)
    upload.add_argument("--comment")
    upload.set_defaults(func=cmd_upload)

    download = subparsers.add_parser(
        "download", parents=[common], help="Download one attachment"
    )
    download.add_argument("page")
    download.add_argument("--space")
    download.add_argument("--attachment-id")
    download.add_argument("--filename")
    download.add_argument("--output", required=True)
    download.add_argument("--overwrite", action="store_true")
    download.add_argument(
        "--max-size-mb",
        type=float,
        default=50,
        help="Maximum attachment size in MB (default: 50)",
    )
    download.set_defaults(func=cmd_download)

    move = subparsers.add_parser("move", parents=[common], help="Preview or confirm a move")
    move.add_argument("page")
    move.add_argument("--parent-id", required=True)
    move.add_argument("--confirm-token")
    move.set_defaults(func=cmd_move)

    trash = subparsers.add_parser(
        "trash", parents=[common], help="Preview or confirm moving a page to trash"
    )
    trash.add_argument("page")
    trash.add_argument("--confirm-token")
    trash.set_defaults(func=cmd_trash)

    restore = subparsers.add_parser(
        "restore", parents=[common], help="Preview or confirm restoring a trashed page"
    )
    restore.add_argument("page")
    restore.add_argument("--confirm-token")
    restore.set_defaults(func=cmd_restore)

    restore_version = subparsers.add_parser(
        "restore-version",
        parents=[common],
        help="Preview or confirm restoring a historical version",
    )
    restore_version.add_argument("page")
    restore_version.add_argument("--version", type=int, required=True)
    restore_version.add_argument("--confirm-token")
    restore_version.set_defaults(func=cmd_restore_version)

    audit = subparsers.add_parser("audit", parents=[common], help="Show recent audit events")
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfluenceReaderError as exc:
        if getattr(args, "json", False):
            print_json(exc.to_dict())
        else:
            print(f"Error ({exc.code}): {exc}", file=sys.stderr)
            if exc.details:
                print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
