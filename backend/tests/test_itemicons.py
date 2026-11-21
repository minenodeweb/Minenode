import asyncio
import io
import json
import random
import time
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from app import itemicons
from app.models import PublicPageSettings

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _inst(tmp_path, *, sid="srv"):
    mods = tmp_path / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    rp = tmp_path / ".minenode" / "resourcepacks"
    rp.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        dir=tmp_path,
        cfg=SimpleNamespace(id=sid, type="fabric", mc_version="1.21.4", public_page=PublicPageSettings()),
    )


BLOCK_MODEL = json.dumps({
    "textures": {"all": "minecraft:block/stone"},
    "elements": [{
        "from": [0, 0, 0], "to": [16, 16, 16],
        "faces": {
            face: {"texture": "#all", "uv": [0, 0, 16, 16]}
            for face in ("up", "down", "north", "south", "east", "west")
        },
    }],
})


def _tinted_model(tex="minecraft:block/tinted_leaf"):
    return json.dumps({
        "textures": {"all": tex},
        "elements": [{
            "from": [0, 0, 0], "to": [16, 16, 16],
            "faces": {
                face: {"texture": "#all", "uv": [0, 0, 16, 16], "tintindex": 0}
                for face in ("up", "down", "north", "south", "east", "west")
            },
        }],
    })


def _png_bytes(color=(180, 150, 120, 255), size=(16, 16)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jar(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _client_jar(version):
    path = itemicons.JAR_DIR / f"{itemicons._safe_name(version)}.jar"
    _jar(path, {
        "assets/minecraft/textures/item/diamond_pickaxe.png": _png_bytes((0, 220, 220, 255)),
        "assets/minecraft/textures/block/stone.png": _png_bytes((180, 150, 120, 255)),
        "assets/minecraft/models/block/stone.json": BLOCK_MODEL,
        "assets/minecraft/textures/block/grass_block_top.png": PNG,
        "assets/minecraft/textures/block/cobblestone.png": PNG,
        "assets/minecraft/textures/block/cherry_planks.png": PNG,
        "assets/minecraft/textures/item/zombie_spawn_egg.png": PNG,
    })
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep the real cache untouched; use a temporary icon root and fake Mojang metadata."""
    monkeypatch.setattr(itemicons, "ROOT", tmp_path / "icons")
    monkeypatch.setattr(itemicons, "JAR_DIR", tmp_path / "icons" / "client")
    monkeypatch.setattr(itemicons, "PACKS_DIR", tmp_path / "icons" / "packs")
    itemicons.clear_state()
    inst = _inst(tmp_path)

    async def fake_version(version):
        return {"downloads": {"client": {"url": "https://example.invalid/client.jar"}}}

    async def fake_download(url, dest, **kwargs):
        dest.write_bytes(PNG)

    monkeypatch.setattr(itemicons.providers, "mojang_version", fake_version)
    monkeypatch.setattr(itemicons.net, "download", fake_download)
    yield inst
    itemicons.clear_state()


def test_single_pass_extraction_and_icon_resolution(_isolate, monkeypatch):
    inst = _isolate
    jar = _client_jar(inst.cfg.mc_version)

    opened = []
    real_zip = itemicons.blockrender.zipfile.ZipFile

    class CountingZip(real_zip):
        def __init__(self, file, *args, **kwargs):
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(itemicons.blockrender.zipfile, "ZipFile", CountingZip)
    pack = asyncio.run(itemicons.prepare(inst))

    assert pack is not None
    assert opened == [str(jar)]  # archive opened exactly once
    assert list(itemicons.PACKS_DIR.glob("*.mnpk")) == [pack.path]
    for legacy in ("assets", "overlays", "png", "blocks", "index"):
        assert not (itemicons.ROOT / legacy).exists()

    # Blocks are rendered as isometric PNGs; flat items & aliases fall back to flat sprites.
    stone_res = asyncio.run(itemicons.get_icon(inst, "minecraft:stone"))
    assert stone_res is not None
    with Image.open(io.BytesIO(stone_res[0])) as im:
        assert im.size[0] == im.size[1] > 16

    assert itemicons.icon_bytes(inst, "minecraft:diamond_pickaxe") is not None
    assert itemicons.icon_bytes(inst, "minecraft:grass_block") is not None
    assert itemicons.icon_bytes(inst, "minecraft:stone_slab") is not None
    assert itemicons.icon_bytes(inst, "minecraft:cobblestone_slab") is not None
    assert itemicons.icon_bytes(inst, "minecraft:cherry_fence") is not None
    assert itemicons.icon_bytes(inst, "minecraft:zombie") is not None
    assert itemicons.icon_bytes(inst, "minecraft:unknown") is None


def test_mod_and_resource_pack_override_blocks_and_items(_isolate):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)
    asyncio.run(itemicons.prepare(inst))
    vanilla_stone = itemicons.icon_bytes(inst, "minecraft:stone")
    vanilla_pick = itemicons.icon_bytes(inst, "minecraft:diamond_pickaxe")

    _jar(inst.dir / "mods" / "create.jar", {
        "assets/create/textures/item/andesite_alloy.png": _png_bytes((150, 160, 155, 255)),
    })
    _jar(inst.dir / ".minenode" / "resourcepacks" / "pack.zip", {
        "assets/minecraft/textures/block/stone.png": _png_bytes((255, 0, 0, 255)),
        "assets/minecraft/textures/item/diamond_pickaxe.png": _png_bytes((0, 255, 0, 255)),
    })

    pack = asyncio.run(itemicons.prepare(inst))
    assert pack is not None
    assert itemicons.icon_bytes(inst, "create:andesite_alloy") is not None

    overridden_stone = itemicons.icon_bytes(inst, "minecraft:stone")
    overridden_pick = itemicons.icon_bytes(inst, "minecraft:diamond_pickaxe")
    assert overridden_stone is not None and overridden_stone != vanilla_stone
    assert overridden_pick is not None and overridden_pick != vanilla_pick
    with Image.open(io.BytesIO(overridden_pick)) as im:
        assert im.getpixel((0, 0)) == (0, 255, 0, 255)


def test_pack_rebuilt_when_mods_change(_isolate):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)
    first_pack = asyncio.run(itemicons.prepare(inst))
    assert itemicons.icon_bytes(inst, "create:andesite_alloy") is None

    _jar(inst.dir / "mods" / "create.jar", {
        "assets/create/textures/item/andesite_alloy.png": PNG,
    })
    second_pack = asyncio.run(itemicons.prepare(inst))
    assert second_pack is not None and second_pack.key != first_pack.key
    assert itemicons.icon_bytes(inst, "create:andesite_alloy") is not None


def test_malformed_and_unknown_ids_return_none_without_opening_archives(_isolate, monkeypatch):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)

    def fail_zip(*args, **kwargs):
        raise AssertionError("ZipFile must not be opened for malformed IDs")

    monkeypatch.setattr(itemicons.blockrender.zipfile, "ZipFile", fail_zip)
    assert asyncio.run(itemicons.get_icon(inst, "")) is None
    assert asyncio.run(itemicons.get_icon(inst, "x" * 200)) is None

    monkeypatch.undo()
    asyncio.run(itemicons.prepare(inst))
    assert asyncio.run(itemicons.get_icon(inst, "minecraft:does_not_exist")) is None


def test_shared_pack_and_cold_start_reload_without_rendering(_isolate, monkeypatch):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)
    pack = asyncio.run(itemicons.prepare(inst))
    assert pack is not None
    expected = itemicons.icon_bytes(inst, "minecraft:stone")

    # Second server with identical mods/packs/version shares the exact .mnpk cache.
    other = _inst(inst.dir.parent / "other-server", sid="other")
    assert itemicons.icon_bytes(other, "minecraft:stone") == expected

    # Simulate panel restart: clear in-memory packs and block any rendering or archive opens.
    itemicons.clear_state()

    def forbid(*args, **kwargs):
        raise AssertionError("Cold-start reload must read directly from .mnpk without rendering or unzipping")

    monkeypatch.setattr(itemicons.blockrender.MinecraftBlockRenderer, "render", forbid)
    monkeypatch.setattr(itemicons.blockrender.zipfile, "ZipFile", forbid)

    reloaded = asyncio.run(itemicons.get_icon(other, "minecraft:stone"))
    assert reloaded is not None and reloaded[0] == expected


def test_blockstates_filtering_items_defs_and_constant_tints(_isolate):
    inst = _isolate
    jar = itemicons.JAR_DIR / f"{itemicons._safe_name(inst.cfg.mc_version)}.jar"
    _jar(jar, {
        "assets/minecraft/textures/block/oak_planks.png": _png_bytes((160, 120, 70, 255)),
        "assets/minecraft/textures/block/tinted_leaf.png": _png_bytes((200, 200, 200, 255)),
        "assets/minecraft/textures/block/wheat_stage7.png": _png_bytes((220, 200, 60, 255)),
        "assets/minecraft/models/block/cube_all.json": json.dumps({
            "textures": {"all": "minecraft:block/oak_planks"},
            "elements": json.loads(BLOCK_MODEL)["elements"],
        }),
        "assets/minecraft/models/block/template_fence.json": json.dumps({
            "textures": {"all": "minecraft:block/oak_planks"},
            "elements": json.loads(BLOCK_MODEL)["elements"],
        }),
        "assets/minecraft/models/block/oak_stairs.json": json.dumps({
            "parent": "minecraft:block/cube_all",
        }),
        "assets/minecraft/models/block/oak_stairs_inner.json": json.dumps({
            "parent": "minecraft:block/cube_all",
        }),
        "assets/minecraft/models/block/custom_door_model.json": json.dumps({
            "parent": "minecraft:block/cube_all",
        }),
        "assets/minecraft/models/block/tinted_leaf.json": _tinted_model(),
        "assets/minecraft/models/block/wheat_stage7.json": json.dumps({
            "textures": {"all": "minecraft:block/wheat_stage7"},
            "elements": json.loads(BLOCK_MODEL)["elements"],
        }),
        "assets/minecraft/models/item/custom_door.json": json.dumps({
            "parent": "minecraft:block/custom_door_model",
        }),
        "assets/minecraft/blockstates/oak_stairs.json": json.dumps({
            "variants": {"": {"model": "minecraft:block/oak_stairs"}},
        }),
        "assets/minecraft/blockstates/wheat.json": json.dumps({
            "variants": {"age=0": {"model": "minecraft:block/missing"}, "age=7": {"model": "minecraft:block/wheat_stage7"}},
        }),
        "assets/minecraft/items/tinted_leaf.json": json.dumps({
            "model": {
                "type": "minecraft:model",
                "model": "minecraft:block/tinted_leaf",
                "tints": [{"type": "minecraft:constant", "value": [255, 0, 0]}],
            },
        }),
    })

    pack = asyncio.run(itemicons.prepare(inst))
    assert pack is not None

    # Real items from blockstates/, models/item/, and items/ are rendered.
    assert "minecraft:oak_stairs" in pack.blocks
    assert "minecraft:wheat" in pack.blocks
    assert "minecraft:custom_door" in pack.blocks
    assert "minecraft:tinted_leaf" in pack.blocks

    # Internal blockstate sub-variants and templates are excluded.
    assert "minecraft:oak_stairs_inner" not in pack.blocks
    assert "minecraft:cube_all" not in pack.blocks
    assert "minecraft:template_fence" not in pack.blocks

    # Constant red tint from items/*.json was applied to tinted_leaf.
    leaf_bytes = itemicons.icon_bytes(inst, "minecraft:tinted_leaf")
    assert leaf_bytes is not None
    with Image.open(io.BytesIO(leaf_bytes)) as im:
        center = im.getpixel((im.width // 2, im.height // 2))
        assert center[0] > 100 and center[1] == 0 and center[2] == 0


def test_empty_element_models_fall_back_to_flat_texture(_isolate):
    inst = _isolate
    jar = itemicons.JAR_DIR / f"{itemicons._safe_name(inst.cfg.mc_version)}.jar"
    _jar(jar, {
        "assets/minecraft/blockstates/water.json": json.dumps({"variants": {"": {"model": "minecraft:block/water"}}}),
        "assets/minecraft/models/block/water.json": json.dumps({"textures": {"particle": "minecraft:block/water_still"}}),
        "assets/minecraft/textures/block/water_still.png": _png_bytes((30, 90, 220, 255)),
        "assets/minecraft/models/block/barrier.json": json.dumps({"textures": {"particle": "minecraft:item/barrier"}}),
        "assets/minecraft/textures/item/barrier.png": _png_bytes((220, 30, 30, 255)),
    })

    pack = asyncio.run(itemicons.prepare(inst))
    assert pack is not None
    assert "minecraft:water" not in pack.blocks
    assert "minecraft:barrier" not in pack.blocks

    barrier = itemicons.icon_bytes(inst, "minecraft:barrier")
    assert barrier is not None
    with Image.open(io.BytesIO(barrier)) as im:
        assert im.size == (16, 16)
        assert im.getpixel((0, 0)) == (220, 30, 30, 255)


def test_animated_flat_strip_is_cropped_to_square(_isolate):
    inst = _isolate
    strip = Image.new("RGBA", (16, 64), (10, 10, 10, 255))
    strip.paste(Image.new("RGBA", (16, 16), (40, 200, 90, 255)), (0, 0))
    buf = io.BytesIO()
    strip.save(buf, format="PNG")

    jar = itemicons.JAR_DIR / f"{itemicons._safe_name(inst.cfg.mc_version)}.jar"
    _jar(jar, {
        "assets/minecraft/textures/item/prismarine_shard.png": buf.getvalue(),
    })

    asyncio.run(itemicons.prepare(inst))
    data = itemicons.icon_bytes(inst, "minecraft:prismarine_shard")
    assert data is not None
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (16, 16)
        assert im.getpixel((0, 0)) == (40, 200, 90, 255)


def test_concurrent_prepare_and_get_icon_build_pack_only_once(_isolate, monkeypatch):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)

    builds = []
    real_build = itemicons._build_pack_sync

    def counting_build(*args, **kwargs):
        builds.append(1)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(itemicons, "_build_pack_sync", counting_build)

    async def run():
        results = await asyncio.gather(
            itemicons.prepare(inst),
            itemicons.get_icon(inst, "minecraft:stone"),
            itemicons.get_icon(inst, "minecraft:diamond_pickaxe"),
            itemicons.get_icon(inst, "minecraft:grass_block"),
        )
        return results

    results = asyncio.run(run())
    assert len(builds) == 1
    assert results[0] is not None
    assert results[1] is not None and results[2] is not None and results[3] is not None


def test_legacy_cache_directories_are_cleaned_up(_isolate):
    inst = _isolate
    _client_jar(inst.cfg.mc_version)
    for name in ("assets", "overlays", "png", "blocks", "index"):
        d = itemicons.ROOT / name / "stale"
        d.mkdir(parents=True, exist_ok=True)
        (d / "file.txt").write_text("old", encoding="utf-8")

    asyncio.run(itemicons.prepare(inst))
    for name in ("assets", "overlays", "png", "blocks", "index"):
        assert not (itemicons.ROOT / name).exists()


def test_performance_smoke_200_blocks(_isolate):
    inst = _isolate
    rng = random.Random(123)
    entries = {
        "assets/minecraft/models/block/cube.json": json.dumps({
            "elements": [{
                "from": [0, 0, 0], "to": [16, 16, 16],
                "faces": {
                    f: {"texture": f"#{f}", "uv": [0, 0, 16, 16]}
                    for f in ("up", "down", "north", "south", "east", "west")
                },
            }],
        }),
        "assets/minecraft/models/block/cube_all.json": json.dumps({
            "parent": "minecraft:block/cube",
            "textures": {f: "#all" for f in ("up", "down", "north", "south", "east", "west")},
        }),
    }
    for i in range(200):
        px = bytes(rng.getrandbits(8) for _ in range(16 * 16 * 4))
        im = Image.frombytes("RGBA", (16, 16), px)
        im.putalpha(255)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        entries[f"assets/minecraft/textures/block/b{i}.png"] = buf.getvalue()
        entries[f"assets/minecraft/models/block/b{i}.json"] = json.dumps({
            "parent": "minecraft:block/cube_all",
            "textures": {"all": f"minecraft:block/b{i}"},
        })
        entries[f"assets/minecraft/blockstates/b{i}.json"] = json.dumps({
            "variants": {"": {"model": f"minecraft:block/b{i}"}},
        })

    jar = itemicons.JAR_DIR / f"{itemicons._safe_name(inst.cfg.mc_version)}.jar"
    _jar(jar, entries)

    t0 = time.perf_counter()
    pack = itemicons._build_pack_sync(inst, itemicons.signature(inst), itemicons._assets_key(itemicons.signature(inst)), jar)
    build_dt = time.perf_counter() - t0
    assert len(pack.blocks) == 200
    assert build_dt < 5.0

    t1 = time.perf_counter()
    for i in range(200):
        res = pack.get(f"minecraft:b{i}")
        assert res is not None and res[0].startswith(b"\x89PNG")
    lookup_dt = time.perf_counter() - t1
    assert lookup_dt < 0.5
