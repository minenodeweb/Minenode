"""Installations from before the rename (MineNode → Varden) keep working."""
import importlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

from app import auth, legacy, packs

OLD = "mine" + "node"


def test_metadata_folder_is_moved(tmp_path):
    old = tmp_path / f".{OLD}"
    (old / "resourcepacks").mkdir(parents=True)
    (old / "content.json").write_text('{"files": {}}')
    legacy.migrate_meta_dir(tmp_path)
    assert not old.exists()
    assert (tmp_path / ".varden" / "content.json").read_text() == '{"files": {}}'
    assert (tmp_path / ".varden" / "resourcepacks").is_dir()
    legacy.migrate_meta_dir(tmp_path)  # nothing left to do – no error


def test_metadata_folders_are_merged_without_overwriting(tmp_path):
    (tmp_path / f".{OLD}").mkdir()
    (tmp_path / ".varden").mkdir()
    (tmp_path / f".{OLD}" / "content.json").write_text("old")
    (tmp_path / f".{OLD}" / "datapacks.json").write_text("old-dp")
    (tmp_path / ".varden" / "content.json").write_text("new")
    legacy.migrate_meta_dir(tmp_path)
    assert (tmp_path / ".varden" / "content.json").read_text() == "new"
    assert (tmp_path / ".varden" / "datapacks.json").read_text() == "old-dp"
    assert not (tmp_path / f".{OLD}").exists()


def test_old_geyser_extension_is_removed(tmp_path):
    (tmp_path / "MineNodeRouter.jar").write_bytes(b"jar")
    (tmp_path / f"{OLD}-router").mkdir()
    (tmp_path / f"{OLD}-router" / "routes.properties").write_text("a=b")
    (tmp_path / "VardenRouter.jar").write_bytes(b"new")
    legacy.remove_old_router(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["VardenRouter.jar"]


def test_old_package_files_are_accepted(tmp_path):
    assert legacy.is_pack_name("survival.vardenpack") and legacy.is_pack_name(f"Survival.{OLD.upper()}PACK")
    assert not legacy.is_pack_name("survival.zip")
    pack = tmp_path / f"old.{OLD}pack"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": f"{OLD}pack", "schema": 1, "server_config": {"id": "x"}}))
    assert packs.validate(pack)["format"] == f"{OLD}pack"


def test_old_session_cookie_is_accepted():
    assert auth.session_token({auth.OLD_COOKIE: "tok"}) == "tok"
    assert auth.session_token({auth.COOKIE: "new", auth.OLD_COOKIE: "old"}) == "new"


def test_old_environment_variables_fill_the_new_names(monkeypatch):
    import app
    monkeypatch.setenv(f"{OLD.upper()}_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv(f"{OLD.upper()}_PORT", "9000")
    monkeypatch.setenv("VARDEN_PORT", "8080")  # the image default – the user's old setting wins
    monkeypatch.setenv(f"{OLD.upper()}_DATA", "/old")
    monkeypatch.setenv("VARDEN_DATA", "/mine")  # set on purpose – kept
    monkeypatch.delenv("VARDEN_TRUSTED_PROXIES", raising=False)
    importlib.reload(app)
    assert os.environ["VARDEN_TRUSTED_PROXIES"] == "10.0.0.0/8"
    assert os.environ["VARDEN_PORT"] == "9000"
    assert os.environ["VARDEN_DATA"] == "/mine"


def test_entrypoint_maps_old_environment_variables():
    script = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"
    remap = script.read_text().split("if [ \"$(id -u)\" = \"0\" ]")[0]
    env = {"PATH": os.environ["PATH"], f"{OLD.upper()}_PORT": "9000", "VARDEN_PORT": "8080",
           f"{OLD.upper()}_SECURE_COOKIES": "true", f"{OLD.upper()}_DATA": "/old", "VARDEN_DATA": "/mine"}
    out = subprocess.run(["sh", "-c", remap + "\nenv"], env=env, capture_output=True, text=True, check=True).stdout
    assert "VARDEN_PORT=9000" in out and "VARDEN_SECURE_COOKIES=true" in out and "VARDEN_DATA=/mine" in out
