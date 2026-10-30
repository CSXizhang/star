"""Shared lock primitives; each store owns its read/modify/write transaction."""
from __future__ import annotations

import contextlib
import os
from typing import BinaryIO


def lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock_file(handle: BinaryIO) -> None:
    # Preserve the stores' cleanup policy without hiding transaction failures.
    with contextlib.suppress(Exception):
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
