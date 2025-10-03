"""Simple file-based locking utilities."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

from ..config import LOCK_EXPIRE_SEC, WORK_DIR_NAME
from ..errors import LockTimeoutError


class FileLock:
    """A cooperative lock implemented using ``.lock`` files."""

    def __init__(self, album_root: Path, name: str):
        self.lock_path = album_root / WORK_DIR_NAME / "locks" / f"{name}.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self, *, timeout: float = LOCK_EXPIRE_SEC) -> None:
        deadline = time.monotonic() + timeout
        info = {
            "pid": os.getpid(),
            "time": time.time(),
            "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        }
        payload = json.dumps(info)
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload.encode("utf-8"))
                finally:
                    os.close(fd)
                return
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise LockTimeoutError(f"Timed out acquiring lock {self.lock_path}")
                # Check expiry
                try:
                    stat = self.lock_path.stat()
                    if time.time() - stat.st_mtime > LOCK_EXPIRE_SEC:
                        self.lock_path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
                time.sleep(0.1)

    def release(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
