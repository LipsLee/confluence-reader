#!/usr/bin/env python3
"""Compatibility entry point for the Confluence Agent CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from confluence_reader.cli import main  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name != "markdown_it" or os.environ.get("CONFLUENCE_READER_BOOTSTRAPPED"):
        raise
    uv = shutil.which("uv")
    if not uv:
        print(
            "Dependencies are missing. Install with: uv tool install "
            f'"{_ROOT}"',
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    environment = dict(os.environ)
    environment["CONFLUENCE_READER_BOOTSTRAPPED"] = "1"
    completed = subprocess.run(
        [
            uv,
            "run",
            "--project",
            str(_ROOT),
            "python",
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ],
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode) from exc


if __name__ == "__main__":
    raise SystemExit(main())
