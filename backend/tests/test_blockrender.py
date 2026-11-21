import io
import json
import zipfile

from PIL import Image

from app.blockrender import MemoryAssetSource, MinecraftBlockRenderer

MODEL = {
    "textures": {"all": "minecraft:block/test_block"},
    "elements": [{
        "from": [0, 0, 0],
        "to": [16, 16, 16],
        "faces": {
            face: {"texture": "#all", "uv": [0, 0, 16, 16]}
            for face in ("up", "down", "north", "south", "east", "west")
        },
    }],
}


def _assets(root, color=(180, 150, 120, 255), *, model=MODEL):
    (root / "minecraft" / "models" / "block").mkdir(parents=True, exist_ok=True)
    (root / "minecraft" / "textures" / "block").mkdir(parents=True, exist_ok=True)
    (root / "minecraft" / "models" / "block" / "test_block.json").write_text(
        json.dumps(model), encoding="utf-8"
    )
    Image.new("RGBA", (16, 16), color).save(
        root / "minecraft" / "textures" / "block" / "test_block.png"
    )
    return root


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _zip(path, entries: dict[str, bytes | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_renders_shaded_isometric_cube(tmp_path):
    renderer = MinecraftBlockRenderer(_assets(tmp_path / "assets"))
    img = renderer.render("minecraft:block/test_block", size=128)

    assert img.size == (128, 128)
    assert img.getchannel("A").getbbox() is not None  # something was actually drawn
    # Top face is the brightest one; with a uniform texture its pixels keep the source colour.
    assert img.getpixel((64, 64))[3] > 0


def test_resolves_parent_chain_and_tint(tmp_path):
    root = _assets(tmp_path / "assets")
    (root / "minecraft" / "models" / "block" / "tinted.json").write_text(json.dumps({
        "parent": "minecraft:block/test_block",
        "textures": {"all": "#base"},
    }), encoding="utf-8")
    renderer = MinecraftBlockRenderer(root)

    plain = renderer.render("minecraft:block/test_block", size=64)
    tinted = renderer.render("minecraft:block/tinted", size=64, tint_rgb=(255, 0, 0))
    assert plain.size == tinted.size == (64, 64)
    assert tinted.getchannel("A").getbbox() is not None


def test_later_asset_root_overrides_textures(tmp_path):
    base = _assets(tmp_path / "base")
    override = _assets(tmp_path / "override", color=(255, 0, 0, 255))

    renderer = MinecraftBlockRenderer([base, override])
    pixel = renderer.get_texture("minecraft:block/test_block").getpixel((0, 0))
    assert pixel == (255, 0, 0, 255)


def test_missing_texture_does_not_crash(tmp_path):
    root = _assets(tmp_path / "assets")
    (root / "minecraft" / "models" / "block" / "test_block.json").write_text(json.dumps({
        "textures": {"all": "minecraft:block/does_not_exist"},
        "elements": MODEL["elements"],
    }), encoding="utf-8")
    renderer = MinecraftBlockRenderer(root)

    assert renderer.get_texture("minecraft:block/does_not_exist").size == (16, 16)
    assert renderer.render("minecraft:block/test_block", size=64).size == (64, 64)


def test_memory_asset_source_overlay_precedence_and_zip_slip(tmp_path):
    base_jar = tmp_path / "base.jar"
    pack_zip = tmp_path / "pack.zip"
    _zip(base_jar, {
        "assets/minecraft/models/block/stone.json": json.dumps(MODEL),
        "assets/minecraft/textures/block/test_block.png": _png_bytes(Image.new("RGBA", (16, 16), (10, 20, 30, 255))),
        "assets/minecraft/textures/gui/title.png": _png_bytes(Image.new("RGBA", (16, 16), (1, 2, 3, 255))),
        "assets/minecraft/../evil.json": "{}",
    })
    _zip(pack_zip, {
        "assets/minecraft/textures/block/test_block.png": _png_bytes(Image.new("RGBA", (16, 16), (220, 30, 40, 255))),
    })

    source = MemoryAssetSource()
    source.load_archive(base_jar)
    source.load_archive(pack_zip)

    assert not source.has_texture("minecraft", "gui/title")
    assert all(".." not in k for k in source.models)
    renderer = MinecraftBlockRenderer(source)
    assert renderer.get_texture("minecraft:block/test_block").getpixel((0, 0)) == (220, 30, 40, 255)
    assert renderer.has_concrete_textures("minecraft:block/stone") is True


def test_cutout_translucent_rotated_and_uv_rotations(tmp_path):
    cutout_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for y in range(4, 12):
        for x in range(4, 12):
            cutout_img.putpixel((x, y), (40, 180, 90, 255))

    translucent_img = Image.new("RGBA", (16, 16), (50, 110, 220, 128))
    arrow_img = Image.new("RGBA", (16, 16), (20, 20, 20, 255))
    for x in range(8):
        arrow_img.putpixel((x, 0), (255, 255, 0, 255))

    source = MemoryAssetSource()
    jar = tmp_path / "assets.jar"
    _zip(jar, {
        "assets/minecraft/textures/block/cutout.png": _png_bytes(cutout_img),
        "assets/minecraft/textures/block/glass.png": _png_bytes(translucent_img),
        "assets/minecraft/textures/block/arrow.png": _png_bytes(arrow_img),
        "assets/minecraft/models/block/cutout.json": json.dumps({
            "textures": {"all": "minecraft:block/cutout"},
            "elements": MODEL["elements"],
        }),
        "assets/minecraft/models/block/glass.json": json.dumps({
            "textures": {"all": "minecraft:block/glass"},
            "elements": MODEL["elements"],
        }),
        "assets/minecraft/models/block/rotated.json": json.dumps({
            "textures": {"all": "minecraft:block/arrow"},
            "elements": [{
                "from": [6, 0, 6],
                "to": [10, 14, 10],
                "rotation": {"origin": [8, 8, 8], "axis": "z", "angle": 22.5},
                "faces": {
                    "up": {"texture": "#all", "uv": [0, 0, 16, 16], "rotation": 90},
                    "north": {"texture": "#all", "uv": [0, 0, 16, 16], "rotation": 180},
                    "east": {"texture": "#all", "uv": [0, 0, 16, 16], "rotation": 270},
                },
            }],
        }),
        "assets/minecraft/models/block/empty.json": json.dumps({
            "textures": {"particle": "minecraft:block/cutout"},
        }),
    })
    source.load_archive(jar)
    renderer = MinecraftBlockRenderer(source)

    cut = renderer.render("minecraft:block/cutout", size=64)
    assert cut.getchannel("A").getbbox() is not None
    assert cut.getpixel((0, 0))[3] == 0

    glass = renderer.render("minecraft:block/glass", size=64)
    a_min, a_max = glass.getchannel("A").getextrema()
    assert a_min == 0 and 0 < a_max < 255

    rot = renderer.render("minecraft:block/rotated", size=64)
    assert rot.getchannel("A").getbbox() is not None

    # render_icon_png produces square cropped PNG for blocks and None for empty elements
    png_data = renderer.render_icon_png("minecraft:block/rotated", size=64)
    assert png_data is not None
    with Image.open(io.BytesIO(png_data)) as out:
        assert out.size[0] == out.size[1] > 0

    assert renderer.render_icon_png("minecraft:block/empty", size=64) is None
    assert renderer.has_concrete_textures("minecraft:block/empty") is False


def test_model_resolution_and_geometry_plan_are_cached(tmp_path):
    root = _assets(tmp_path / "assets")
    (root / "minecraft" / "models" / "block" / "child.json").write_text(json.dumps({
        "parent": "minecraft:block/test_block",
        "textures": {"all": "minecraft:block/test_block"},
    }), encoding="utf-8")
    (root / "minecraft" / "textures" / "block" / "alt.png").write_bytes(
        _png_bytes(Image.new("RGBA", (16, 16), (90, 200, 120, 255)))
    )
    (root / "minecraft" / "models" / "block" / "alt_cube.json").write_text(json.dumps({
        "parent": "minecraft:block/test_block",
        "textures": {"all": "minecraft:block/alt"},
    }), encoding="utf-8")

    renderer = MinecraftBlockRenderer(root)
    renderer.render("minecraft:block/child", size=64)
    renderer.render("minecraft:block/alt_cube", size=64)

    # Identical cube geometry across different models shares one cached face plan.
    assert len(renderer._geom_cache) == 1
    # Calling resolve_model again returns the cached dictionary without disk I/O.
    assert renderer.resolve_model("minecraft:block/child") is renderer.resolve_model("minecraft:block/child")
