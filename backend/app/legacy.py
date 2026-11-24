"""Data written before the rename from MineNode to Varden, carried over to the new names.

Each step is a no-op once done (or on a fresh install), so it is safe to run on every start.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("varden.legacy")

OLD = "minenode"
META_DIR = ".varden"
OLD_META_DIR = f".{OLD}"
PACK_EXTENSIONS = (".vardenpack", f".{OLD}pack")
PACK_FORMATS = ("vardenpack", f"{OLD}pack")


def migrate_meta_dir(server_dir: Path) -> None:
    """``<server>/.minenode`` (panel metadata) becomes ``<server>/.varden``."""
    old, new = Path(server_dir) / OLD_META_DIR, Path(server_dir) / META_DIR
    if not old.is_dir():
        return
    try:
        if not new.exists():
            os.replace(old, new)
            return
        # Both exist (e.g. an old backup restored over a migrated server): keep what is newer, add the rest.
        for item in old.rglob("*"):
            target = new / item.relative_to(old)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), target)
        shutil.rmtree(old, ignore_errors=True)
    except OSError:
        log.exception("Could not move %s to %s", old, new)


def remove_old_router(extensions_dir: Path) -> None:
    """The Geyser extension used to be MineNodeRouter: two routers must never be loaded at once."""
    for name in ("MineNodeRouter.jar", f"{OLD}-router"):
        path = Path(extensions_dir) / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError:
            log.exception("Could not remove the old Geyser extension %s", path)


def is_pack_name(filename: str) -> bool:
    return filename.lower().endswith(PACK_EXTENSIONS)
