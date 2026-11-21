"""PicoLimbo for the authentication lobby: compiled during the Docker build.

PicoLimbo (https://github.com/Quozul/PicoLimbo) is fetched and compiled in its own Dockerfile
build stage with MineNode's login patch (``assets/picolimbo/minenode-auth.patch``) and copied
into the runtime image at ``/app/bin/pico_limbo`` together with ``picolimbo-version.json``.
No PicoLimbo source files live in this repository, and no compiler or Rust toolchain runs in the
runtime container. ``MINENODE_PICOLIMBO_BINARY`` overrides the bundled binary path.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from .config import AUTH_LOBBY_DIR, PICOLIMBO_BIN, PICOLIMBO_META

log = logging.getLogger("minenode.picolimbo")

REPO = os.environ.get("MINENODE_PICOLIMBO_REPO", "Quozul/PicoLimbo")
PATCH = Path(__file__).parent / "assets" / "picolimbo" / "minenode-auth.patch"
BINARY = PICOLIMBO_BIN
VERSION_FILE = PICOLIMBO_META
LOCAL_OVERRIDE = os.environ.get("MINENODE_PICOLIMBO_BINARY")
_LEGACY_DIRS = ("rust", "build", "bin")


class PicoLimboError(Exception):
    pass


def display_version(tag: str | None) -> str | None:
    """v1.15.0+mc26.3 -> 1.15.0+mc26.3"""
    if not tag:
        return None
    return tag[1:] if tag.startswith("v") else tag


def _binary_path() -> Path:
    return Path(LOCAL_OVERRIDE) if LOCAL_OVERRIDE else BINARY


def _version_file(bin_path: Path) -> Path:
    return bin_path.with_name("picolimbo-version.json") if LOCAL_OVERRIDE else VERSION_FILE


def installed() -> dict | None:
    """Metadata of the bundled or locally overridden PicoLimbo binary, if present."""
    bin_path = _binary_path()
    if not bin_path.is_file():
        return None
    try:
        data = json.loads(_version_file(bin_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def binary() -> Path:
    """Return the executable PicoLimbo binary path or raise a clear configuration error."""
    bin_path = _binary_path()
    if bin_path.is_file():
        return bin_path
    if LOCAL_OVERRIDE:
        raise PicoLimboError(f"MINENODE_PICOLIMBO_BINARY wskazuje na nieistniejący plik: {bin_path}")
    raise PicoLimboError(
        f"Brak skompilowanego pliku PicoLimbo: {bin_path} "
        "(zbuduj obraz Docker MineNode lub ustaw MINENODE_PICOLIMBO_BINARY)"
    )


def status() -> dict:
    """Status metadata exposed to the Authentication panel view."""
    info = installed()
    version = (info or {}).get("version") or display_version((info or {}).get("tag"))
    return {
        "source": "local" if LOCAL_OVERRIDE else "image",
        "repo": (info or {}).get("repo") or REPO,
        "version": version,
        "tag": (info or {}).get("tag"),
        "built_at": (info or {}).get("built_at"),
    }


def cleanup_legacy_data(lobby_dir: Path | None = None) -> list[str]:
    """Remove leftover runtime compilation directories from older installs (~1 GB under /data)."""
    base = lobby_dir or AUTH_LOBBY_DIR
    removed: list[str] = []
    for name in _LEGACY_DIRS:
        target = base / name
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
            removed.append(name)
        except OSError as exc:
            log.warning("Could not remove legacy PicoLimbo directory %s: %s", target, exc)
    if removed:
        log.info("Removed legacy runtime PicoLimbo directories from %s: %s", base, ", ".join(removed))
    return removed
