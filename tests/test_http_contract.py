from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from confluence_reader.client import ConfluenceClient
from confluence_reader.config import Config
from confluence_reader.errors import ApiError


def page_payload(page_id: str, version: int, *, title: str = "Doc") -> dict[str, Any]:
    return {
        "id": page_id,
        "type": "page",
        "status": "current",
        "title": title,
        "space": {"id": "1", "key": "DOC", "name": "Docs"},
        "version": {"number": version, "when": "", "by": {"displayName": "Tester"}},
        "ancestors": [],
        "metadata": {"labels": {"results": []}},
        "body": {"storage": {"value": "<p>Hello</p>"}},
        "_links": {"webui": f"/spaces/DOC/pages/{page_id}/Doc"},
    }


def v2_page_payload(page_id: str, version: int, *, title: str = "Doc") -> dict[str, Any]:
    return {
        "id": page_id,
        "type": "page",
        "status": "current",
        "title": title,
        "spaceId": "1",
        "parentId": None,
        "version": {
            "number": version,
            "message": "",
            "createdAt": "2025-01-01T00:00:00Z",
            "authorId": "author-1",
        },
        "body": {
            "storage": {
                "representation": "storage",
                "value": "<p>Hello</p>",
            }
        },
        "_links": {"webui": f"/spaces/DOC/pages/{page_id}/Doc"},
        "labels": {"results": [{"name": "test"}]},
    }


class Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    fail_post = False

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.requests.append({"method": "GET", "path": self.path, "body": b""})
        if "/api/v2/spaces" in self.path:
            if "/api/v2/spaces/1" in self.path:
                self._json(200, {"id": "1", "key": "DOC", "name": "Docs"})
            else:
                self._json(200, {"results": [{"id": "1", "key": "DOC"}]})
            return
        if "/api/v2/pages/123/versions" in self.path:
            self._json(
                200,
                {
                    "results": [
                        {"number": 1, "message": "One", "createdAt": "2025-01-01T00:00:00Z"}
                    ],
                    "_links": {},
                },
            )
            return
        if "/api/v2/pages/123" in self.path:
            self._json(200, v2_page_payload("123", 2))
            return
        if "/rest/api/content/123" in self.path:
            self._json(200, page_payload("123", 2))
            return
        self._json(404, {"message": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        self.requests.append({"method": "POST", "path": self.path, "body": body})
        if self.fail_post:
            self._json(500, {"message": "server error"})
            return
        if self.path.endswith("/api/v2/pages"):
            self._json(200, {"id": "123"})
            return
        self._json(404, {"message": "not found"})

    def do_PUT(self) -> None:
        body = self._body()
        self.requests.append({"method": "PUT", "path": self.path, "body": body})
        self._json(200, {"id": "123"})

    def do_DELETE(self) -> None:
        self.requests.append({"method": "DELETE", "path": self.path, "body": b""})
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def server():
    Handler.requests = []
    Handler.fail_post = False
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def config_for(server: ThreadingHTTPServer, tmp_path: Path) -> Config:
    return Config(
        base_url=f"http://127.0.0.1:{server.server_port}/wiki",
        personal_access_token="token",
        auth="bearer",
        state_dir=tmp_path / "state",
        flavor="cloud",
    )


def test_cloud_create_uses_v2_and_no_purge(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    client = ConfluenceClient(config_for(server, tmp_path))
    page = client.create_page(space_key="DOC", title="Doc", body_storage="<p>Hello</p>")
    assert page["id"] == "123"
    assert page["space"]["key"] == "DOC"
    assert page["labels"] == ["test"]
    post = next(item for item in Handler.requests if item["method"] == "POST")
    payload = json.loads(post["body"])
    assert payload["spaceId"] == "1"
    assert payload["body"]["representation"] == "storage"
    assert any(item["method"] == "GET" and "/api/v2/pages/123" in item["path"] for item in Handler.requests)
    client.trash_page("123")
    delete = next(item for item in Handler.requests if item["method"] == "DELETE")
    assert "purge" not in delete["path"]


def test_cloud_update_sends_incremented_version(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    client = ConfluenceClient(config_for(server, tmp_path))
    result = client.update_page(
        "123",
        expected_version=2,
        title="Changed",
        body_storage="<p>New</p>",
    )
    assert result["id"] == "123"
    put = next(item for item in Handler.requests if item["method"] == "PUT")
    payload = json.loads(put["body"])
    assert payload["version"]["number"] == 3
    assert payload["title"] == "Changed"


def test_writes_are_not_retried(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    Handler.fail_post = True
    client = ConfluenceClient(config_for(server, tmp_path))
    with pytest.raises(ApiError):
        client.create_page(space_key="DOC", title="Doc", body_storage="<p>Hello</p>")
    posts = [item for item in Handler.requests if item["method"] == "POST"]
    assert len(posts) == 1
