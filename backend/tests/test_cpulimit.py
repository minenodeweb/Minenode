import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import cpulimit, manager as manager_mod
from app.models import JavaConfig, LaunchSpec, PanelSettings, ServerConfig


@pytest.fixture(autouse=True)
def _clean_cpulimit_cache():
    cpulimit.reset_cache()
    yield
    cpulimit.reset_cache()


def test_java_config_cpu_limit_validation():
    assert JavaConfig().cpu_limit_percent is None
    assert JavaConfig(cpu_limit_percent=0).cpu_limit_percent is None
    assert JavaConfig(cpu_limit_percent="").cpu_limit_percent is None
    assert JavaConfig(cpu_limit_percent="0").cpu_limit_percent is None
    assert JavaConfig(cpu_limit_percent=50).cpu_limit_percent == 50
    assert JavaConfig(cpu_limit_percent=150).cpu_limit_percent == 150
    assert JavaConfig(cpu_limit_percent=6400).cpu_limit_percent == 6400

    for bad in (9, -10, 6401, "abc"):
        with pytest.raises(ValidationError):
            JavaConfig(cpu_limit_percent=bad)


def test_jvm_cpu_flags():
    assert cpulimit.jvm_cpu_flags(None) == []
    assert cpulimit.jvm_cpu_flags(0) == []
    assert cpulimit.jvm_cpu_flags(50) == ["-XX:ActiveProcessorCount=1"]
    assert cpulimit.jvm_cpu_flags(100) == ["-XX:ActiveProcessorCount=1"]
    assert cpulimit.jvm_cpu_flags(101) == ["-XX:ActiveProcessorCount=2"]
    assert cpulimit.jvm_cpu_flags(150) == ["-XX:ActiveProcessorCount=2"]
    assert cpulimit.jvm_cpu_flags(250) == ["-XX:ActiveProcessorCount=3"]


def test_wrap_command_by_backend(monkeypatch, tmp_path):
    cmd = ["java", "-Xms1G", "-Xmx2G", "-jar", "server.jar", "nogui"]

    monkeypatch.setattr(cpulimit, "_detect", lambda: ("systemd", None, ["systemd-run", "--scope", "-q"]))
    assert cpulimit.wrap_command(cmd, 150) == [
        "systemd-run", "--scope", "-q", "-p", "CPUQuota=150%", *cmd,
    ]
    assert cpulimit.wrap_command(cmd, None) == cmd
    assert cpulimit.wrap_command(cmd, 0) == cmd

    for mode, cg in (("cgroup", tmp_path), ("cpulimit", None), ("jvm", None)):
        monkeypatch.setattr(cpulimit, "_detect", lambda m=mode, d=cg: (m, d, None))
        assert cpulimit.wrap_command(cmd, 150) == cmd


def test_cgroup_attach_and_release(tmp_path, monkeypatch):
    monkeypatch.setattr(cpulimit, "_detect", lambda: ("cgroup", tmp_path, None))

    async def scenario():
        handle = await cpulimit.attach("srv/1", 1234, 150)
        assert handle is not None and handle.mode == "cgroup"
        cg = handle.cgroup_dir
        assert cg is not None and cg.parent == tmp_path
        assert cg.name == "srv_1-1234"
        assert (cg / "cpu.max").read_text(encoding="utf-8") == "150000 100000\n"
        assert (cg / "cgroup.procs").read_text(encoding="utf-8") == "1234\n"

        # While files/processes remain in the dir, release() still lifts cpu.max without failing.
        await handle.release()
        assert (cg / "cpu.max").read_text(encoding="utf-8") == "max 100000\n"
        assert handle.has_pending_cleanup()

        # Once kernel pseudo-files/tasks are gone after process exit, a second release() removes the dir.
        (cg / "cpu.max").unlink()
        (cg / "cgroup.procs").unlink()
        await handle.release()
        assert not handle.has_pending_cleanup()
        assert not cg.exists()

    asyncio.run(scenario())


def test_cpulimit_attach_and_release(monkeypatch):
    monkeypatch.setattr(cpulimit, "_detect", lambda: ("cpulimit", None, None))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/cpulimit" if name == "cpulimit" else None)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)

    calls = []

    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = 0
            return 0

    async def fake_exec(*args, **kwargs):
        proc = FakeProc()
        calls.append((args, proc))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        # Above or equal to total host cores does not spawn a sidecar process.
        full = await cpulimit.attach("s1", 4321, 400)
        assert full is not None and full.proc is None
        assert calls == []

        handle = await cpulimit.attach("s1", 4321, 150)
        assert handle is not None and handle.proc is not None
        assert calls[0][0] == ("/usr/bin/cpulimit", "-p", "4321", "-l", "150", "-z", "-q")
        assert handle.has_pending_cleanup()

        await handle.release()
        assert calls[0][1].terminated is True
        assert not handle.has_pending_cleanup()

    asyncio.run(scenario())


def test_manager_build_command_and_summary_with_cpu_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "SERVERS_DIR", tmp_path / "servers")
    monkeypatch.setattr(manager_mod, "PANEL_DIR", tmp_path / "panel")

    cfg = ServerConfig(
        id="s1", name="Survival", type="paper", mc_version="1.21.4",
        subdomain="s1", internal_port=25600, installed=True,
        launch=LaunchSpec(kind="jar", target="paper.jar"),
        java=JavaConfig(min_ram_mb=2048, max_ram_mb=4096, cpu_limit_percent=150),
    )
    inst = manager_mod.ServerInstance(SimpleNamespace(settings=PanelSettings()), cfg)
    assert inst.summary()["cpu_limit_percent"] == 150

    monkeypatch.setattr(cpulimit, "_detect", lambda: ("cgroup", tmp_path, None))
    cmd_cg = inst.build_command("java", 21)
    assert cmd_cg[0] == "java"
    assert "-XX:ActiveProcessorCount=2" in cmd_cg

    monkeypatch.setattr(cpulimit, "_detect", lambda: ("systemd", None, ["systemd-run", "--scope", "-q"]))
    cmd_sd = inst.build_command("java", 21)
    assert cmd_sd[:6] == ["systemd-run", "--scope", "-q", "-p", "CPUQuota=150%", "java"]
    assert "-XX:ActiveProcessorCount=2" in cmd_sd


def test_docker_files_configure_cpulimit_and_cgroup_delegation():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    runtime_section = dockerfile.split("FROM python:3.14-slim-bookworm", 1)[1]
    assert "cpulimit" in runtime_section
    assert "gcc" not in runtime_section
    assert "libc6-dev" not in runtime_section
    assert "patch" not in runtime_section

    entrypoint = (root / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "setup_cgroup_v2" in entrypoint
    assert "VARDEN_CGROUP_SERVERS" in entrypoint
