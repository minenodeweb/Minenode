"""Isometric renderer for Minecraft block models.

The implementation renders a block from its JSON model + textures (the same data the vanilla
client uses for inventory icons), producing a shaded 3D image instead of a flat texture. Assets
are read either from an in-memory archive overlay (:class:`MemoryAssetSource`) or from a list of
asset root directories (:class:`DirectoryAssetSource`), ordered from lowest to highest priority so
mods and resource packs transparently override vanilla assets.

Based on the Minecraft model -> 2D projection approach: 3D vertices of every model element are
rotated into an isometric camera, back-facing sides are culled, the remaining faces are
depth-sorted (painter's algorithm) and each texture is affine-warped onto its quad with directional
shading matching Minecraft's GUI lighting. Resolved models, color-shaded textures, and projected
face geometry + polygon masks are cached so batches of blocks render rapidly across threads.
"""
from __future__ import annotations

import io
import json
import math
import threading
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

# Face brightness multipliers (matching Minecraft GUI lighting)
FACE_SHADING = {
    "up": 1.0,
    "north": 0.8,
    "south": 0.8,
    "east": 0.6,
    "west": 0.6,
    "down": 0.5,
}

# Vertex order for each face: [TL UV, TR UV, BL UV, BR UV]
# Box vertex indices (0..7) as defined in _get_box_vertices
FACE_VERTICES = {
    "north": (1, 0, 5, 4),  # -Z
    "south": (3, 2, 7, 6),  # +Z
    "west": (0, 3, 4, 7),   # -X
    "east": (2, 1, 6, 5),   # +X
    "up": (0, 1, 3, 2),     # +Y
    "down": (7, 6, 4, 5),   # -Y
}

_UNSET = object()
_MISSING = object()
_BUILTIN_PARENTS = ("builtin/", "minecraft:builtin/")

# Vanilla texture subdirectories never used as block/item icons.
_SKIP_VANILLA_TEX_DIRS = frozenset({
    "gui", "font", "painting", "environment", "colormap", "mob_effect",
    "particle", "trims", "models", "effect", "map", "misc", "entity", "atlas",
})

_LUT_LOCK = threading.Lock()
_COLOR_LUTS: dict[tuple[tuple[int, int, int] | None, float], list[int] | None] = {}


def _get_color_lut(tint_rgb: tuple[int, int, int] | None, shade: float) -> list[int] | None:
    """Return a 1024-entry RGBA lookup table combining biome tint and directional shading."""
    if tint_rgb is None and shade >= 1.0:
        return None
    key = (tint_rgb, shade)
    with _LUT_LOCK:
        cached = _COLOR_LUTS.get(key, _UNSET)
        if cached is not _UNSET:
            return cached
        if tint_rgb is not None:
            rt, gt, bt = tint_rgb
            lut = (
                [min(255, max(0, int(int(i * rt / 255) * shade))) for i in range(256)]
                + [min(255, max(0, int(int(i * gt / 255) * shade))) for i in range(256)]
                + [min(255, max(0, int(int(i * bt / 255) * shade))) for i in range(256)]
                + list(range(256))
            )
        else:
            channel = [min(255, max(0, int(i * shade))) for i in range(256)]
            lut = channel * 3 + list(range(256))
        _COLOR_LUTS[key] = lut
        return lut


class MemoryAssetSource:
    """In-memory asset store populated in a single sequential pass over JAR/ZIP archives."""

    def __init__(self) -> None:
        self.models: dict[str, bytes] = {}
        self.textures: dict[str, bytes] = {}
        self.item_defs: dict[str, bytes] = {}
        self.blockstates: dict[str, bytes] = {}
        self.namespaces_with_blockstates: set[str] = set()

    def load_archive(self, path: Path) -> None:
        """Read models, textures, item definitions, and blockstates from *path* once.

        Later archives overwrite earlier ones with the same resource location.
        """
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.replace("\\", "/")
                    if not name.startswith("assets/"):
                        continue
                    parts = name.split("/")
                    if len(parts) < 4 or any(p in ("", ".", "..") for p in parts):
                        continue
                    ns = parts[1]
                    category = parts[2]
                    lower = name.lower()
                    if category == "models" and lower.endswith(".json"):
                        rel = "/".join(parts[3:])[:-5]
                        try:
                            self.models[f"{ns}:{rel}"] = zf.read(info)
                        except (OSError, zipfile.BadZipFile, RuntimeError):
                            continue
                    elif category == "items" and len(parts) == 4 and lower.endswith(".json"):
                        stem = parts[3][:-5]
                        try:
                            self.item_defs[f"{ns}:{stem}"] = zf.read(info)
                        except (OSError, zipfile.BadZipFile, RuntimeError):
                            continue
                    elif category == "blockstates" and len(parts) == 4 and lower.endswith(".json"):
                        stem = parts[3][:-5]
                        try:
                            self.blockstates[f"{ns}:{stem}"] = zf.read(info)
                            self.namespaces_with_blockstates.add(ns)
                        except (OSError, zipfile.BadZipFile, RuntimeError):
                            continue
                    elif category == "textures" and lower.endswith(".png"):
                        if ns == "minecraft" and parts[3] in _SKIP_VANILLA_TEX_DIRS:
                            continue
                        rel = "/".join(parts[3:])[:-4]
                        try:
                            self.textures[f"{ns}:{rel}"] = zf.read(info)
                        except (OSError, zipfile.BadZipFile, RuntimeError):
                            continue
        except (OSError, zipfile.BadZipFile, ValueError):
            return

    def has_model(self, ns: str, path: str) -> bool:
        return f"{ns}:{path}" in self.models

    def read_model(self, ns: str, path: str) -> dict:
        raw = self.models.get(f"{ns}:{path}")
        if raw is None:
            raise FileNotFoundError(f"{ns}:{path}")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid model JSON: {ns}:{path}")
        return data

    def has_texture(self, ns: str, path: str) -> bool:
        return f"{ns}:{path}" in self.textures

    def read_texture_bytes(self, ns: str, path: str) -> bytes | None:
        return self.textures.get(f"{ns}:{path}")


class DirectoryAssetSource:
    """Directory-backed asset store (last root has highest priority)."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots

    def _find(self, *parts: str) -> Path | None:
        rel = Path(*parts)
        for root in reversed(self.roots):
            candidate = root / rel
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def has_model(self, ns: str, path: str) -> bool:
        return self._find(ns, "models", f"{path}.json") is not None

    def read_model(self, ns: str, path: str) -> dict:
        file_path = self._find(ns, "models", f"{path}.json")
        if file_path is None:
            raise FileNotFoundError(f"{ns}:{path}")
        data = json.loads(file_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid model JSON: {ns}:{path}")
        return data

    def has_texture(self, ns: str, path: str) -> bool:
        return self._find(ns, "textures", f"{path}.png") is not None

    def read_texture_bytes(self, ns: str, path: str) -> bytes | None:
        file_path = self._find(ns, "textures", f"{path}.png")
        if file_path is None:
            return None
        try:
            return file_path.read_bytes()
        except OSError:
            return None


class MinecraftBlockRenderer:
    def __init__(self, assets_dir: str | Path | list[str | Path] | MemoryAssetSource):
        """*assets_dir* is a :class:`MemoryAssetSource` or a path (or ordered list of paths)
        containing ``<namespace>/models`` and ``<namespace>/textures``. Later roots override earlier ones.
        """
        if isinstance(assets_dir, MemoryAssetSource):
            self._source: MemoryAssetSource | DirectoryAssetSource = assets_dir
            self.assets_dirs: list[Path] = []
            self.assets_dir = Path(".")
        else:
            if isinstance(assets_dir, (str, Path)):
                assets_dir = [assets_dir]
            self.assets_dirs = [Path(p) for p in assets_dir]
            self.assets_dir = self.assets_dirs[0] if self.assets_dirs else Path(".")
            self._source = DirectoryAssetSource(self.assets_dirs)

        self._lock = threading.Lock()
        self._raw_model_cache: dict[str, dict | object] = {}
        self._resolved_cache: dict[str, dict] = {}
        self._base_tex_cache: dict[str, tuple[Image.Image, str]] = {}
        self._shaded_tex_cache: dict[tuple[str, tuple[int, int, int] | None, float], tuple[Image.Image, str]] = {}
        self._geom_cache: dict[tuple[Any, ...], list[tuple]] = {}

    @staticmethod
    def _strip_namespace(res_loc: str) -> tuple[str, str]:
        if ":" in res_loc:
            ns, path = res_loc.split(":", 1)
            return ns, path
        return "minecraft", res_loc

    @staticmethod
    def _is_builtin(model_loc: str) -> bool:
        return model_loc.startswith(_BUILTIN_PARENTS)

    def has_model(self, model_loc: str) -> bool:
        ns, path = self._strip_namespace(model_loc)
        return self._source.has_model(ns, path)

    def load_model_json(self, model_loc: str) -> dict:
        with self._lock:
            cached = self._raw_model_cache.get(model_loc, _UNSET)
        if cached is _MISSING:
            raise FileNotFoundError(model_loc)
        if cached is not _UNSET:
            return cached  # type: ignore[return-value]

        ns, path = self._strip_namespace(model_loc)
        try:
            data = self._source.read_model(ns, path)
        except FileNotFoundError:
            with self._lock:
                self._raw_model_cache.setdefault(model_loc, _MISSING)
            raise
        with self._lock:
            return self._raw_model_cache.setdefault(model_loc, data)  # type: ignore[return-value]

    def resolve_model(self, model_loc: str) -> dict:
        """Recursively merge parent chain, texture references, and model elements (cached)."""
        with self._lock:
            cached = self._resolved_cache.get(model_loc)
        if cached is not None:
            return cached

        chain: list[dict] = []
        curr: str | None = model_loc
        visited: set[str] = set()
        while isinstance(curr, str) and curr and curr not in visited:
            if self._is_builtin(curr):
                break
            visited.add(curr)
            data = self.load_model_json(curr)
            chain.append(data)
            curr = data.get("parent")

        chain.reverse()

        merged_textures: dict[str, Any] = {}
        elements: list[dict] = []
        for node in chain:
            textures = node.get("textures")
            if isinstance(textures, dict):
                merged_textures.update(textures)
            if isinstance(node.get("elements"), list):
                elements = node["elements"]

        resolved_textures: dict[str, Any] = {}
        for key, val in merged_textures.items():
            target = val
            seen: set[str] = set()
            while isinstance(target, str) and target.startswith("#"):
                ref_key = target[1:]
                if ref_key in seen or ref_key not in merged_textures:
                    break
                seen.add(ref_key)
                target = merged_textures[ref_key]
            resolved_textures[key] = target

        result = {"textures": resolved_textures, "elements": elements}
        with self._lock:
            return self._resolved_cache.setdefault(model_loc, result)

    def has_concrete_textures(self, model_loc: str, *, require_existing: bool = True) -> bool:
        """True if *model_loc* has geometry and at least one resolved, non-missing texture."""
        try:
            model = self.resolve_model(model_loc)
        except (FileNotFoundError, ValueError, OSError, TypeError):
            return False
        elements = model.get("elements")
        if not elements:
            return False
        textures_map = model.get("textures") or {}
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            faces = elem.get("faces")
            if not isinstance(faces, dict):
                continue
            for face_data in faces.values():
                if not isinstance(face_data, dict):
                    continue
                tex_ref = face_data.get("texture", "")
                if not isinstance(tex_ref, str) or not tex_ref:
                    continue
                tex_loc = textures_map.get(tex_ref[1:]) if tex_ref.startswith("#") else tex_ref
                if not isinstance(tex_loc, str) or not tex_loc or tex_loc.startswith("#"):
                    continue
                if not require_existing or self._source.has_texture(*self._strip_namespace(tex_loc)):
                    return True
        return False

    @staticmethod
    def _missing_texture() -> Image.Image:
        """Graceful magenta placeholder when a mod references a missing texture."""
        img = Image.new("RGBA", (16, 16), (255, 0, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 15, 15), outline=(0, 0, 0, 255))
        img.load()
        return img

    @staticmethod
    def _classify_alpha(img: Image.Image) -> str:
        alpha = img.getchannel("A")
        a_min, _ = alpha.getextrema()
        if a_min == 255:
            return "opaque"
        if sum(alpha.histogram()[1:255]) == 0:
            return "cutout"
        return "translucent"

    def _get_base_texture(self, tex_loc: str) -> tuple[Image.Image, str]:
        with self._lock:
            cached = self._base_tex_cache.get(tex_loc)
        if cached is not None:
            return cached

        ns, path = self._strip_namespace(tex_loc)
        raw = self._source.read_texture_bytes(ns, path)
        if raw is None:
            img = self._missing_texture()
            entry = (img, "opaque")
        else:
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGBA")
                w, h = img.size
                if h > w > 0:
                    img = img.crop((0, 0, w, w))
                img.load()
                entry = (img, self._classify_alpha(img))
            except (OSError, ValueError):
                img = self._missing_texture()
                entry = (img, "opaque")

        with self._lock:
            return self._base_tex_cache.setdefault(tex_loc, entry)

    def _get_texture_entry(
        self,
        tex_loc: str,
        tint_rgb: tuple[int, int, int] | None,
        shade: float,
    ) -> tuple[Image.Image, str]:
        if tint_rgb is None and shade >= 1.0:
            return self._get_base_texture(tex_loc)

        key = (tex_loc, tint_rgb, shade)
        with self._lock:
            cached = self._shaded_tex_cache.get(key)
        if cached is not None:
            return cached

        base_img, alpha_mode = self._get_base_texture(tex_loc)
        lut = _get_color_lut(tint_rgb, shade)
        shaded = base_img.point(lut) if lut is not None else base_img
        shaded.load()
        entry = (shaded, alpha_mode)
        with self._lock:
            return self._shaded_tex_cache.setdefault(key, entry)

    def get_texture(self, tex_loc: str, tint_rgb: tuple[int, int, int] | None = None) -> Image.Image:
        """Load a PNG texture (first frame if animated strip) with optional biome tint."""
        return self._get_texture_entry(tex_loc, tint_rgb, 1.0)[0]

    @staticmethod
    def _rotate_point(pt, axis: str, angle_deg: float, origin) -> tuple[float, float, float]:
        """Rotate a 3D point around *origin* and *axis* ('x', 'y', 'z')."""
        x, y, z = pt[0] - origin[0], pt[1] - origin[1], pt[2] - origin[2]
        rad = math.radians(angle_deg)
        c, s = math.cos(rad), math.sin(rad)

        if axis == "x":
            y, z = y * c - z * s, y * s + z * c
        elif axis == "y":
            x, z = x * c + z * s, -x * s + z * c
        elif axis == "z":
            x, y = x * c - y * s, x * s + y * c

        return (x + origin[0], y + origin[1], z + origin[2])

    def _get_box_vertices(self, elem: dict) -> list[tuple[float, float, float]]:
        """Return the 8 box vertices after applying the element's local rotation."""
        x1, y1, z1 = elem["from"]
        x2, y2, z2 = elem["to"]

        verts = [
            (x1, y2, z1),  # 0: minX, maxY, minZ
            (x2, y2, z1),  # 1: maxX, maxY, minZ
            (x2, y2, z2),  # 2: maxX, maxY, maxZ
            (x1, y2, z2),  # 3: minX, maxY, maxZ
            (x1, y1, z1),  # 4: minX, minY, minZ
            (x2, y1, z1),  # 5: maxX, minY, minZ
            (x2, y1, z2),  # 6: maxX, minY, maxZ
            (x1, y1, z2),  # 7: minX, minY, maxZ
        ]

        if "rotation" in elem:
            rot = elem["rotation"]
            origin = tuple(rot["origin"])
            axis = rot["axis"]
            angle = rot["angle"]
            verts = [self._rotate_point(v, axis, angle, origin) for v in verts]

        return verts

    def _project_to_camera(
        self,
        pt: tuple[float, float, float],
        rot_y: float = 45.0,
        rot_x: float = 30.0,
        block_rot_y: float = 0.0,
        block_rot_x: float = 0.0,
    ) -> tuple[float, float, float]:
        """Transform a [0..16] world point into camera space (screen_x, screen_y, depth)."""
        center = (8.0, 8.0, 8.0)

        if block_rot_x:
            pt = self._rotate_point(pt, "x", -block_rot_x, center)
        if block_rot_y:
            pt = self._rotate_point(pt, "y", -block_rot_y, center)

        pt = self._rotate_point(pt, "y", rot_y, center)
        pt = self._rotate_point(pt, "x", rot_x, center)

        return (pt[0] - 8.0, -(pt[1] - 8.0), pt[2] - 8.0)

    @staticmethod
    def _solve_affine(
        src_pts: tuple[tuple[float, float], tuple[float, float], tuple[float, float]],
        dst_pts: tuple[tuple[float, float], tuple[float, float], tuple[float, float]],
    ) -> tuple[float, float, float, float, float, float]:
        """Compute the 6-parameter PIL.Image.Transform.AFFINE matrix mapping dst -> src."""
        (x0, y0), (x1, y1), (x2, y2) = dst_pts
        (u0, v0), (u1, v1), (u2, v2) = src_pts

        det = (x0 - x2) * (y1 - y2) - (x1 - x2) * (y0 - y2)
        if abs(det) < 1e-8:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

        a = ((u0 - u2) * (y1 - y2) - (u1 - u2) * (y0 - y2)) / det
        b = ((u1 - u2) * (x0 - x2) - (u0 - u2) * (x1 - x2)) / det
        c = u2 - a * x2 - b * y2

        d = ((v0 - v2) * (y1 - y2) - (v1 - v2) * (y0 - y2)) / det
        e = ((v1 - v2) * (x0 - x2) - (v0 - v2) * (x1 - x2)) / det
        f = v2 - d * x2 - e * y2

        return (a, b, c, d, e, f)

    @staticmethod
    def _default_uv(elem: dict, face_name: str) -> tuple[float, float, float, float]:
        x1, y1, z1 = elem["from"]
        x2, y2, z2 = elem["to"]
        if face_name in ("north", "south"):
            return (float(x1), float(16 - y2), float(x2), float(16 - y1))
        if face_name in ("east", "west"):
            return (float(z1), float(16 - y2), float(z2), float(16 - y1))
        return (float(x1), float(z1), float(x2), float(z2))

    def _geometry_key(
        self,
        elements: list[dict],
        size: int,
        rot_y: float,
        rot_x: float,
        block_rot_y: float,
        block_rot_x: float,
    ) -> tuple[Any, ...]:
        elem_specs = []
        for elem in elements:
            frm = tuple(float(v) for v in elem["from"])
            to = tuple(float(v) for v in elem["to"])
            rot = elem.get("rotation")
            rot_spec = (
                (tuple(float(v) for v in rot["origin"]), rot["axis"], float(rot["angle"]))
                if rot else None
            )
            face_specs = []
            for face_name, face_data in (elem.get("faces") or {}).items():
                if face_name not in FACE_VERTICES or not isinstance(face_data, dict):
                    continue
                raw_uv = face_data.get("uv")
                uv = (
                    tuple(float(c) for c in raw_uv)
                    if raw_uv is not None
                    else self._default_uv(elem, face_name)
                )
                uv_rot = int(face_data.get("rotation", 0) or 0)
                face_specs.append((face_name, uv, uv_rot))
            elem_specs.append((frm, to, rot_spec, tuple(face_specs)))
        return (tuple(elem_specs), size, rot_y, rot_x, block_rot_y, block_rot_x)

    def _get_face_plan(
        self,
        elements: list[dict],
        size: int,
        rot_y: float,
        rot_x: float,
        block_rot_y: float,
        block_rot_x: float,
    ) -> list[tuple]:
        """Compute (and cache) depth-sorted face transforms and polygon masks in 0..16 UV space."""
        key = self._geometry_key(elements, size, rot_y, rot_x, block_rot_y, block_rot_x)
        with self._lock:
            cached = self._geom_cache.get(key)
        if cached is not None:
            return cached

        scale = size / 28.0
        offset = size / 2.0
        planned: list[tuple] = []

        for elem_idx, elem in enumerate(elements):
            verts_3d = self._get_box_vertices(elem)
            proj = [
                self._project_to_camera(v, rot_y, rot_x, block_rot_y, block_rot_x)
                for v in verts_3d
            ]
            for face_name, face_data in (elem.get("faces") or {}).items():
                if face_name not in FACE_VERTICES or not isinstance(face_data, dict):
                    continue
                idx_tl, idx_tr, idx_bl, idx_br = FACE_VERTICES[face_name]
                p_tl, p_tr, p_bl, p_br = proj[idx_tl], proj[idx_tr], proj[idx_bl], proj[idx_br]

                ux, uy = p_tr[0] - p_tl[0], p_tr[1] - p_tl[1]
                vx, vy = p_bl[0] - p_tl[0], p_bl[1] - p_tl[1]
                if ux * vy - uy * vx <= 0:
                    continue

                avg_depth = (p_tl[2] + p_tr[2] + p_bl[2] + p_br[2]) / 4.0

                screen_pts = (
                    (p_tl[0] * scale + offset, p_tl[1] * scale + offset),
                    (p_tr[0] * scale + offset, p_tr[1] * scale + offset),
                    (p_bl[0] * scale + offset, p_bl[1] * scale + offset),
                    (p_br[0] * scale + offset, p_br[1] * scale + offset),
                )
                xs = (screen_pts[0][0], screen_pts[1][0], screen_pts[2][0], screen_pts[3][0])
                ys = (screen_pts[0][1], screen_pts[1][1], screen_pts[2][1], screen_pts[3][1])
                bx0 = max(0, math.floor(min(xs)))
                by0 = max(0, math.floor(min(ys)))
                bx1 = min(size, math.ceil(max(xs)) + 1)
                by1 = min(size, math.ceil(max(ys)) + 1)
                bw = bx1 - bx0
                bh = by1 - by0
                if bw <= 0 or bh <= 0:
                    continue

                local_pts = [(x - bx0, y - by0) for x, y in screen_pts]

                raw_uv = face_data.get("uv")
                u1, v1, u2, v2 = raw_uv if raw_uv is not None else self._default_uv(elem, face_name)
                uv_corners = [(u1, v1), (u2, v1), (u1, v2), (u2, v2)]
                uv_rot = face_data.get("rotation", 0)
                if uv_rot == 90:
                    uv_corners = [uv_corners[2], uv_corners[0], uv_corners[3], uv_corners[1]]
                elif uv_rot == 180:
                    uv_corners = [uv_corners[3], uv_corners[2], uv_corners[1], uv_corners[0]]
                elif uv_rot == 270:
                    uv_corners = [uv_corners[1], uv_corners[3], uv_corners[0], uv_corners[2]]

                mat16 = self._solve_affine(tuple(uv_corners[:3]), tuple(local_pts[:3]))

                mask = Image.new("L", (bw, bh), 0)
                ImageDraw.Draw(mask).polygon(
                    [local_pts[0], local_pts[1], local_pts[3], local_pts[2]], fill=255
                )
                mask.load()

                shade = FACE_SHADING.get(face_name, 1.0)
                planned.append((avg_depth, elem_idx, face_name, shade, bx0, by0, bw, bh, mat16, mask))

        planned.sort(key=lambda item: item[0])
        compact = [item[1:] for item in planned]
        with self._lock:
            return self._geom_cache.setdefault(key, compact)

    def render(
        self,
        model_loc: str,
        size: int = 256,
        rot_y: float = -45.0,
        rot_x: float = 30.0,
        block_rot_y: float = 0.0,
        block_rot_x: float = 0.0,
        tint_rgb: tuple[int, int, int] | None = None,
    ) -> Image.Image:
        """Render a block model to an RGBA image of *size* x *size* pixels."""
        model = self.resolve_model(model_loc)
        textures_map = model["textures"]
        elements = model["elements"]

        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        if not elements:
            return canvas

        plan = self._get_face_plan(elements, size, rot_y, rot_x, block_rot_y, block_rot_x)
        if not plan:
            return canvas

        for elem_idx, face_name, shade, bx0, by0, bw, bh, mat16, mask in plan:
            face_data = elements[elem_idx]["faces"][face_name]
            tex_ref = face_data.get("texture", "")
            if tex_ref.startswith("#"):
                tex_loc = textures_map.get(tex_ref[1:])
            else:
                tex_loc = tex_ref
            if not tex_loc:
                continue

            use_tint = tint_rgb if "tintindex" in face_data else None
            tex, alpha_mode = self._get_texture_entry(tex_loc, use_tint, shade)
            tw, th = tex.size
            if tw == 16 and th == 16:
                mat = mat16
            else:
                sx, sy = tw / 16.0, th / 16.0
                mat = (
                    mat16[0] * sx, mat16[1] * sx, mat16[2] * sx,
                    mat16[3] * sy, mat16[4] * sy, mat16[5] * sy,
                )

            warped = tex.transform(
                (bw, bh),
                Image.Transform.AFFINE,
                mat,
                resample=Image.Resampling.NEAREST,
            )

            if alpha_mode == "opaque":
                canvas.paste(warped, (bx0, by0), mask)
            elif alpha_mode == "cutout":
                final_mask = ImageChops.multiply(warped.getchannel("A"), mask)
                canvas.paste(warped, (bx0, by0), final_mask)
            else:
                final_alpha = ImageChops.multiply(warped.getchannel("A"), mask)
                warped.putalpha(final_alpha)
                sub = canvas.crop((bx0, by0, bx0 + bw, by0 + bh))
                canvas.paste(Image.alpha_composite(sub, warped), (bx0, by0))

        return canvas

    def render_icon_png(
        self,
        model_loc: str,
        *,
        size: int = 128,
        rot_y: float = -45.0,
        rot_x: float = 30.0,
        tint_rgb: tuple[int, int, int] | None = None,
        compress_level: int = 1,
    ) -> bytes | None:
        """Render *model_loc*, crop transparent margins to a square, and encode as PNG bytes."""
        img = self.render(model_loc, size=size, rot_y=rot_y, rot_x=rot_x, tint_rgb=tint_rgb)
        bbox = img.getbbox()
        if not bbox:
            return None
        cropped = img.crop(bbox)
        w, h = cropped.size
        side = max(w, h)
        if side != w or side != h:
            square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            square.paste(cropped, ((side - w) // 2, (side - h) // 2))
            cropped = square
        buf = io.BytesIO()
        cropped.save(buf, format="PNG", compress_level=compress_level)
        return buf.getvalue()
