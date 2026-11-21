import asyncio
import json
from pathlib import Path

import pytest

from app import auth_lobby, picolimbo


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(picolimbo, "BINARY", tmp_path / "app-bin" / "pico_limbo")
    monkeypatch.setattr(picolimbo, "VERSION_FILE", tmp_path / "app-bin" / "picolimbo-version.json")
    monkeypatch.setattr(picolimbo, "LOCAL_OVERRIDE", None)


def test_patch_and_repo_defaults():
    assert picolimbo.REPO == "Quozul/PicoLimbo"
    assert picolimbo.PATCH.is_file() and b"pico_limbo/src/auth.rs" in picolimbo.PATCH.read_bytes()
    assert picolimbo.display_version("v1.15.0+mc26.3") == "1.15.0+mc26.3"
    assert picolimbo.display_version(None) is None


def test_image_binary_and_metadata_status():
    picolimbo.BINARY.parent.mkdir(parents=True, exist_ok=True)
    picolimbo.BINARY.write_text("elf-binary", encoding="utf-8")
    picolimbo.VERSION_FILE.write_text(
        json.dumps({
            "tag": "v1.15.0+mc26.3",
            "version": "1.15.0+mc26.3",
            "repo": "Quozul/PicoLimbo",
            "source": "image",
            "built_at": 123.0,
        }),
        encoding="utf-8",
    )

    assert picolimbo.binary() == picolimbo.BINARY
    info = picolimbo.installed()
    assert info is not None and info["tag"] == "v1.15.0+mc26.3"
    assert picolimbo.status() == {
        "source": "image",
        "repo": "Quozul/PicoLimbo",
        "version": "1.15.0+mc26.3",
        "tag": "v1.15.0+mc26.3",
        "built_at": 123.0,
    }


def test_missing_binary_raises_clear_error():
    assert picolimbo.installed() is None
    assert picolimbo.status() == {
        "source": "image",
        "repo": "Quozul/PicoLimbo",
        "version": None,
        "tag": None,
        "built_at": None,
    }
    with pytest.raises(picolimbo.PicoLimboError, match="MINENODE_PICOLIMBO_BINARY"):
        picolimbo.binary()


def test_local_binary_override(tmp_path, monkeypatch):
    local = tmp_path / "my_pico_limbo"
    monkeypatch.setattr(picolimbo, "LOCAL_OVERRIDE", str(local))
    with pytest.raises(picolimbo.PicoLimboError, match="MINENODE_PICOLIMBO_BINARY"):
        picolimbo.binary()

    local.write_text("x", encoding="utf-8")
    assert picolimbo.binary() == local
    assert picolimbo.status()["source"] == "local"
    assert picolimbo.status()["version"] is None

    local.with_name("picolimbo-version.json").write_text(
        json.dumps({"tag": "v1.16.0+mc26.4", "built_at": 456.0}),
        encoding="utf-8",
    )
    assert picolimbo.status()["source"] == "local"
    assert picolimbo.status()["version"] == "1.16.0+mc26.4"
    assert picolimbo.status()["tag"] == "v1.16.0+mc26.4"
    assert picolimbo.status()["built_at"] == 456.0


def test_cleanup_legacy_data_removes_only_runtime_build_dirs(tmp_path):
    lobby_dir = tmp_path / "auth-lobby"
    (lobby_dir / "rust" / "bin").mkdir(parents=True)
    (lobby_dir / "rust" / "bin" / "rustc").write_text("old", encoding="utf-8")
    (lobby_dir / "build" / "target").mkdir(parents=True)
    (lobby_dir / "build" / "target" / "a.o").write_text("old", encoding="utf-8")
    (lobby_dir / "bin").mkdir(parents=True)
    (lobby_dir / "bin" / "pico_limbo").write_text("old", encoding="utf-8")
    config_file = lobby_dir / "server.toml"
    config_file.write_text('bind = "127.0.0.1:25999"\n', encoding="utf-8")

    removed = picolimbo.cleanup_legacy_data(lobby_dir)

    assert sorted(removed) == ["bin", "build", "rust"]
    assert not (lobby_dir / "rust").exists()
    assert not (lobby_dir / "build").exists()
    assert not (lobby_dir / "bin").exists()
    assert config_file.is_file()


def test_auth_lobby_start_handles_missing_binary_gracefully(tmp_path, monkeypatch):
    lobby_dir = tmp_path / "auth-lobby"
    (lobby_dir / "rust").mkdir(parents=True)
    (lobby_dir / "rust" / "x").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(auth_lobby, "AUTH_LOBBY_DIR", lobby_dir)

    async def scenario():
        lobby = auth_lobby.AuthLobby(manager=None)
        await lobby.start()
        assert not lobby.running
        assert lobby.proc is None
        assert lobby.error and "PicoLimbo" in lobby.error
        assert lobby.picolimbo_status()["source"] == "image"
        assert lobby._cleanup_task is not None
        await lobby._cleanup_task
        await lobby.stop()

    asyncio.run(scenario())
    assert not (lobby_dir / "rust").exists()


def test_dockerfile_builds_picolimbo_latest_without_runtime_compiler():
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "FROM rust:1-slim-bookworm AS picolimbo" in text
    assert "ARG PICOLIMBO_TAG=latest" in text
    assert "COPY --from=picolimbo /out/pico_limbo ./bin/pico_limbo" in text
    assert "COPY --from=picolimbo /out/picolimbo-version.json ./bin/picolimbo-version.json" in text

    runtime_section = text.split("FROM python:3.12-slim-bookworm", 1)[1]
    assert "gcc" not in runtime_section
    assert "libc6-dev" not in runtime_section
    assert "patch" not in runtime_section
