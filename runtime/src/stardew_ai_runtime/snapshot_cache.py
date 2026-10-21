"""Coordinate bridge publication and MCP reads of observation/turn cache files."""
from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def cache_lock(path: Path) -> Iterator[None]:
    # Windows cannot replace a file while an ordinary reader has it open.
    # Readers therefore use the same cross-process mutex as publishers, and
    # close their data handle before releasing it. The lock file is never replaced.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_cache(path: Path) -> Any:
    with cache_lock(path):
        return json.loads(path.read_text(encoding="utf-8"))


def write_cache(path: Path, value: Any) -> None:
    with cache_lock(path):
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
