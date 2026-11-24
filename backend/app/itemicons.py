"""Item and block icons for the public player-statistics page.

Blocks are rendered from their JSON models into shaded isometric PNGs (see :mod:`app.blockrender`);
items without a block model fall back to their flat texture sprite. Assets are read in a single
sequential pass directly from (in increasing priority):

* the vanilla ``client.jar`` for the server's Minecraft version,
* every enabled mod/plugin JAR in the server's content folder,
* the server's locally installed resource pack(s), which override vanilla/mod assets.

The first time an asset set is seen, all block models are rendered in parallel and bundled together
with all flat textures into a single atomic binary pack (``CACHE_DIR/itemicons/packs/<key>.mnpk``).
No loose asset or icon files are unpacked to disk. Subsequent icon requests are served in O(1)
directly from the pack index and file.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import threading
import time
import weakref
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from . import blockrender, net, providers
from .config import CACHE_DIR
from .content import content_dir

if TYPE_CHECKING:
    from .manager import ServerInstance

ROOT = CACHE_DIR / "itemicons"
JAR_DIR = ROOT / "client"
PACKS_DIR = ROOT / "packs"

# Isometric camera and output size used for rendered block icons.
RENDER_SIZE = 128
ROT_Y, ROT_X = -45.0, 30.0

PACK_MAGIC = b"MNPK0001"
PACK_HEADER_FMT = "<8sQI"
PACK_HEADER_SIZE = struct.calcsize(PACK_HEADER_FMT)
PACK_FORMAT_VERSION = 1

MAX_LOADED_PACKS = 8
MAX_DISK_PACKS = 24
VANILLA_RETRY_COOLDOWN = 60.0

_LEGACY_DIRS = ("assets", "overlays", "png", "blocks", "index")

# Default biome colours for blocks whose textures are tinted by Minecraft.
_TINTS: dict[str, tuple[int, int, int]] = {
    "grass_block": (124, 189, 107),
    "short_grass": (124, 189, 107),
    "grass": (124, 189, 107),
    "tall_grass": (124, 189, 107),
    "fern": (124, 189, 107),
    "large_fern": (124, 189, 107),
    "vine": (124, 189, 107),
    "lily_pad": (124, 189, 107),
    "sugar_cane": (124, 189, 107),
    "bush": (124, 189, 107),
    "water": (63, 116, 204),
    "oak_leaves": (83, 183, 63),
    "spruce_leaves": (48, 112, 62),
    "birch_leaves": (92, 174, 67),
    "jungle_leaves": (68, 152, 49),
    "acacia_leaves": (96, 155, 50),
    "dark_oak_leaves": (49, 127, 47),
    "cherry_leaves": (255, 174, 201),
    "mangrove_leaves": (68, 156, 68),
}

# Derived blocks reuse their base material's flat texture when no block model is available.
_SHAPES = (
    "_pressure_plate", "_fence_gate", "_hanging_sign",
    "_slab", "_stairs", "_wall", "_button", "_fence", "_trapdoor", "_door", "_sign",
)
_PLURALS = {
    "brick": "bricks",
    "stone_brick": "stone_bricks",
    "nether_brick": "nether_bricks",
    "red_nether_brick": "red_nether_bricks",
    "mud_brick": "mud_bricks",
    "prismarine_brick": "prismarine_bricks",
    "quartz": "quartz_block",
}
_TEXTURE_VARIANTS = ("_top", "_side", "_bottom", "_item")

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_STATE_LOCK = threading.Lock()
_PACKS_LOCK = threading.Lock()
_PACKS: OrderedDict[str, "IconPack"] = OrderedDict()
_BUILD_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()
_JAR_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()
_VANILLA_RETRY_AT: dict[str, float] = {}
_LEGACY_CLEANED: set[Path] = set()


def clear_state() -> None:
    """Reset in-memory caches and cooldowns (mainly for tests)."""
    with _PACKS_LOCK:
        _PACKS.clear()
    with _STATE_LOCK:
        _BUILD_LOCKS.clear()
        _JAR_LOCKS.clear()
        _VANILLA_RETRY_AT.clear()
        _LEGACY_CLEANED.clear()


def _safe_name(value: str) -> str:
    return _SAFE.sub("_", str(value))[:120] or "unknown"


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get_loop_lock(
    registry: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]],
    key: str,
) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _STATE_LOCK:
        per_loop = registry.get(loop)
        if per_loop is None:
            per_loop = {}
            registry[loop] = per_loop
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        return lock


def _scan_files(folder: Path, suffix: str) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.name.lower().endswith(suffix):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                out.append((entry.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return []
    out.sort(key=lambda item: item[0])
    return out


def _mod_jars(inst: "ServerInstance") -> list[Path]:
    folder = content_dir(inst)
    try:
        return sorted(p for p in folder.glob("*.jar") if p.is_file())
    except OSError:
        return []


def _resource_packs(inst: "ServerInstance") -> list[Path]:
    """Locally installed resource-pack ZIPs (their textures override vanilla/mod icons)."""
    folder = Path(inst.dir) / ".varden" / "resourcepacks"
    try:
        return sorted(p for p in folder.glob("*.zip") if p.is_file())
    except OSError:
        return []


def signature(inst: "ServerInstance") -> list[Any]:
    version = str(getattr(inst.cfg, "mc_version", "") or "")
    mods = _scan_files(content_dir(inst), ".jar")
    packs = _scan_files(Path(inst.dir) / ".varden" / "resourcepacks", ".zip")
    return [version, mods, packs]


def _assets_key(sig: Any) -> str:
    """Stable id for an asset set – identical for every server sharing version/mods/pack."""
    payload = json.dumps([PACK_FORMAT_VERSION, RENDER_SIZE, sig], sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _pack_path(key: str) -> Path:
    return PACKS_DIR / f"{key}.mnpk"


def _candidates(item_id: str) -> list[str]:
    """Ordered id -> flat texture keys to try, from the exact id to derived/base textures."""
    ns, _, name = item_id.partition(":")
    if not name:
        ns, name = "minecraft", ns
    names = [name]
    names += [f"{name}{suffix}" for suffix in _TEXTURE_VARIANTS]
    names.append(f"{name}_spawn_egg")
    for shape in _SHAPES:
        if not name.endswith(shape) or len(name) == len(shape):
            continue
        base = name[: -len(shape)]
        names += [base, f"{base}_planks", f"{base}_block", f"{base}_bricks", f"{base}s"]
        if base in _PLURALS:
            names.append(_PLURALS[base])
    seen: set[str] = set()
    out: list[str] = []
    for value in names:
        if not value:
            continue
        for key in (f"{ns}:{value}", value):
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _tint_for(name: str) -> tuple[int, int, int] | None:
    if name.endswith("_leaves"):
        return _TINTS.get(name) or (83, 183, 63)
    return _TINTS.get(name)


def _norm_loc(loc: str, default_ns: str = "minecraft") -> str:
    return loc if ":" in loc else f"{default_ns}:{loc}"


class IconPack:
    """In-memory index over a single ``<key>.mnpk`` binary icon pack."""

    def __init__(
        self,
        key: str,
        path: Path,
        *,
        has_vanilla: bool,
        blobs: list[tuple[int, int]],
        blocks: dict[str, int],
        flat: dict[str, int],
    ) -> None:
        self.key = key
        self.path = path
        self.has_vanilla = has_vanilla
        self.blobs = blobs
        self.blocks = blocks
        self.flat = flat

    @classmethod
    def open(cls, key: str, path: Path) -> "IconPack | None":
        try:
            file_size = path.stat().st_size
            if file_size < PACK_HEADER_SIZE:
                return None
            with path.open("rb") as fh:
                magic, header_offset, header_length = struct.unpack(
                    PACK_HEADER_FMT, fh.read(PACK_HEADER_SIZE)
                )
                if magic != PACK_MAGIC:
                    return None
                if header_offset < PACK_HEADER_SIZE or header_offset + header_length > file_size:
                    return None
                fh.seek(header_offset)
                meta = json.loads(fh.read(header_length).decode("utf-8"))
        except (OSError, ValueError, struct.error):
            return None

        if not isinstance(meta, dict):
            return None
        if meta.get("version") != PACK_FORMAT_VERSION or meta.get("render_size") != RENDER_SIZE:
            return None
        raw_blobs = meta.get("blobs")
        raw_blocks = meta.get("blocks")
        raw_flat = meta.get("flat")
        if not (isinstance(raw_blobs, list) and isinstance(raw_blocks, dict) and isinstance(raw_flat, dict)):
            return None

        blobs: list[tuple[int, int]] = []
        for entry in raw_blobs:
            if not (isinstance(entry, list) and len(entry) == 2):
                return None
            off, length = entry
            if not (isinstance(off, int) and isinstance(length, int)):
                return None
            if off < PACK_HEADER_SIZE or length < 0 or off + length > header_offset:
                return None
            blobs.append((off, length))

        blocks = {k: v for k, v in raw_blocks.items() if isinstance(k, str) and isinstance(v, int)}
        flat = {k: v for k, v in raw_flat.items() if isinstance(k, str) and isinstance(v, int)}
        return cls(
            key,
            path,
            has_vanilla=bool(meta.get("has_vanilla", False)),
            blobs=blobs,
            blocks=blocks,
            flat=flat,
        )

    def resolve_blob(self, item_id: str) -> int | None:
        item_id = str(item_id or "").strip()
        if not item_id or len(item_id) > 128:
            return None
        canon = item_id if ":" in item_id else f"minecraft:{item_id}"
        idx = self.blocks.get(canon)
        if idx is None and canon != item_id:
            idx = self.blocks.get(item_id)
        if idx is not None:
            return idx
        for candidate in _candidates(item_id):
            idx = self.flat.get(candidate)
            if idx is not None:
                return idx
        return None

    def get(self, item_id: str) -> tuple[bytes, str] | None:
        idx = self.resolve_blob(item_id)
        if idx is None or not (0 <= idx < len(self.blobs)):
            return None
        off, length = self.blobs[idx]
        try:
            with self.path.open("rb") as fh:
                fh.seek(off)
                data = fh.read(length)
        except OSError:
            return None
        if len(data) != length:
            return None
        return data, f'"{self.key}-{idx}"'


def _write_pack(
    path: Path,
    blobs: list[bytes],
    blocks_map: dict[str, int],
    flat_map: dict[str, int],
    *,
    has_vanilla: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    try:
        blob_table: list[list[int]] = []
        pos = PACK_HEADER_SIZE
        with tmp.open("wb") as fh:
            fh.write(b"\x00" * PACK_HEADER_SIZE)
            for data in blobs:
                fh.write(data)
                blob_table.append([pos, len(data)])
                pos += len(data)
            header = json.dumps(
                {
                    "version": PACK_FORMAT_VERSION,
                    "render_size": RENDER_SIZE,
                    "has_vanilla": has_vanilla,
                    "blobs": blob_table,
                    "blocks": blocks_map,
                    "flat": flat_map,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            fh.write(header)
            fh.seek(0)
            fh.write(struct.pack(PACK_HEADER_FMT, PACK_MAGIC, pos, len(header)))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _get_loaded_pack(key: str) -> IconPack | None:
    with _PACKS_LOCK:
        pack = _PACKS.get(key)
        if pack is not None:
            if pack.path.is_file():
                _PACKS.move_to_end(key)
                return pack
            _PACKS.pop(key, None)

    path = _pack_path(key)
    if not path.is_file():
        return None
    pack = IconPack.open(key, path)
    if pack is None:
        return None
    return _cache_pack(key, pack)


def _cache_pack(key: str, pack: IconPack) -> IconPack:
    with _PACKS_LOCK:
        _PACKS[key] = pack
        _PACKS.move_to_end(key)
        while len(_PACKS) > MAX_LOADED_PACKS:
            _PACKS.popitem(last=False)
    return pack


def _parse_tint_value(val: Any) -> tuple[int, int, int] | None:
    if isinstance(val, int) and not isinstance(val, bool):
        return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        try:
            r, g, b = float(val[0]), float(val[1]), float(val[2])
        except (TypeError, ValueError):
            return None
        if 0.0 <= r <= 1.0 and 0.0 <= g <= 1.0 and 0.0 <= b <= 1.0:
            return (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))
        return (
            min(255, max(0, int(r))),
            min(255, max(0, int(g))),
            min(255, max(0, int(b))),
        )
    return None


def _extract_item_def(
    raw: bytes, default_ns: str = "minecraft"
) -> tuple[str | None, tuple[int, int, int] | None]:
    """Extract target model and optional constant tint from an MC 1.21.4+ ``items/*.json`` def."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    node: Any = data.get("model")
    for _ in range(16):
        if not isinstance(node, dict):
            break
        model_val = node.get("model")
        if isinstance(model_val, str):
            tint: tuple[int, int, int] | None = None
            tints = node.get("tints")
            if isinstance(tints, list):
                for entry in tints:
                    if isinstance(entry, dict) and entry.get("type") in ("minecraft:constant", "constant"):
                        tint = _parse_tint_value(entry.get("value"))
                        if tint is not None:
                            break
            return _norm_loc(model_val, default_ns), tint
        if isinstance(model_val, dict):
            node = model_val
            continue
        if isinstance(node.get("on_false"), dict):
            node = node["on_false"]
            continue
        if isinstance(node.get("fallback"), dict):
            node = node["fallback"]
            continue
        cases = node.get("cases")
        if isinstance(cases, list) and cases and isinstance(cases[0], dict) and isinstance(cases[0].get("model"), dict):
            node = cases[0]["model"]
            continue
        entries = node.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict) and isinstance(entries[0].get("model"), dict):
            node = entries[0]["model"]
            continue
        models = node.get("models")
        if isinstance(models, list) and models and isinstance(models[0], dict):
            node = models[0]
            continue
        if isinstance(node.get("on_true"), dict):
            node = node["on_true"]
            continue
        break
    return None, None


def _extract_blockstate_model(raw: bytes, default_ns: str = "minecraft") -> str | None:
    """Pick a representative block model from a ``blockstates/*.json`` file."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def _pick(entry: Any) -> str | None:
        if isinstance(entry, list) and entry:
            entry = entry[0]
        if isinstance(entry, dict):
            m = entry.get("model")
            if isinstance(m, str) and m:
                return _norm_loc(m, default_ns)
        return None

    variants = data.get("variants")
    if isinstance(variants, dict):
        if "" in variants:
            picked = _pick(variants[""])
            if picked:
                return picked
        for val in reversed(list(variants.values())):
            picked = _pick(val)
            if picked:
                return picked

    multipart = data.get("multipart")
    if isinstance(multipart, list):
        for part in multipart:
            if isinstance(part, dict):
                picked = _pick(part.get("apply"))
                if picked:
                    return picked
    return None


def _extract_layer0(
    renderer: blockrender.MinecraftBlockRenderer,
    model_loc: str,
    source: blockrender.MemoryAssetSource,
) -> str | None:
    ns, _ = renderer._strip_namespace(model_loc)
    layer0: str | None = None
    try:
        resolved = renderer.resolve_model(model_loc)
        cand = (resolved.get("textures") or {}).get("layer0")
        if isinstance(cand, str):
            layer0 = cand
    except (FileNotFoundError, ValueError, OSError, TypeError):
        try:
            raw = renderer.load_model_json(model_loc)
            cand = (raw.get("textures") or {}).get("layer0") if isinstance(raw, dict) else None
            if isinstance(cand, str):
                layer0 = cand
        except (FileNotFoundError, ValueError, OSError):
            return None
    if not layer0 or layer0.startswith("#"):
        return None
    norm = _norm_loc(layer0, ns)
    if source.has_texture(*renderer._strip_namespace(norm)):
        return norm
    return None


def _discover_targets(
    source: blockrender.MemoryAssetSource,
    renderer: blockrender.MinecraftBlockRenderer,
) -> tuple[dict[str, tuple[str, tuple[int, int, int] | None]], dict[str, str]]:
    """Identify renderable block models and explicit item layer0 sprites in one pass."""
    block_targets: dict[str, tuple[str, tuple[int, int, int] | None]] = {}
    item_layer0: dict[str, str] = {}

    # 1. Canonical block items known from blockstates/<name>.json -> <ns>:block/<name>
    for item_id in source.blockstates:
        ns, name = item_id.split(":", 1)
        candidate = f"{ns}:block/{name}"
        if renderer.has_concrete_textures(candidate):
            block_targets[item_id] = (candidate, None)

    # 2. MC 1.21.4+ assets/<ns>/items/<name>.json definitions (models & constant tints)
    for item_id, raw in source.item_defs.items():
        ns, _ = item_id.split(":", 1)
        target_model, item_tint = _extract_item_def(raw, default_ns=ns)
        if not target_model:
            continue
        if renderer.has_concrete_textures(target_model):
            if item_id not in block_targets:
                block_targets[item_id] = (target_model, item_tint)
            elif item_tint is not None and block_targets[item_id][1] is None:
                block_targets[item_id] = (block_targets[item_id][0], item_tint)
        elif item_id not in item_layer0:
            layer0 = _extract_layer0(renderer, target_model, source)
            if layer0:
                item_layer0[item_id] = layer0

    # 3. Item models assets/<ns>/models/item/<name>.json (doors, stairs, slabs, custom items)
    for model_loc in source.models:
        ns, path = model_loc.split(":", 1)
        if not path.startswith("item/") or "/" in path[5:]:
            continue
        item_id = f"{ns}:{path[5:]}"
        if item_id in block_targets:
            continue
        if renderer.has_concrete_textures(model_loc):
            chosen = model_loc
            try:
                raw = renderer.load_model_json(model_loc)
            except (FileNotFoundError, ValueError, OSError):
                raw = None
            if (
                isinstance(raw, dict)
                and not raw.get("textures")
                and not raw.get("elements")
                and isinstance(raw.get("parent"), str)
            ):
                parent_loc = _norm_loc(raw["parent"], ns)
                if renderer.has_concrete_textures(parent_loc):
                    chosen = parent_loc
            block_targets[item_id] = (chosen, None)
        elif item_id not in item_layer0:
            layer0 = _extract_layer0(renderer, model_loc, source)
            if layer0:
                item_layer0[item_id] = layer0

    # 4. Blockstates whose model path differs from block/<name> (e.g. crops, pistons)
    for item_id, raw in source.blockstates.items():
        if item_id in block_targets:
            continue
        ns, _ = item_id.split(":", 1)
        variant_model = _extract_blockstate_model(raw, default_ns=ns)
        if variant_model and renderer.has_concrete_textures(variant_model):
            block_targets[item_id] = (variant_model, None)

    # 5. Fallback for minimal/mod archives without blockstates/
    for model_loc in source.models:
        ns, path = model_loc.split(":", 1)
        if ns in source.namespaces_with_blockstates or not path.startswith("block/"):
            continue
        stem = path.rsplit("/", 1)[-1]
        item_id = f"{ns}:{stem}"
        if item_id not in block_targets and renderer.has_concrete_textures(model_loc):
            block_targets[item_id] = (model_loc, None)

    return block_targets, item_layer0


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _normalize_flat_png(raw: bytes) -> bytes:
    """Keep standard PNGs untouched; crop animated vertical strips (h > w) to frame 0."""
    if len(raw) >= 24 and raw[:8] == _PNG_SIG and raw[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw[16:24])
        if w > 0 and h <= w:
            return raw
    try:
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            if h > w > 0:
                frame = im.convert("RGBA").crop((0, 0, w, w))
                buf = io.BytesIO()
                frame.save(buf, format="PNG", compress_level=1)
                return buf.getvalue()
    except (OSError, ValueError):
        pass
    return raw


def _cleanup_legacy_and_prune(keep_path: Path) -> None:
    root_resolved = ROOT.resolve()
    need_legacy = False
    with _STATE_LOCK:
        if root_resolved not in _LEGACY_CLEANED:
            _LEGACY_CLEANED.add(root_resolved)
            need_legacy = True
    if need_legacy:
        for name in _LEGACY_DIRS:
            shutil.rmtree(ROOT / name, ignore_errors=True)

    try:
        packs = [p for p in PACKS_DIR.glob("*.mnpk") if p.is_file()]
    except OSError:
        return
    if len(packs) <= MAX_DISK_PACKS:
        return
    with_mtime: list[tuple[float, Path]] = []
    for p in packs:
        if p == keep_path:
            continue
        try:
            with_mtime.append((p.stat().st_mtime, p))
        except OSError:
            continue
    with_mtime.sort(key=lambda item: item[0])
    excess = len(packs) - MAX_DISK_PACKS
    for _, old_path in with_mtime[:excess]:
        with contextlib.suppress(OSError):
            old_path.unlink()
        with _PACKS_LOCK:
            _PACKS.pop(old_path.stem, None)


def _build_pack_sync(
    inst: "ServerInstance",
    sig: Any,
    key: str,
    jar: Path | None,
) -> IconPack:
    """Unpack archives once in memory, render all blocks in parallel, and write one .mnpk pack."""
    source = blockrender.MemoryAssetSource()
    has_vanilla = bool(jar and jar.is_file())
    if has_vanilla and jar is not None:
        source.load_archive(jar)
    for mod in _mod_jars(inst):
        source.load_archive(mod)
    for pack_zip in _resource_packs(inst):
        source.load_archive(pack_zip)

    renderer = blockrender.MinecraftBlockRenderer(source)
    block_targets, item_layer0 = _discover_targets(source, renderer)

    blobs_bytes: list[bytes] = []
    blob_by_sha1: dict[str, int] = {}

    def add_blob(data: bytes) -> int:
        digest = hashlib.sha1(data).hexdigest()
        idx = blob_by_sha1.get(digest)
        if idx is not None:
            return idx
        idx = len(blobs_bytes)
        blobs_bytes.append(data)
        blob_by_sha1[digest] = idx
        return idx

    # Deduplicate block renders sharing identical (model_loc, effective_tint)
    jobs: dict[tuple[str, tuple[int, int, int] | None], list[str]] = {}
    for item_id, (model_loc, item_tint) in block_targets.items():
        name = item_id.split(":", 1)[-1]
        tint = _tint_for(name) or item_tint
        jobs.setdefault((model_loc, tint), []).append(item_id)

    unique_jobs = list(jobs.keys())

    def render_job(job: tuple[str, tuple[int, int, int] | None]) -> bytes | None:
        model_loc, tint = job
        try:
            return renderer.render_icon_png(
                model_loc,
                size=RENDER_SIZE,
                rot_y=ROT_Y,
                rot_x=ROT_X,
                tint_rgb=tint,
                compress_level=1,
            )
        except Exception:
            return None

    if len(unique_jobs) <= 8:
        rendered_results = [render_job(j) for j in unique_jobs]
    else:
        workers = min(4, max(1, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rendered_results = list(pool.map(render_job, unique_jobs))

    blocks_map: dict[str, int] = {}
    for job, png_bytes in zip(unique_jobs, rendered_results):
        if not png_bytes:
            continue
        blob_idx = add_blob(png_bytes)
        for item_id in jobs[job]:
            blocks_map[item_id] = blob_idx

    # Collect flat fallback textures from the same in-memory source: block first, then item.
    flat_map: dict[str, int] = {}
    tex_loc_to_blob: dict[str, int] = {}
    block_tex_entries: list[tuple[str, str, str, bytes]] = []
    item_tex_entries: list[tuple[str, str, str, bytes]] = []

    for tex_loc, raw in source.textures.items():
        ns, rel = tex_loc.split(":", 1)
        head, _, tail = rel.partition("/")
        if head in ("block", "blocks") and tail:
            block_tex_entries.append((tex_loc, ns, tail, raw))
        elif head in ("item", "items") and tail:
            item_tex_entries.append((tex_loc, ns, tail, raw))

    def register_flat(entries: list[tuple[str, str, str, bytes]]) -> None:
        for tex_loc, ns, tail, raw in entries:
            normalized = _normalize_flat_png(raw)
            blob_idx = add_blob(normalized)
            tex_loc_to_blob[tex_loc] = blob_idx
            stem = tail.rsplit("/", 1)[-1]
            flat_map[f"{ns}:{stem}"] = blob_idx
            flat_map[f"{ns}:{tail}"] = blob_idx
            if ns == "minecraft":
                flat_map[stem] = blob_idx

    register_flat(block_tex_entries)
    register_flat(item_tex_entries)

    for item_id, tex_loc in item_layer0.items():
        blob_idx = tex_loc_to_blob.get(tex_loc)
        if blob_idx is None:
            raw = source.read_texture_bytes(*renderer._strip_namespace(tex_loc))
            if raw is None:
                continue
            blob_idx = add_blob(_normalize_flat_png(raw))
            tex_loc_to_blob[tex_loc] = blob_idx
        flat_map[item_id] = blob_idx
        if item_id.startswith("minecraft:"):
            flat_map[item_id.split(":", 1)[1]] = blob_idx

    out_path = _pack_path(key)
    _write_pack(out_path, blobs_bytes, blocks_map, flat_map, has_vanilla=has_vanilla)
    _cleanup_legacy_and_prune(out_path)
    pack = IconPack.open(key, out_path)
    if pack is None:
        raise RuntimeError(f"Failed to open newly written icon pack {out_path}")
    return pack


async def _ensure_client_jar(version: str, *, force: bool = False) -> Path | None:
    if not version:
        return None
    jar = JAR_DIR / f"{_safe_name(version)}.jar"
    marker = jar.with_suffix(".jar.sha1")
    jar_lock = _get_loop_lock(_JAR_LOCKS, version)
    async with jar_lock:
        if not force and jar.is_file() and marker.is_file():
            return jar
        try:
            meta = await providers.mojang_version(version)
        except Exception:
            meta = None
        download = (meta or {}).get("downloads", {}).get("client") or {}
        url, sha = download.get("url"), download.get("sha1")
        if url:
            if jar.is_file() and (not sha or await asyncio.to_thread(_sha1_file, jar) == sha):
                with contextlib.suppress(OSError):
                    marker.write_text(sha or "ok", encoding="utf-8")
                return jar
            JAR_DIR.mkdir(parents=True, exist_ok=True)
            try:
                await net.download(url, jar, sha1=sha)
            except Exception:
                return jar if jar.is_file() else None
            with contextlib.suppress(OSError):
                marker.write_text(sha or "ok", encoding="utf-8")
            return jar
        if jar.is_file():
            return jar
        return None


def _within_vanilla_cooldown(version: str) -> bool:
    if not version:
        return False
    with _STATE_LOCK:
        ts = _VANILLA_RETRY_AT.get(version)
    return ts is not None and (time.monotonic() - ts) < VANILLA_RETRY_COOLDOWN


async def prepare(inst: "ServerInstance", *, force: bool = False) -> IconPack | None:
    """Ensure a ready-to-serve icon pack exists for the server's current asset signature."""
    sig = signature(inst)
    key = _assets_key(sig)
    version = sig[0]

    pack = None if force else _get_loaded_pack(key)
    if pack is not None and (pack.has_vanilla or not version or _within_vanilla_cooldown(version)):
        return pack

    build_lock = _get_loop_lock(_BUILD_LOCKS, key)
    async with build_lock:
        if not force:
            pack = _get_loaded_pack(key)
            if pack is not None and (pack.has_vanilla or not version or _within_vanilla_cooldown(version)):
                return pack

        jar = await _ensure_client_jar(version, force=force)
        with _STATE_LOCK:
            if jar is None and version:
                _VANILLA_RETRY_AT[version] = time.monotonic()
            elif version:
                _VANILLA_RETRY_AT.pop(version, None)

        if not force and pack is not None and jar is None:
            return pack

        pack = await asyncio.to_thread(_build_pack_sync, inst, sig, key, jar)
        return _cache_pack(key, pack)


async def get_icon(inst: "ServerInstance", item_id: str) -> tuple[bytes, str] | None:
    """Return ``(png_bytes, etag)`` for *item_id*, building the pack once if needed."""
    item_id = str(item_id or "").strip()
    if not item_id or len(item_id) > 128:
        return None
    pack = await prepare(inst)
    if pack is None:
        return None
    return await asyncio.to_thread(pack.get, item_id)


def icon_bytes(inst: "ServerInstance", item_id: str) -> bytes | None:
    """Synchronously read icon PNG bytes if the pack for *inst* is already built."""
    item_id = str(item_id or "").strip()
    if not item_id or len(item_id) > 128:
        return None
    pack = _get_loaded_pack(_assets_key(signature(inst)))
    if pack is None:
        return None
    res = pack.get(item_id)
    return res[0] if res else None
