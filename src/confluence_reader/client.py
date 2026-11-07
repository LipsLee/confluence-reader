"""Confluence Cloud and Data Center client."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from .config import Config
from .errors import (
    ApiError,
    ConflictError,
    ConfluenceReaderError,
    NotFoundError,
    UnsupportedOperationError,
    ValidationError,
    VersionConflictError,
)
from .transport import ConfluenceTransport, multipart_body, origin_of
from .utils import (
    PAGE_EXPAND,
    cql_escape,
    decode_cursor,
    encode_cursor,
    excerpt_from_search,
    parse_page_ref,
)


class ConfluenceClient:
    """Low-level client that normalizes Cloud and Data Center differences."""

    def __init__(
        self,
        config: Config,
        *,
        transport: ConfluenceTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or ConfluenceTransport(config)
        self.base_url = config.base_url.rstrip("/")
        self.origin = origin_of(self.base_url)
        self._trash_support: dict[str, bool] = {}
        self._space_cache: dict[str, dict[str, Any]] = {}

    @property
    def flavor(self) -> str:
        if self.config.flavor in {"cloud", "data_center"}:
            return self.config.flavor
        host = (urlparse(self.base_url).hostname or "").lower()
        if host.endswith(".atlassian.net"):
            return "cloud"
        return "data_center"

    @property
    def is_cloud(self) -> bool:
        return self.flavor == "cloud"

    def api_url(self, path_or_url: str) -> str:
        return self.transport.api_url(path_or_url)

    def web_url(self, link: str | None) -> str:
        if not link:
            return ""
        if link.startswith(("http://", "https://")):
            return link
        base_path = urlparse(self.base_url).path.rstrip("/")
        if link.startswith("/"):
            if base_path and link.startswith(f"{base_path}/"):
                return f"{self.origin}{link}"
            if link.startswith("/wiki/") and base_path != "/wiki":
                return f"{self.origin}{link}"
            if base_path and link.startswith(
                ("/api/", "/rest/", "/spaces/", "/pages/", "/download/")
            ):
                return f"{self.base_url}{link}"
            return f"{self.origin}{link}"
        return urljoin(self.base_url + "/", link)

    def _resolve_next(self, next_link: str, current_url: str) -> str:
        if next_link.startswith(("http://", "https://")):
            target = next_link
        elif next_link.startswith("/"):
            base_path = urlparse(self.base_url).path.rstrip("/")
            if base_path and next_link.startswith(
                ("/api/", "/rest/", "/download/", "/spaces/", "/pages/")
            ):
                target = f"{self.base_url}{next_link}"
            else:
                target = f"{self.origin}{next_link}"
        else:
            target = urljoin(current_url, next_link)
        if origin_of(target) != self.origin:
            raise ValidationError("Refusing to follow a pagination link outside the configured site")
        return target

    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        return self.transport.request("GET", path_or_url, params=params).json()

    def post_json(self, path_or_url: str, payload: Any) -> Any:
        return self.transport.request("POST", path_or_url, json_body=payload).json()

    def put_json(self, path_or_url: str, payload: Any) -> Any:
        return self.transport.request("PUT", path_or_url, json_body=payload).json()

    def delete(self, path_or_url: str) -> Any:
        response = self.transport.request("DELETE", path_or_url)
        if not response.data:
            return {}
        return response.json()

    def _collection_page(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        limit = max(1, min(100, int(limit)))
        if cursor:
            url = decode_cursor(cursor)
            if origin_of(url) != self.origin:
                raise ValidationError("Pagination cursor points outside the configured site")
        else:
            query = dict(params or {})
            query["limit"] = limit
            url = self.transport.with_query(self.api_url(path), query)
        response = self.transport.request("GET", url)
        data = response.json()
        if not isinstance(data, dict):
            raise ApiError("Unexpected collection response from Confluence")
        results = data.get("results") or []
        if not isinstance(results, list):
            results = []
        clean_results = [item for item in results if isinstance(item, dict)]
        next_link = (data.get("_links") or {}).get("next")
        next_cursor = None
        if next_link:
            next_cursor = encode_cursor(self._resolve_next(str(next_link), response.url))
        return clean_results, next_cursor, response.url

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            items, cursor, _ = self._collection_page(
                path,
                params=params,
                cursor=cursor,
                limit=min(100, max(1, limit)),
            )
            for item in items:
                result.append(item)
                if max_items is not None and len(result) >= max_items:
                    return result
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return result

    def doctor(self) -> dict[str, Any]:
        result = self.config.public_summary()
        checks: list[dict[str, Any]] = []
        user: dict[str, Any] = {}
        try:
            user = self.get_json("/rest/api/user/current")
            checks.append({"name": "current_user", "ok": True})
        except Exception as exc:
            checks.append({"name": "current_user", "ok": False, "error": str(exc)})
            result.update(
                {
                    "reachable": False,
                    "flavor_detected": self.flavor,
                    "checks": checks,
                    "capabilities": self.capabilities(),
                }
            )
            return result
        try:
            spaces = self.paginate("/rest/api/space", limit=1, max_items=1)
            checks.append(
                {
                    "name": "space_list",
                    "ok": True,
                    "sample_space_key": spaces[0].get("key") if spaces else None,
                }
            )
        except Exception as exc:
            checks.append({"name": "space_list", "ok": False, "error": str(exc)})
        result.update(
            {
                "reachable": True,
                "flavor_detected": self.flavor,
                "api": "v2+v1" if self.is_cloud else "v1",
                "user": {
                    "displayName": user.get("displayName"),
                    "username": user.get("username"),
                    "accountId": user.get("accountId"),
                },
                "checks": checks,
                "capabilities": self.capabilities(),
            }
        )
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "writes_enabled": self.config.writes_allowed,
            "body_formats": ["markdown", "storage"],
            "same_space_move": True,
            "cross_space_move": False,
            "permanent_purge": False,
            "archive": False,
            "trash": True if self.is_cloud else "capability_checked_per_space",
            "restore_page": True if self.is_cloud else "capability_checked_per_space",
            "restore_version": True,
        }

    def get_page_by_id(self, page_id: str, *, status: str = "current") -> dict[str, Any]:
        page_id = str(page_id).strip()
        if not page_id.isdigit():
            raise ValidationError(f"Invalid Confluence page id: {page_id}")
        if self.is_cloud:
            params: dict[str, Any] = {
                "body-format": "storage",
                "include-labels": "true",
            }
            if status != "current":
                params["status"] = status
            try:
                data = self.get_json(f"/api/v2/pages/{quote(page_id)}", params)
                if isinstance(data, dict) and data.get("id"):
                    return self.normalize_page(data)
            except NotFoundError:
                pass
            except ApiError as exc:
                if exc.status not in {400, 404, 405}:
                    raise
        data = self.get_json(
            f"/rest/api/content/{quote(page_id)}",
            {"expand": PAGE_EXPAND, "status": status},
        )
        return self.normalize_page(data)

    def _raw_v1_page(self, page_id: str, *, status: str = "current") -> dict[str, Any]:
        data = self.get_json(
            f"/rest/api/content/{quote(str(page_id))}",
            {"expand": PAGE_EXPAND, "status": status},
        )
        if not isinstance(data, dict):
            raise ApiError("Unexpected page response from Confluence")
        return data

    def get_page_by_title(self, space: str, title: str) -> dict[str, Any]:
        data = self.get_json(
            "/rest/api/content",
            {
                "spaceKey": space,
                "title": title,
                "type": "page",
                "limit": 1,
                "expand": PAGE_EXPAND,
            },
        )
        results = data.get("results") or []
        if not results:
            raise NotFoundError(f"Page not found in space {space}: {title}", status=404)
        return self.normalize_page(results[0])

    def follow_redirects(self, url: str) -> str:
        if origin_of(url) != self.origin:
            raise ValidationError(
                "Refusing to open a URL outside the configured Confluence site"
            )
        response = self.transport.request(
            "GET",
            url,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        )
        return response.url

    def resolve_page(self, ref: str, space: str | None = None) -> dict[str, Any]:
        ref = (ref or "").strip()
        if not ref:
            raise ValidationError("Page reference is empty")
        page_id, space_from_url, title_from_url = parse_page_ref(ref)
        if page_id:
            return self.get_page_by_id(page_id)
        if space_from_url:
            space = space_from_url
        if title_from_url:
            title = title_from_url
        elif ref.startswith(("http://", "https://")):
            final_url = self.follow_redirects(ref)
            final_id, final_space, final_title = parse_page_ref(final_url)
            if final_id:
                return self.get_page_by_id(final_id)
            if final_space:
                space = space or final_space
            if final_title:
                title = final_title
            else:
                raise ValidationError(
                    "Could not extract a page id from this Confluence URL. "
                    "Open the canonical page and copy its /pages/<id>/ URL."
                )
        elif "/" in ref:
            space_candidate, title = ref.split("/", 1)
            space = space or space_candidate.strip()
            title = title.strip()
        else:
            title = ref
        if space:
            return self.get_page_by_title(space, title)
        results = self.search(cql=f'title = "{cql_escape(title)}" and type = page', max_items=20)
        if not results:
            raise NotFoundError(f"Page not found by title: {title}", status=404)
        if len(results) > 1:
            candidates = ", ".join(
                f"{item.get('title')} ({item.get('id')}, "
                f"space {(item.get('space') or {}).get('key', '?')})"
                for item in results[:8]
            )
            raise ConflictError(
                f"Multiple pages match '{title}'. Pass space. Candidates: {candidates}"
            )
        return self.get_page_by_id(str(results[0]["id"]))

    def search_page(
        self,
        *,
        cql: str,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        items, next_cursor, _ = self._collection_page(
            "/rest/api/search",
            params={"cql": cql, "expand": "content.space,content.version"},
            cursor=cursor,
            limit=limit,
        )
        return {
            "cql": cql,
            "count": len(items),
            "results": [self.normalize_search_result(item) for item in items],
            "next_cursor": next_cursor,
        }

    def search(self, *, cql: str, max_items: int = 25) -> list[dict[str, Any]]:
        items = self.paginate(
            "/rest/api/search",
            {"cql": cql, "expand": "content.space,content.version"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        return [self.normalize_search_result(item) for item in items]

    def get_children_page(
        self,
        page_id: str,
        *,
        recursive: bool = False,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        suffix = "descendant/page" if recursive else "child/page"
        items, next_cursor, _ = self._collection_page(
            f"/rest/api/content/{quote(str(page_id))}/{suffix}",
            params={"expand": "space,version,ancestors"},
            cursor=cursor,
            limit=limit,
        )
        children = [self.normalize_summary(item) for item in items]
        if recursive:
            children.sort(
                key=lambda item: (
                    len(item.get("ancestors") or []),
                    str(item.get("title") or "").casefold(),
                )
            )
        return {
            "count": len(children),
            "recursive": recursive,
            "children": children,
            "next_cursor": next_cursor,
        }

    def get_children(
        self,
        page_id: str,
        *,
        recursive: bool = False,
        max_items: int = 500,
    ) -> list[dict[str, Any]]:
        suffix = "descendant/page" if recursive else "child/page"
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/{suffix}",
            {"expand": "space,version,ancestors"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        children = [self.normalize_summary(item) for item in items]
        if recursive:
            children.sort(
                key=lambda item: (
                    len(item.get("ancestors") or []),
                    str(item.get("title") or "").casefold(),
                )
            )
        return children

    def get_comments_page(
        self,
        page_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        items, next_cursor, _ = self._collection_page(
            f"/rest/api/content/{quote(str(page_id))}/child/comment",
            params={"expand": "body.storage,version,history"},
            cursor=cursor,
            limit=limit,
        )
        comments = [self.normalize_comment(item) for item in items]
        return {"count": len(comments), "comments": comments, "next_cursor": next_cursor}

    def get_comments(self, page_id: str, *, max_items: int = 500) -> list[dict[str, Any]]:
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/child/comment",
            {"expand": "body.storage,version,history"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        return [self.normalize_comment(item) for item in items]

    def get_attachments_page(
        self,
        page_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        items, next_cursor, _ = self._collection_page(
            f"/rest/api/content/{quote(str(page_id))}/child/attachment",
            params={"expand": "version"},
            cursor=cursor,
            limit=limit,
        )
        attachments = [self.normalize_attachment(item) for item in items]
        return {
            "count": len(attachments),
            "attachments": attachments,
            "next_cursor": next_cursor,
        }

    def get_attachments(self, page_id: str, *, max_items: int = 500) -> list[dict[str, Any]]:
        items = self.paginate(
            f"/rest/api/content/{quote(str(page_id))}/child/attachment",
            {"expand": "version"},
            limit=min(100, max(1, max_items)),
            max_items=max_items,
        )
        return [self.normalize_attachment(item) for item in items]

    def list_versions_page(
        self,
        page_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        path = (
            f"/api/v2/pages/{quote(str(page_id))}/versions"
            if self.is_cloud
            else f"/rest/api/content/{quote(str(page_id))}/version"
        )
        items, next_cursor, _ = self._collection_page(
            path,
            cursor=cursor,
            limit=limit,
        )
        versions = [self.normalize_version(item) for item in items]
        return {"count": len(versions), "versions": versions, "next_cursor": next_cursor}

    def get_page_version(self, page_id: str, version_number: int) -> dict[str, Any]:
        if version_number < 1:
            raise ValidationError("Version number must be positive")
        if self.is_cloud:
            try:
                data = self.get_json(
                    f"/api/v2/pages/{quote(str(page_id))}",
                    {
                        "body-format": "storage",
                        "status": "historical",
                        "version": version_number,
                    },
                )
                if isinstance(data, dict):
                    returned_version = int((data.get("version") or {}).get("number") or 0)
                    if data.get("id") and returned_version == version_number:
                        return self.normalize_page(data)
            except NotFoundError:
                pass
            except ApiError as exc:
                if exc.status not in {400, 404, 405}:
                    raise
        try:
            data = self.get_json(
                f"/rest/api/content/{quote(str(page_id))}",
                {
                    "status": "historical",
                    "version": version_number,
                    "expand": PAGE_EXPAND,
                },
            )
            if isinstance(data, dict) and data.get("id"):
                return self.normalize_page(data)
        except NotFoundError:
            pass
        try:
            data = self.get_json(
                f"/rest/api/content/{quote(str(page_id))}/version/{version_number}",
                {"expand": "content.body.storage,content.space,content.ancestors"},
            )
        except NotFoundError as exc:
            raise UnsupportedOperationError(
                "This Confluence site could not retrieve the requested historical version",
                details={"page_id": str(page_id), "version": version_number},
            ) from exc
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, dict):
            merged = dict(content)
            merged.setdefault("version", data)
            return self.normalize_page(merged)
        raise UnsupportedOperationError(
            "The historical version response did not include page content",
            details={"page_id": str(page_id), "version": version_number},
        )

    def get_space(self, space_key: str) -> dict[str, Any]:
        data = self.get_json(f"/rest/api/space/{quote(str(space_key))}")
        if not isinstance(data, dict):
            raise ApiError("Unexpected space response from Confluence")
        return data

    def get_space_by_id(self, space_id: str) -> dict[str, Any]:
        space_id = str(space_id)
        if space_id in self._space_cache:
            return dict(self._space_cache[space_id])
        data = self.get_json(f"/api/v2/spaces/{quote(space_id)}")
        if not isinstance(data, dict):
            raise ApiError("Unexpected space response from Confluence")
        self._space_cache[space_id] = dict(data)
        return data

    def get_space_id(self, space_key: str) -> str:
        data = self.get_json("/api/v2/spaces", {"keys": space_key, "limit": 1})
        results = data.get("results") or []
        if results:
            return str(results[0].get("id", ""))
        legacy = self.get_space(space_key)
        if legacy.get("id"):
            return str(legacy["id"])
        raise NotFoundError(f"Confluence space not found: {space_key}")

    def _same_space_parent(self, page: dict[str, Any], parent_id: str) -> dict[str, Any]:
        parent = self.get_page_by_id(parent_id)
        if str((page.get("space") or {}).get("key")) != str((parent.get("space") or {}).get("key")):
            raise ValidationError("Cross-space page moves are not supported")
        if parent_id == str(page.get("id")):
            raise ValidationError("A page cannot be moved under itself")
        if any(str(item.get("id")) == str(page.get("id")) for item in parent.get("ancestors") or []):
            raise ValidationError("A page cannot be moved under one of its descendants")
        return parent

    def create_page(
        self,
        *,
        space_key: str,
        title: str,
        body_storage: str,
        parent_id: str | None = None,
        version_message: str = "",
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValidationError("Page title is required")
        if not space_key.strip():
            raise ValidationError("Space key is required")
        if parent_id:
            parent = self.get_page_by_id(parent_id)
            if str((parent.get("space") or {}).get("key")) != space_key:
                raise ValidationError("The parent page must be in the requested space")
        if self.is_cloud:
            payload: dict[str, Any] = {
                "spaceId": self.get_space_id(space_key),
                "status": "current",
                "title": title,
                "body": {"representation": "storage", "value": body_storage},
            }
            if parent_id:
                payload["parentId"] = parent_id
            data = self.post_json("/api/v2/pages", payload)
        else:
            payload = {
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "body": {
                    "storage": {
                        "value": body_storage,
                        "representation": "storage",
                    }
                },
            }
            if parent_id:
                payload["ancestors"] = [{"id": parent_id}]
            data = self.post_json("/rest/api/content", payload)
        page_id = str((data or {}).get("id") or "")
        if not page_id:
            results = self.search(
                cql=f'space = "{cql_escape(space_key)}" and title = "{cql_escape(title)}"',
                max_items=5,
            )
            if len(results) == 1:
                page_id = str(results[0]["id"])
        if not page_id:
            raise ApiError("Confluence created the page but did not return its id")
        return self.get_page_by_id(page_id)

    def _update_v2(
        self,
        *,
        page_id: str,
        current: dict[str, Any],
        title: str,
        body_storage: str,
        version_message: str,
        parent_id: str | None,
        status: str,
        restore_only: bool = False,
    ) -> dict[str, Any]:
        current_version = int((current.get("version") or {}).get("number") or 0)
        payload: dict[str, Any] = {
            "id": str(page_id),
            "status": status,
            "title": title,
            "body": {"representation": "storage", "value": body_storage},
            "version": {
                "number": current_version + 1,
                "message": version_message,
            },
        }
        if parent_id and not restore_only:
            payload["parentId"] = parent_id
        self.put_json(f"/api/v2/pages/{quote(str(page_id))}", payload)
        return self.get_page_by_id(page_id)

    def _update_v1(
        self,
        *,
        page_id: str,
        current: dict[str, Any],
        title: str,
        body_storage: str,
        version_message: str,
        parent_id: str | None,
        status: str,
        restore_only: bool = False,
    ) -> dict[str, Any]:
        current_version = int((current.get("version") or {}).get("number") or 0)
        payload: dict[str, Any] = {
            "id": str(page_id),
            "type": "page",
            "status": status,
            "version": {
                "number": current_version + 1,
                "message": version_message,
            },
        }
        if not restore_only:
            payload.update(
                {
                    "title": title,
                    "space": {"key": (current.get("space") or {}).get("key")},
                    "body": {
                        "storage": {
                            "value": body_storage,
                            "representation": "storage",
                        }
                    },
                }
            )
            try:
                raw_current = self._raw_v1_page(page_id, status=status)
                ancestors = [
                    {"id": str(item.get("id"))}
                    for item in raw_current.get("ancestors") or []
                    if item.get("id")
                ]
                if ancestors:
                    payload["ancestors"] = ancestors
            except ConfluenceReaderError:
                pass
        if parent_id:
            parent_raw = self._raw_v1_page(parent_id)
            ancestors = [
                {"id": str(item.get("id"))}
                for item in parent_raw.get("ancestors") or []
                if item.get("id")
            ]
            ancestors.append({"id": str(parent_id)})
            payload["ancestors"] = ancestors
        self.put_json(f"/rest/api/content/{quote(str(page_id))}", payload)
        return self.get_page_by_id(page_id, status=status)

    def update_page(
        self,
        page_id: str,
        *,
        expected_version: int,
        title: str,
        body_storage: str,
        version_message: str = "",
        parent_id: str | None = None,
        status: str = "current",
        restore_only: bool = False,
    ) -> dict[str, Any]:
        current = self.get_page_by_id(page_id, status=status)
        current_version = int((current.get("version") or {}).get("number") or 0)
        if expected_version != current_version:
            raise VersionConflictError(
                "The page changed after it was read",
                status=409,
                details={
                    "page_id": str(page_id),
                    "expected_version": expected_version,
                    "current_version": current_version,
                    "current": current,
                },
            )
        if parent_id:
            self._same_space_parent(current, parent_id)
        if self.is_cloud:
            return self._update_v2(
                page_id=page_id,
                current=current,
                title=title,
                body_storage=body_storage,
                version_message=version_message,
                parent_id=parent_id,
                status=status,
                restore_only=restore_only,
            )
        return self._update_v1(
            page_id=page_id,
            current=current,
            title=title,
            body_storage=body_storage,
            version_message=version_message,
            parent_id=parent_id,
            status=status,
            restore_only=restore_only,
        )

    def supports_trash(self, space_key: str) -> bool:
        if self.is_cloud:
            return True
        if space_key in self._trash_support:
            return self._trash_support[space_key]
        try:
            self.get_json(
                f"/rest/api/space/{quote(str(space_key))}/trash",
                {"limit": 1},
            )
            self._trash_support[space_key] = True
        except (NotFoundError, ConfluenceReaderError):
            self._trash_support[space_key] = False
        return self._trash_support[space_key]

    def trash_page(self, page_id: str) -> dict[str, Any]:
        page = self.get_page_by_id(page_id)
        space_key = str((page.get("space") or {}).get("key") or "")
        if not self.supports_trash(space_key):
            raise UnsupportedOperationError(
                "This Confluence site does not expose a safe trash capability for the space",
                details={"page_id": str(page_id), "space_key": space_key},
            )
        if self.is_cloud:
            self.delete(f"/api/v2/pages/{quote(str(page_id))}")
        else:
            self.delete(f"/rest/api/content/{quote(str(page_id))}")
        return {
            "ok": True,
            "page_id": str(page_id),
            "title": page.get("title"),
            "space_key": space_key,
            "version_before": (page.get("version") or {}).get("number"),
            "version_after": None,
            "url": page.get("url"),
            "action": "trashed",
        }

    def get_trashed_page(self, page_id: str) -> dict[str, Any]:
        try:
            return self.get_page_by_id(page_id, status="trashed")
        except NotFoundError as exc:
            raise NotFoundError(
                "The trashed page was not found or cannot be accessed with this account",
                details={"page_id": str(page_id)},
            ) from exc

    def restore_page(self, page_id: str) -> dict[str, Any]:
        current = self.get_trashed_page(page_id)
        space_key = str((current.get("space") or {}).get("key") or "")
        if not self.supports_trash(space_key):
            raise UnsupportedOperationError(
                "This Confluence site does not expose a safe trash/restore capability",
                details={"page_id": str(page_id), "space_key": space_key},
            )
        expected_version = int((current.get("version") or {}).get("number") or 0)
        if self.is_cloud:
            restored = self._update_v2(
                page_id=page_id,
                current=current,
                title=str(current.get("title") or ""),
                body_storage=str(current.get("body_storage") or ""),
                version_message="Restored from trash by Confluence Agent",
                parent_id=None,
                status="current",
                restore_only=True,
            )
        else:
            restored = self._update_v1(
                page_id=page_id,
                current=current,
                title=str(current.get("title") or ""),
                body_storage=str(current.get("body_storage") or ""),
                version_message="Restored from trash by Confluence Agent",
                parent_id=None,
                status="current",
                restore_only=True,
            )
        return {
            "ok": True,
            "page_id": str(page_id),
            "title": restored.get("title"),
            "space_key": space_key,
            "version_before": expected_version,
            "version_after": (restored.get("version") or {}).get("number"),
            "url": restored.get("url"),
            "action": "restored",
        }

    def add_comment(self, page_id: str, body_storage: str) -> dict[str, Any]:
        page = self.get_page_by_id(page_id)
        payload = {
            "type": "comment",
            "title": "Re: " + str(page.get("title") or "comment"),
            "container": {"id": str(page_id), "type": "page"},
            "body": {
                "storage": {
                    "value": body_storage,
                    "representation": "storage",
                }
            },
        }
        data = self.post_json("/rest/api/content", payload)
        if not isinstance(data, dict):
            raise ApiError("Unexpected comment response from Confluence")
        return {
            "ok": True,
            "comment_id": str(data.get("id") or ""),
            "page_id": str(page_id),
            "url": self.web_url((data.get("_links") or {}).get("webui")),
        }

    def add_labels(self, page_id: str, labels: Iterable[str]) -> dict[str, Any]:
        cleaned = sorted({str(label).strip() for label in labels if str(label).strip()})
        if not cleaned:
            raise ValidationError("At least one label is required")
        payload = [{"prefix": "global", "name": label} for label in cleaned]
        data = self.post_json(
            f"/rest/api/content/{quote(str(page_id))}/label",
            payload,
        )
        result = data.get("results") if isinstance(data, dict) else None
        if not isinstance(result, list):
            result = []
        return {
            "ok": True,
            "page_id": str(page_id),
            "labels": [str(item.get("name")) for item in result if item.get("name")],
        }

    def upload_attachment(
        self,
        page_id: str,
        file_path: Path,
        *,
        replace_existing: bool = False,
        expected_attachment_version: int | None = None,
        comment: str = "",
    ) -> dict[str, Any]:
        if not file_path.is_file():
            raise ValidationError(f"Attachment file not found: {file_path}")
        size = file_path.stat().st_size
        if size > self.config.max_attachment_bytes:
            raise ValidationError(
                f"Attachment exceeds the configured {self.config.max_attachment_mb:g} MB limit"
            )
        existing = next(
            (
                item
                for item in self.get_attachments(page_id)
                if str(item.get("filename")) == file_path.name
            ),
            None,
        )
        fields = {"comment": comment, "minorEdit": "true"}
        if existing:
            if not replace_existing:
                raise ConflictError(
                    f"Attachment already exists: {file_path.name}",
                    details={
                        "attachment_id": existing.get("id"),
                        "version": existing.get("version"),
                    },
                )
            if expected_attachment_version is None:
                raise ValidationError(
                    "expected_attachment_version is required when replacing an attachment"
                )
            if int(existing.get("version") or 0) != expected_attachment_version:
                raise VersionConflictError(
                    "The attachment changed after it was read",
                    details={
                        "attachment_id": existing.get("id"),
                        "expected_version": expected_attachment_version,
                        "current_version": existing.get("version"),
                    },
                )
            endpoint = (
                f"/rest/api/content/{quote(str(page_id))}/child/attachment/"
                f"{quote(str(existing.get('id')))}/data"
            )
        else:
            endpoint = f"/rest/api/content/{quote(str(page_id))}/child/attachment"
        body, content_type = multipart_body(fields, {"file": file_path})
        response = self.transport.request(
            "POST",
            endpoint,
            data=body,
            content_type=content_type,
            extra_headers={"X-Atlassian-Token": "no-check"},
        )
        data = response.json()
        results = data.get("results") if isinstance(data, dict) else None
        item = results[0] if isinstance(results, list) and results else data
        if not isinstance(item, dict):
            raise ApiError("Unexpected attachment response from Confluence")
        normalized = self.normalize_attachment(item)
        return {
            "ok": True,
            "page_id": str(page_id),
            "attachment": normalized,
            "action": "replaced" if existing else "created",
        }

    def download_attachment(
        self,
        attachment: dict[str, Any],
        destination: Path,
        *,
        overwrite: bool = False,
        max_bytes: int | None = None,
    ) -> int:
        url = str(attachment.get("download_url") or "")
        if not url:
            raise ValidationError(
                f"Attachment has no download URL: {attachment.get('filename')}"
            )
        return self.transport.download(
            url,
            destination,
            max_bytes=max_bytes or self.config.max_attachment_bytes,
            overwrite=overwrite,
        )

    def normalize_page(self, data: dict[str, Any]) -> dict[str, Any]:
        space = dict(data.get("space") or {})
        space_id = str(space.get("id") or data.get("spaceId") or "")
        if space_id and (not space.get("key") or not space.get("name")):
            try:
                details = self.get_space_by_id(space_id)
            except ConfluenceReaderError:
                details = {}
            if details:
                space = {
                    "id": space_id,
                    "key": space.get("key") or details.get("key", ""),
                    "name": space.get("name") or details.get("name", ""),
                }
        webui = str((data.get("_links") or {}).get("webui") or "")
        if not space.get("key"):
            match = re.search(r"/spaces/([^/]+)/", webui)
            if match:
                space["key"] = match.group(1)
                space.setdefault("name", match.group(1))
        if space_id and not space.get("id"):
            space["id"] = space_id
        version = data.get("version") or {}
        labels_container = (
            (data.get("metadata") or {}).get("labels")
            or data.get("labels")
            or {}
        )
        if isinstance(labels_container, list):
            labels_container = {"results": labels_container}
        labels = [
            str(label.get("name"))
            for label in labels_container.get("results") or []
            if isinstance(label, dict) and label.get("name")
        ]
        ancestors = [self.normalize_summary(item) for item in data.get("ancestors") or []]
        body = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        version_by = version.get("by")
        if isinstance(version_by, dict):
            version_author = version_by.get("displayName", "")
        else:
            version_author = str(version_by or "")
        version_author = version_author or str(version.get("authorId") or "")
        return {
            "id": str(data.get("id", "")),
            "type": data.get("type", "page"),
            "status": data.get("status", ""),
            "title": data.get("title", ""),
            "space": {
                "id": str(space.get("id", space_id)),
                "key": space.get("key", ""),
                "name": space.get("name", ""),
            },
            "version": {
                "number": version.get("number"),
                "message": version.get("message", ""),
                "when": version.get("when") or version.get("createdAt", ""),
                "by": version_author,
            },
            "updated": version.get("when") or version.get("createdAt", ""),
            "author": version_author,
            "ancestors": ancestors,
            "parent_id": ancestors[-1]["id"] if ancestors else data.get("parentId"),
            "labels": labels,
            "url": self.web_url((data.get("_links") or {}).get("webui")),
            "body_storage": body,
        }

    def normalize_summary(self, data: dict[str, Any]) -> dict[str, Any]:
        space = data.get("space") or {}
        version = data.get("version") or {}
        ancestors = [
            {
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
            }
            for item in data.get("ancestors") or []
            if isinstance(item, dict)
        ]
        return {
            "id": str(data.get("id", "")),
            "type": data.get("type", "page"),
            "title": data.get("title", ""),
            "space": {
                "id": str(space.get("id", "")),
                "key": space.get("key", ""),
                "name": space.get("name", ""),
            },
            "version": version.get("number"),
            "updated": version.get("when", ""),
            "ancestors": ancestors,
            "parent_id": ancestors[-1]["id"] if ancestors else data.get("parentId"),
            "url": self.web_url((data.get("_links") or {}).get("webui")),
        }

    def normalize_search_result(self, item: dict[str, Any]) -> dict[str, Any]:
        content = item.get("content") or {}
        space = content.get("space") or item.get("resultGlobalContainer") or {}
        version = content.get("version") or {}
        content_id = content.get("id") or item.get("contentId")
        return {
            "id": str(content_id or ""),
            "type": content.get("type") or item.get("entityType") or "page",
            "title": content.get("title") or item.get("title") or "",
            "space": {
                "key": space.get("key", ""),
                "name": space.get("name") or space.get("title") or "",
            },
            "excerpt": excerpt_from_search(item.get("excerpt") or ""),
            "updated": version.get("when") or item.get("lastModified") or "",
            "url": self.web_url(
                (content.get("_links") or {}).get("webui") or item.get("url") or ""
            ),
        }

    def normalize_comment(self, item: dict[str, Any]) -> dict[str, Any]:
        version = item.get("version") or {}
        history = item.get("history") or {}
        author = (
            (version.get("by") or {}).get("displayName")
            or (history.get("createdBy") or {}).get("displayName")
            or ""
        )
        body = ((item.get("body") or {}).get("storage") or {}).get("value") or ""
        return {
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "author": author,
            "created": history.get("createdDate") or version.get("when"),
            "body_storage": body,
            "url": self.web_url((item.get("_links") or {}).get("webui")),
        }

    def normalize_attachment(self, item: dict[str, Any]) -> dict[str, Any]:
        extensions = item.get("extensions") or {}
        metadata = item.get("metadata") or {}
        links = item.get("_links") or {}
        return {
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "filename": item.get("title", ""),
            "media_type": extensions.get("mediaType") or metadata.get("mediaType") or "",
            "size": int(extensions.get("fileSize") or 0),
            "version": (item.get("version") or {}).get("number"),
            "download_url": self.web_url(links.get("download") or ""),
            "url": self.web_url(links.get("webui") or ""),
        }

    def normalize_version(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": item.get("number"),
            "message": item.get("message", ""),
            "created": item.get("createdAt") or item.get("when") or "",
            "author": (
                item.get("authorDisplayName")
                or (item.get("by") or {}).get("displayName")
                or item.get("authorId")
                or ""
            ),
            "minor_edit": bool(item.get("minorEdit") or item.get("minorEdit") is True),
        }
