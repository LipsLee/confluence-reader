#!/usr/bin/env python3
"""Backward-compatible import path for the Markdown converters."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from confluence_reader.markdown import (  # noqa: E402,F401,I001
    body_to_storage,
    markdown_to_storage,
    storage_to_markdown,
    strip_html,
)

__all__ = [
    "body_to_storage",
    "markdown_to_storage",
    "storage_to_markdown",
    "strip_html",
]
