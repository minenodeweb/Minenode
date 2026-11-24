"""Tiny, crash-safe JSON persistence (atomic writes, thread-safe)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class JsonStore:
    """A JSON file kept in memory.

    ``follow_file=True`` re-reads the file when something else changed it on disk (e.g. the
    ``reset_password.sh`` tool editing users.json while the panel runs); without it the panel would
    keep the old data and overwrite the outside change on its next save."""

    def __init__(self, path: Path, default: Callable[[], Any], follow_file: bool = False):
        self.path = path
        self._default = default
        self._lock = threading.RLock()
        self._follow = follow_file
        self._seen = self._stamp()
        self._data = self._read()

    def _read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return self._default()
        except json.JSONDecodeError:
            # Keep a copy of the broken file and start fresh instead of crashing the panel.
            broken = self.path.with_suffix(self.path.suffix + ".broken")
            os.replace(self.path, broken)
            return self._default()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    @property
    def data(self) -> Any:
        if self._follow:
            with self._lock:
                stamp = self._stamp()
                if stamp != self._seen:
                    self._seen = stamp
                    self._data = self._read()
        return self._data

    def save(self, data: Any | None = None) -> None:
        with self._lock:
            if data is not None:
                self._data = data
            atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))
            self._seen = self._stamp()
