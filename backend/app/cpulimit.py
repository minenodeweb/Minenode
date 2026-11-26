"""Per-server CPU quota enforcement across Linux hosts and Docker containers."""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

BackendMode = Literal["cgroup", "systemd", "cpulimit", "jvm"]

PERIOD_US = 100_000  # 100 ms CFS period (matches systemd CPUQuota=)
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_CACHE: dict[str, Any] = {}


def reset_cache() -> None:
    """Clear the detected CPU limiter backend cache (intended for tests)."""
    _CACHE.clear()


def jvm_cpu_flags(pct: int | None) -> list[str]:
    """Return JVM processor-count flag scaled from a CPU percentage quota."""
    if pct is None or pct <= 0:
        return []
    cores = max(1, math.ceil(int(pct) / 100))
    return [f"-XX:ActiveProcessorCount={cores}"]


def _detect_cgroup_dir() -> Path | None:
    """Detect a writable delegated cgroup v2 directory for Minecraft servers."""
    env_dir = os.environ.get("VARDEN_CGROUP_SERVERS", "").strip()
    d = Path(env_dir) if env_dir else Path("/sys/fs/cgroup/varden/servers")
    if not d.is_dir():
        return None
    subtree_file = d / "cgroup.subtree_control"
    ancestor_procs = d.parent / "cgroup.procs"
    if not subtree_file.is_file() or not ancestor_procs.is_file():
        return None
    try:
        subtree = subtree_file.read_text(encoding="utf-8", errors="ignore")
        if "cpu" not in subtree.split():
            return None
        if not os.access(ancestor_procs, os.W_OK):
            return None
    except OSError:
        return None

    probe = d / f".varden-probe-{os.getpid()}"
    try:
        probe.mkdir(exist_ok=True)
        cpu_max = probe / "cpu.max"
        procs = probe / "cgroup.procs"
        if not cpu_max.is_file() or not procs.is_file():
            return None
        if not os.access(cpu_max, os.W_OK) or not os.access(procs, os.W_OK):
            return None
        return d
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            probe.rmdir()


def _detect_systemd_prefix() -> list[str] | None:
    """Detect whether systemd-run can create transient scopes with CPUQuota."""
    if not Path("/run/systemd/system").is_dir():
        return None
    sd = shutil.which("systemd-run")
    true_bin = shutil.which("true")
    if not sd or not true_bin:
        return None
    euid = getattr(os, "geteuid", lambda: 1)()
    prefix = ["systemd-run", "--scope", "-q"] if euid == 0 else ["systemd-run", "--user", "--scope", "-q"]
    try:
        res = subprocess.run(
            [*prefix, "-p", "CPUQuota=100%", true_bin],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        return prefix if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _detect() -> tuple[BackendMode, Path | None, list[str] | None]:
    """Detect and cache the best available CPU limiting mechanism."""
    cached = _CACHE.get("info")
    if cached is not None:
        return cached

    if sys.platform.startswith("linux"):
        if (cg_dir := _detect_cgroup_dir()) is not None:
            res: tuple[BackendMode, Path | None, list[str] | None] = ("cgroup", cg_dir, None)
        elif (sd_prefix := _detect_systemd_prefix()) is not None:
            res = ("systemd", None, sd_prefix)
        elif shutil.which("cpulimit"):
            res = ("cpulimit", None, None)
        else:
            res = ("jvm", None, None)
    else:
        res = ("jvm", None, None)

    _CACHE["info"] = res
    return res


def backend_mode() -> BackendMode:
    """Return the active CPU quota enforcement backend."""
    return _detect()[0]


def wrap_command(cmd: list[str], pct: int | None) -> list[str]:
    """Wrap the server launch command when using systemd transient scopes."""
    if pct is None or pct <= 0:
        return list(cmd)
    mode, _, sd_prefix = _detect()
    if mode == "systemd" and sd_prefix:
        return [*sd_prefix, "-p", f"CPUQuota={int(pct)}%", *cmd]
    return list(cmd)


@dataclass
class CpuLimitHandle:
    """Tracks an attached CPU limiter so it can be cleanly released on stop/exit."""

    mode: BackendMode
    pid: int
    pct: int
    cgroup_dir: Path | None = None
    proc: asyncio.subprocess.Process | None = None

    def has_pending_cleanup(self) -> bool:
        """Return True while a sidecar process or cgroup directory still exists."""
        return self.proc is not None or self.cgroup_dir is not None

    async def release(self) -> None:
        """Stop throttling the process and remove any per-run resources."""
        if self.proc is not None:
            try:
                if self.proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.terminate()
                    try:
                        await asyncio.wait_for(self.proc.wait(), 2.0)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            self.proc.kill()
                        with contextlib.suppress(asyncio.TimeoutError, Exception):
                            await asyncio.wait_for(self.proc.wait(), 2.0)
            finally:
                self.proc = None
                if hasattr(signal, "SIGCONT"):
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(self.pid, signal.SIGCONT)

        if self.cgroup_dir is not None:
            # Lift quota immediately so shutdown and world saves run at full speed.
            with contextlib.suppress(OSError):
                (self.cgroup_dir / "cpu.max").write_text(f"max {PERIOD_US}\n", encoding="utf-8")
            if not self.cgroup_dir.exists():
                self.cgroup_dir = None
            else:
                try:
                    self.cgroup_dir.rmdir()
                    self.cgroup_dir = None
                except OSError:
                    # Directory can be EBUSY while Java is still exiting; cleared on post-exit release.
                    pass


async def attach(server_id: str, pid: int, pct: int | None) -> CpuLimitHandle | None:
    """Attach the detected post-spawn CPU limiter (cgroup v2 or cpulimit) to a running PID."""
    if pct is None or pct <= 0:
        return None
    pct = int(pct)
    mode, cg_root, _ = _detect()

    if mode == "cgroup" and cg_root is not None:
        safe = _SAFE_ID_RE.sub("_", server_id).strip("._") or "srv"
        cg = cg_root / f"{safe}-{pid}"
        cg.mkdir(parents=True, exist_ok=True)
        try:
            (cg / "cpu.max").write_text(f"{pct * 1000} {PERIOD_US}\n", encoding="utf-8")
            (cg / "cgroup.procs").write_text(f"{pid}\n", encoding="utf-8")
        except OSError:
            with contextlib.suppress(OSError):
                cg.rmdir()
            raise
        return CpuLimitHandle(mode="cgroup", pid=pid, pct=pct, cgroup_dir=cg)

    if mode == "systemd":
        return CpuLimitHandle(mode="systemd", pid=pid, pct=pct)

    if mode == "cpulimit":
        binary = shutil.which("cpulimit")
        if not binary:
            return None
        max_host_pct = max(1, os.cpu_count() or 1) * 100
        if pct >= max_host_pct:
            return CpuLimitHandle(mode="cpulimit", pid=pid, pct=pct)
        proc = await asyncio.create_subprocess_exec(
            binary, "-p", str(pid), "-l", str(pct), "-z", "-q",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return CpuLimitHandle(mode="cpulimit", pid=pid, pct=pct, proc=proc)

    return CpuLimitHandle(mode="jvm", pid=pid, pct=pct)
