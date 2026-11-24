import asyncio
from types import SimpleNamespace

from app import content, skins


def _inst(tmp_path, engine="paper", enabled=True):
    return SimpleNamespace(cfg=SimpleNamespace(type=engine, mc_version="26.3", skins_enabled=enabled),
                           dir=tmp_path, online_mode=False, panel_log=lambda *a: None)


def test_jar_names_of_every_platform_are_recognised(tmp_path):
    for engine, name, folder in (("paper", "SkinsRestorer.jar", "plugins"),
                                 ("fabric", "SkinsRestorer-Mod-Fabric-15.12.6.jar", "mods"),
                                 ("neoforge", "SkinsRestorer-Mod-NeoForge-15.12.6.jar", "mods")):
        d = tmp_path / engine
        (d / folder).mkdir(parents=True)
        (d / folder / name).write_bytes(b"x")
        assert skins.SKINSRESTORER.installed_info(_inst(d, engine))["file"] == name
    other = tmp_path / "other"
    (other / "plugins").mkdir(parents=True)
    (other / "plugins" / "SkinsRestorerAddon-1.0.jar").write_bytes(b"x")  # some unrelated add-on
    assert skins.SKINSRESTORER.installed_info(_inst(other)) is None


def test_support_per_engine(tmp_path):
    assert skins.supported(_inst(tmp_path, "purpur")) and skins.supported(_inst(tmp_path, "quilt"))
    assert not skins.supported(_inst(tmp_path, "forge"))
    assert "Forge" in skins.unsupported_reason(_inst(tmp_path, "forge"))
    assert not skins.supported(_inst(tmp_path, "vanilla"))


def test_switched_off_keeps_the_jar_but_disabled(tmp_path):
    inst = _inst(tmp_path, enabled=False)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "SkinsRestorer.jar").write_bytes(b"x")
    content.save_meta(inst, {"files": {"SkinsRestorer.jar": {"project_id": skins.SKINSRESTORER.project_id}}, "hashes": {}})
    asyncio.run(skins.prepare(inst))
    assert (tmp_path / "plugins" / "SkinsRestorer.jar.disabled").exists()


def test_switched_on_installs_from_modrinth(tmp_path, monkeypatch):
    inst = _inst(tmp_path)
    calls = []

    async def ensure(i, refresh=False):
        calls.append(i)
        return {"updated": True, "version": "15.12.6", "installed_files": []}
    monkeypatch.setattr(skins.SKINSRESTORER, "ensure", ensure)
    assert asyncio.run(skins.prepare(inst))["version"] == "15.12.6" and calls == [inst]
