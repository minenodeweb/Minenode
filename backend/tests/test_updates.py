import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from app import content, datapacks, net, resourcepacks, updates
from app.modpacks import ModpackError


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


DATAPACK = _zip({"pack.mcmeta": json.dumps({"pack": {"pack_format": 48, "description": "x"}}),
                 "data/x/function/a.mcfunction": "say hi"})


def _inst(tmp_path, engine="paper", modpack=None, running=False):
    logs = []
    inst = SimpleNamespace(
        cfg=SimpleNamespace(id="s1", name="Survival", type=engine, mc_version="1.21.4", modpack=modpack),
        dir=tmp_path, running=running, state="online" if running else "offline",
        props=lambda: {"level-name": "world"}, panel_log=logs.append, logs=logs)
    (tmp_path / "server.properties").write_text("level-name=world\n")
    return inst


# ---------------------------------------------------------------- plugins / mods

def test_only_files_installed_by_the_panel_are_updatable(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    content.save_meta(inst, {"files": {
        "LuckPerms-5.4.jar": {"source": "modrinth", "project_id": "lp"},
        "Chunky-1.4.jar": {"source": "curseforge", "project_id": "123"},
        "Geyser-compat.jar": {"source": "managed"},
    }, "hashes": {}})

    async def inventory(inst, check=True):
        def it(file, update=True):
            return {"file": file, "title": file, "enabled": True, "version_number": "1",
                    "project_id": "p-" + file, "update": {"version_id": "v-" + file, "version_number": "2"} if update else None}
        return {"items": [it("LuckPerms-5.4.jar"), it("Chunky-1.4.jar"), it("Geyser-compat.jar"),
                          it("MojPlugin.jar"), it("Aktualny.jar", update=False)]}
    monkeypatch.setattr(content, "inventory", inventory)
    items = {i["file"]: i for i in asyncio.run(updates._content_items(inst, False))}
    assert items["LuckPerms-5.4.jar"]["update_available"] and items["LuckPerms-5.4.jar"]["latest"] == "2"
    assert items["Chunky-1.4.jar"]["update_available"]  # from CurseForge, newer version found on Modrinth
    assert not items["MojPlugin.jar"]["update_available"] and "ręcznie" in items["MojPlugin.jar"]["skip"]
    assert items["MojPlugin.jar"]["latest"] == "2"  # shown, but never changed
    assert not items["Geyser-compat.jar"]["update_available"] and "Bedrock" in items["Geyser-compat.jar"]["skip"]
    assert not items["Aktualny.jar"]["update_available"]


def test_modpack_files_are_left_alone(tmp_path, monkeypatch):
    inst = _inst(tmp_path, engine="fabric", modpack={"name": "Pack"})

    async def inventory(inst, check=True):
        return {"items": [{"file": "sodium.jar", "title": "Sodium", "enabled": True,
                           "update": {"version_id": "v2", "version_number": "2"}}]}
    monkeypatch.setattr(content, "inventory", inventory)
    item = asyncio.run(updates._content_items(inst, False))[0]
    assert not item["update_available"] and "modpacka" in item["skip"]


def test_content_update_replaces_a_curseforge_copy_and_keeps_it_disabled(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "Chunky-CF.jar.disabled").write_bytes(b"old")
    content.save_meta(inst, {"files": {"Chunky-CF.jar": {"source": "curseforge", "project_id": "123"}}, "hashes": {}})

    class FakeInstaller:
        def __init__(self, inst, key):
            self.inst = inst

        async def modrinth(self, pid, version_id):
            (plugins / "Chunky-Paper-1.5.jar").write_bytes(b"new")
            meta = content.load_meta(self.inst)
            meta["files"]["Chunky-Paper-1.5.jar"] = {"source": "modrinth", "project_id": pid, "version_id": version_id}
            content.save_meta(self.inst, meta)
    monkeypatch.setattr(content, "Installer", FakeInstaller)
    item = {"kind": "plugin", "file": "Chunky-CF.jar", "project_id": "fALzjamp", "version_id": "v15", "enabled": False}
    asyncio.run(updates._update_content(inst, item, ""))
    assert not (plugins / "Chunky-CF.jar.disabled").exists()
    assert (plugins / "Chunky-Paper-1.5.jar.disabled").exists()  # disabled before, disabled after
    assert "Chunky-CF.jar" not in content.load_meta(inst)["files"]


# ---------------------------------------------------------------- datapacks

def test_datapack_update_keeps_a_disabled_pack_disabled(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    disabled = datapacks.disabled_dir(inst)
    disabled.mkdir(parents=True)
    (disabled / "terralith-2.5.zip").write_bytes(DATAPACK)
    datapacks._save_meta(inst, {"terralith-2.5.zip": {"source": "modrinth", "project_id": "terra", "version": "2.5"}})

    async def get_json(url, **kw):
        return {"title": "Terralith", "icon_url": None}

    async def download(url, dest, **kw):
        dest.write_bytes(DATAPACK)
    monkeypatch.setattr(net, "get_json", get_json)
    monkeypatch.setattr(net, "download", download)
    monkeypatch.setattr(datapacks, "TMP_DIR", tmp_path / "tmp")
    version = {"id": "v26", "project_id": "terra", "version_number": "2.6",
               "files": [{"filename": "terralith-2.6.zip", "url": "u", "primary": True, "hashes": {}}]}
    asyncio.run(datapacks.update_modrinth(inst, "terralith-2.5.zip", version))
    assert (disabled / "terralith-2.6.zip").exists() and not (disabled / "terralith-2.5.zip").exists()
    assert not (datapacks.active_dir(inst) / "terralith-2.6.zip").exists()
    meta = datapacks._load_meta(inst)
    assert meta["terralith-2.6.zip"]["version_id"] == "v26" and "terralith-2.5.zip" not in meta


def test_uploaded_datapacks_are_not_checked(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    active = datapacks.active_dir(inst)
    active.mkdir(parents=True)
    (active / "moj.zip").write_bytes(DATAPACK)
    (active / "terralith.zip").write_bytes(DATAPACK)
    datapacks._save_meta(inst, {"moj.zip": {"source": "upload"},
                                "terralith.zip": {"source": "modrinth", "project_id": "terra", "version": "2.5"}})
    calls = []

    async def versions(pid, mc, loaders):
        calls.append((pid, mc, tuple(loaders)))
        return [{"id": "v26", "version_number": "2.6", "version_type": "release"}]
    monkeypatch.setattr(content, "modrinth_versions", versions)
    items = {i["file"]: i for i in asyncio.run(updates._datapack_items(inst, True))}
    assert items["terralith.zip"]["update_available"] and items["terralith.zip"]["latest"] == "2.6"
    assert not items["moj.zip"]["update_available"] and items["moj.zip"]["skip"]
    assert calls == [("terra", "1.21.4", ("datapack",))]  # exactly this Minecraft version


# ---------------------------------------------------------------- resource packs

def test_resource_pack_update_keeps_it_active_and_required(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    m = SimpleNamespace(settings=SimpleNamespace(public_panel_url="http://panel.test", base_domain="",
                                                 pack_subdomain="packs", public_address=""))
    inst.m = m
    old = {"id": "old1", "name": "Faithful", "source": "modrinth", "project_id": "faith", "version": "1.0",
           "token": "tok", "sha1": "a" * 40}
    resourcepacks.save_index(inst, [old])
    resourcepacks.packs_dir(inst).mkdir(parents=True, exist_ok=True)
    (resourcepacks.packs_dir(inst) / "old1.zip").write_bytes(b"zip")
    (tmp_path / "server.properties").write_text(
        "resource-pack=http://panel.test/rp/s1/tok/old1.zip\nrequire-resource-pack=true\n"
        'resource-pack-prompt={"text": "Zainstaluj"}\n')

    async def install_modrinth(inst, project_id, version_id):
        new = {"id": "new1", "name": "Faithful", "source": "modrinth", "project_id": project_id, "version": "1.1",
               "token": "tok2", "sha1": "b" * 40}
        resourcepacks.save_index(inst, resourcepacks.load_index(inst) + [new])
        return new
    monkeypatch.setattr(resourcepacks, "install_modrinth", install_modrinth)
    asyncio.run(resourcepacks.update_modrinth(inst, m, "old1", "v11"))
    props = (tmp_path / "server.properties").read_text()
    assert "/new1.zip" in props and "require-resource-pack=true" in props and "Zainstaluj" in props
    assert [p["id"] for p in resourcepacks.load_index(inst)] == ["new1"]


# ---------------------------------------------------------------- applying

def test_apply_installs_only_the_chosen_updates(tmp_path, monkeypatch):
    inst = _inst(tmp_path, running=True)
    state = {"items": [
        {"id": "plugin:a.jar", "kind": "plugin", "title": "A", "version": "1", "latest": "2", "update_available": True},
        {"id": "plugin:b.jar", "kind": "plugin", "title": "B", "version": "1", "latest": "3", "update_available": True},
        {"id": "plugin:c.jar", "kind": "plugin", "title": "C", "version": "1", "latest": None, "update_available": False},
    ]}
    done = []

    async def server_updates(inst, refresh=False):
        return state

    async def apply_one(inst, item, key):
        if item["title"] == "B":
            raise ModpackError("brak pliku")
        done.append(item["id"])
    monkeypatch.setattr(updates, "server_updates", server_updates)
    monkeypatch.setattr(updates, "_apply_one", apply_one)
    r = asyncio.run(updates.apply(inst, ["plugin:a.jar", "plugin:c.jar"]))
    assert done == ["plugin:a.jar"] and r["restart_required"] and not r["failed"]
    r = asyncio.run(updates.apply(inst, None))  # "update all": every available one, failures reported
    assert done == ["plugin:a.jar", "plugin:a.jar"] and r["failed"][0]["title"] == "B"


def test_components_report_picolimbo_from_the_image(monkeypatch):
    from app import picolimbo
    monkeypatch.setattr(picolimbo, "status", lambda: {"source": "image", "repo": "Quozul/PicoLimbo",
                                                      "version": "1.15.0+mc26.3", "tag": "v1.15.0+mc26.3"})

    async def latest(repo, refresh=False):
        return "v1.16.0+mc26.4"
    monkeypatch.setattr(updates, "_github_latest", latest)
    comps = asyncio.run(updates.components(SimpleNamespace(bedrock=None)))
    pico = next(c for c in comps if c["id"] == "picolimbo")
    assert pico["update_available"] and pico["latest"] == "1.16.0+mc26.4" and pico["action"] == "image"
    with pytest.raises(ModpackError):
        asyncio.run(updates.update_component(SimpleNamespace(bedrock=None), "picolimbo"))


# ---------------------------------------------------------------- choosing the right build on Modrinth

def _v(vid, number, loaders, vtype="release", date="2026-10-10", pid="p"):
    return {"id": vid, "version_number": number, "loaders": loaders, "version_type": vtype,
            "date_published": date, "project_id": pid}


def test_paper_plugin_is_not_updated_to_the_spigot_build(monkeypatch):
    installed = {"h1": _v("v29p", "5.29-paper", ["folia", "paper", "purpur"]),
                 "h2": _v("v28p", "5.28-paper", ["folia", "paper", "purpur"], date="2026-09-25")}
    asked = []

    async def post_json(url, body, **kw):
        asked.append(body["loaders"])
        # Modrinth with "paper" only: the Paper builds. With spigot allowed it would answer 5.29-spigot.
        assert body["loaders"] == ["paper"]
        return {h: _v("v29p", "5.29-paper", ["folia", "paper", "purpur"]) for h in body["hashes"]}
    monkeypatch.setattr(net, "post_json", post_json)
    out = asyncio.run(content.find_updates(installed, content.LOADER_COMPAT["paper"], "26.3"))
    assert "h1" not in out  # already the newest Paper build
    assert out["h2"]["version_number"] == "5.29-paper"


def test_a_release_is_not_updated_to_a_beta(monkeypatch):
    installed = {"h": _v("v22", "0.22.0", ["paper"], date="2026-11-01", pid="lss")}

    async def post_json(url, body, **kw):
        return {"h": _v("b24", "0.24.0-beta.1", ["paper"], vtype="beta", date="2026-11-24", pid="lss")}

    async def versions(pid, mc, loaders):
        return [_v("b24", "0.24.0-beta.1", ["paper"], vtype="beta", date="2026-11-24"),
                _v("v23", "0.23.0", ["paper"], date="2026-11-23")]
    monkeypatch.setattr(net, "post_json", post_json)
    monkeypatch.setattr(content, "modrinth_versions", versions)
    out = asyncio.run(content.find_updates(installed, ["paper", "spigot"], "26.3"))
    assert out["h"]["version_number"] == "0.23.0"
    beta = {"h": _v("b23", "0.23.0-beta.2", ["paper"], vtype="beta", date="2026-11-20", pid="lss")}
    assert asyncio.run(content.find_updates(beta, ["paper"], "26.3"))["h"]["version_number"] == "0.24.0-beta.1"


def test_a_spigot_only_plugin_keeps_using_spigot_builds(monkeypatch):
    installed = {"h": _v("old", "1.0", ["bukkit", "spigot"], date="2026-01-01")}
    asked = []

    async def post_json(url, body, **kw):
        asked.append(body["loaders"])
        return {"h": _v("new", "1.1", ["bukkit", "spigot"], date="2026-02-01")}
    monkeypatch.setattr(net, "post_json", post_json)
    out = asyncio.run(content.find_updates(installed, content.LOADER_COMPAT["paper"], "1.21.4"))
    assert asked == [["spigot"]] and out["h"]["version_number"] == "1.1"
