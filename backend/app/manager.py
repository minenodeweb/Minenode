"""Server lifecycle, console streaming, monitoring and player tracking."""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import gzip
import logging
import os
import re
import shlex
import shutil
import signal
import time
import uuid
import zlib
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import psutil

from . import cpulimit
from . import java as javamod
from . import legacy
from . import players as playersmod
from . import playerstats
from . import providers
from .config import INTERNAL_MAP_PORT_START, INTERNAL_PORT_START, INTERNAL_VOICE_PORT_START, PANEL_DIR, PROXY_PORT, SERVERS_DIR, VOICE_PORT
from .models import PanelSettings, ServerConfig
from .properties import read_properties, update_properties
from .store import JsonStore

log = logging.getLogger("varden")

DEFAULT_ICON = Path(__file__).resolve().parent / "assets" / "server-icon.png"
BACKEND_HOST = "127.0.0.1"  # game servers listen on loopback only; players reach them through the proxy


def server_env(java: str | Path) -> dict[str, str]:
    """Environment for a server process: the chosen Java is also `java` on PATH and JAVA_HOME.

    Launchers (custom JARs, some hybrids, start scripts) run `java` again as a child process and
    look it up on PATH; inside the container the panel's Temurin is not on PATH otherwise."""
    env = {**os.environ, "TERM": "xterm-256color"}
    bin_dir = Path(java).parent
    if bin_dir.name == "bin" and (bin_dir / Path(java).name).exists():
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")]).rstrip(os.pathsep)
        env["JAVA_HOME"] = str(bin_dir.parent)
    return env
ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")
CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ABCDEFGHJKSTfhlnsu]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\([A-Za-z0-9]|\r")
NAME = r"([A-Za-z0-9_]{1,16})"
# Newer servers (e.g. Purpur/Paper for 26.x) log broadcasts as "]: System chat: Steve joined the game".
JOIN_RE = re.compile(rf"\]: (?:System chat: )?{NAME} joined the game")
LEAVE_RE = re.compile(rf"\]: (?:System chat: )?{NAME} left the game")
# Printed by the server itself whatever plugins do to the join/leave chat messages (EssentialsX etc.)
LOGIN_RE = re.compile(rf"\]: {NAME}\[/[^\]]*\] logged in with entity id")
# Player chat from the server log: "]: <Steve> message", optionally prefixed with "[Not Secure]".
# Bedrock players seen through Geyser can have dots or spaces in their names. Only the first "]: "
# counts (the end of the log prefix), so text inside a message or a kick reason is never a new line.
_FIRST = r"^(?:(?!\]: ).)*\]: "
CHAT_RE = re.compile(_FIRST + r"(?:\[Not Secure\] )?<([^<>]{1,40})>\s?(.*)$")
EMOTE_RE = re.compile(_FIRST + r"(?:\[Not Secure\] )?\* [^\s<>]{1,40} ")  # /me
LOG_TIME_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")
CHAT_BUFFER = 50
# Upper bound for rotated logs scanned backwards until CHAT_BUFFER messages are found.
CHAT_LOG_FILES = 200
ROTATED_LOG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d+)\.log(?:\.gz)?$", re.I)
MC_CODE_RE = re.compile(r"§(?:#[0-9A-Fa-f]{6}|[Xx](?:§[0-9A-Fa-f]){6}|[0-9A-FK-ORXa-fk-orx])")
_NON_INFO_HEADER = re.compile(r"(?:^|[/\s\[])(?:WARN(?:ING)?|ERROR|FATAL|SEVERE|DEBUG|TRACE)\](?:\s|$)", re.I)
_CHAT_THREAD_RE = re.compile(r"Async Chat Thread", re.I)
_CHAT_WRAPPER_RE = re.compile(r"^(?:\[Not Secure\]\s*|System chat:\s*|\[System\]\s*|\[CHAT\]\s*)+", re.I)
_TAG_PREFIX_RE = re.compile(r"^(?:\[(?!Server\]|Rcon\])(?![^\]]*: )[^\]]{1,64}\]\s*)+")
_ANGLE_CHAT_RE = re.compile(r"^<((?:<[^<>]{1,24}>\s*|\[[^\]]{1,24}\]\s*)*[^<>]{1,64})>\s?(.*)$")
_INNER_NAME_TAG_RE = re.compile(r"^(?:<[^<>]{1,24}>\s*|\[[^\]]{1,24}\]\s*)+")
_SEP_CHAT_RE = re.compile(r"^([.]?[A-Za-z0-9_]{1,16}(?: [A-Za-z0-9_]{1,16})?)\s*([:»›>])\s+(.+)$")
_EMOTE_BODY_RE = re.compile(r"^\* [^\s<>]{1,40} ")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ISO_LOG_TIME_RE = re.compile(r"^\[?(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:\s+[^\]]+)?\]?")
_FORGE_LOG_TIME_RE = re.compile(
    r"^\[(\d{1,2})[- ]?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- ]?(\d{4})\s+(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:\s+[^\]]+)?\]",
    re.I,
)
_CLOCK_LOG_TIME_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:\s+[^\]]+)?\]")
LOST_RE = re.compile(rf"\]: {NAME} lost connection:")
UUID_RE = re.compile(rf"UUID of player {NAME} is ([0-9a-fA-F-]{{32,36}})")
DONE_RE = re.compile(r"Done \([\d.,]+s\)!")
LIST_RE = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players online:?\s*(.*)$")
WHITELIST_DENY_RES = [
    re.compile(rf"name={NAME}\b.*not white-?listed", re.I),
    re.compile(rf"Disconnecting {NAME} \(.*not white-?listed", re.I),
    re.compile(rf"{NAME} \(/[^)]*\) lost connection: You are not white-?listed", re.I),
    re.compile(rf"{NAME} lost connection: You are not white-?listed", re.I),
]


def _clean_log_line(raw_line: str) -> str:
    """Strip terminal escapes, carriage returns, BOM, Minecraft section codes and prompts."""
    text = CTRL_RE.sub("", raw_line.rstrip("\n"))
    text = ANSI_RE.sub("", text).lstrip("\ufeff")
    text = MC_CODE_RE.sub("", text)
    while text.startswith("> "):
        text = text[2:]
    return text


def _split_log_line(plain: str) -> tuple[str, str] | None:
    """Split a log line at the first ']: ' into (header, body), skipping non-INFO log levels."""
    cleaned = _clean_log_line(plain).lstrip()
    sep = cleaned.find("]: ")
    if sep == -1:
        return None
    header = cleaned[: sep + 1]
    if _NON_INFO_HEADER.search(header):
        return None
    return header, cleaned[sep + 3 :]


def _parse_chat_line(plain: str, known_names: set[str] | None = None) -> tuple[str, str] | None:
    """Extract (player_name, message_text) from vanilla, modded, System chat or plugin chat lines."""
    parts = _split_log_line(plain)
    if not parts:
        return None
    header, body = parts
    rest = _CHAT_WRAPPER_RE.sub("", body)
    rest = _TAG_PREFIX_RE.sub("", rest)
    rest = _CHAT_WRAPPER_RE.sub("", rest)
    if m := _ANGLE_CHAT_RE.match(rest):
        raw_name = m.group(1).strip()
        text = m.group(2).strip()
        if not raw_name or not text or raw_name.startswith("/") or "=" in raw_name or '"' in raw_name:
            return None
        name = _INNER_NAME_TAG_RE.sub("", raw_name).strip() or raw_name
        return name[:40], text[:500]
    if m := _SEP_CHAT_RE.match(rest):
        name = m.group(1).strip()
        text = m.group(3).strip()
        if not name or not text:
            return None
        if _CHAT_THREAD_RE.search(header) or (known_names is not None and name.lower() in known_names):
            return name[:40], text[:500]
    return None


def _is_emote_line(plain: str) -> bool:
    parts = _split_log_line(plain)
    if not parts:
        return False
    rest = _CHAT_WRAPPER_RE.sub("", parts[1])
    return bool(_EMOTE_BODY_RE.match(rest))


def _parse_log_timestamp(path: Path, plain: str, mtime: float) -> float:
    """Parse log line timestamp across Paper, Vanilla, Fabric, Forge/NeoForge and ISO formats."""
    cleaned = _clean_log_line(plain).lstrip()
    if m := _ISO_LOG_TIME_RE.match(cleaned):
        try:
            return datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
            ).timestamp()
        except ValueError:
            return mtime
    if m := _FORGE_LOG_TIME_RE.match(cleaned):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            try:
                return datetime.datetime(
                    int(m.group(3)), mon, int(m.group(1)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                ).timestamp()
            except ValueError:
                return mtime
    if m := _CLOCK_LOG_TIME_RE.match(cleaned):
        h, minute, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
        named = re.match(r"^(\d{4})-(\d{2})-(\d{2})-", path.name)
        day = datetime.datetime(int(named.group(1)), int(named.group(2)), int(named.group(3))) if named \
            else datetime.datetime.fromtimestamp(mtime)
        try:
            at = day.replace(hour=h, minute=minute, second=sec, microsecond=0).timestamp()
        except ValueError:
            return mtime
        if not named and at > mtime + 60:
            at -= 86400
        return at
    return mtime


def _extract_log_known(plain: str, names: set[str]) -> None:
    """Remember player names seen in join/login/UUID log lines while scanning history."""
    cleaned = _clean_log_line(plain)
    for pat in (UUID_RE, LOGIN_RE, JOIN_RE):
        if m := pat.search(cleaned):
            names.add(m.group(1).lower())
            return

# https://aikar.co/2018/07/02/tuning-the-jvm-g1gc-garbage-collector-flags-for-minecraft/ (mcflags.emc.gs).
# Aikar's rules: the same -Xms and -Xmx, and the >12 GB variant of the young-generation settings.
AIKAR = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200", "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
    "-Dusing.aikars.flags=https://mcflags.emc.gs", "-Daikars.new.flags=true",
]


def jvm_flags(preset: str, max_ram_mb: int, java_feature: int | None) -> list[str]:
    if preset == "none":
        return []
    flags = ["-XX:+IgnoreUnrecognizedVMOptions"]
    if preset == "zgc" and (java_feature is None or java_feature >= 17):
        flags += ["-XX:+UseZGC", "-XX:+AlwaysPreTouch", "-XX:+DisableExplicitGC", "-XX:+PerfDisableSharedMem"]
        if java_feature is None or 21 <= java_feature < 24:
            flags.append("-XX:+ZGenerational")
        return flags
    big = max_ram_mb >= 12 * 1024
    flags += AIKAR + [
        f"-XX:G1NewSizePercent={40 if big else 30}", f"-XX:G1MaxNewSizePercent={50 if big else 40}",
        f"-XX:G1HeapRegionSize={'16M' if big else '8M'}", f"-XX:G1ReservePercent={15 if big else 20}",
        f"-XX:InitiatingHeapOccupancyPercent={20 if big else 15}",
    ]
    return flags


class ServerError(RuntimeError):
    pass


class ServerInstance:
    def __init__(self, manager: "Manager", cfg: ServerConfig):
        self.m = manager
        self.cfg = cfg
        legacy.migrate_meta_dir(SERVERS_DIR / cfg.id)  # .minenode -> .varden (rename from MineNode)
        self.state = "offline" if cfg.installed else "not_installed"
        self.proc: asyncio.subprocess.Process | None = None
        self.console: deque[dict] = deque(maxlen=4000)
        self._seq = 0
        self.chat: deque[dict] = deque(maxlen=CHAT_BUFFER)
        self._chat_seq = 0
        self.subs: set[asyncio.Queue] = set()
        self.online: dict[str, float] = {}
        self.stats: deque[dict] = deque(maxlen=450)  # 15 minutes at 2s
        self.job: dict | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.java_feature: int | None = None
        self._stop_requested = False
        self._crashes: deque[float] = deque(maxlen=5)
        self._lock = asyncio.Lock()
        self._ps: psutil.Process | None = None
        self._waiters: list[tuple[re.Pattern, asyncio.Future]] = []
        self._tasks: set[asyncio.Task] = set()
        self._props_cache: tuple[float, dict] | None = None
        self._last_daily_restart: str | None = None
        self.empty_since: float | None = None  # for idle auto-shutdown
        self._start_seq = 0
        self._autofix_count = 0
        self.players_store = JsonStore(PANEL_DIR / "players" / f"{cfg.id}.json", lambda: {"attempts": {}, "known": {}})
        self._map_render = None
        self._chunky = None
        self._cpu_limiter: cpulimit.CpuLimitHandle | None = None
        self._proxy_players: dict[str, list] = {}  # lowercase name -> [name, open connections, since]
        self._chat_log_sig: tuple | None = None
        self._chat_rot_sig: tuple | None = None
        self._chat_history_loaded = False
        self._chat_latest_state: dict | None = None
        self._chat_rot_cache: dict[str, tuple[tuple, list[tuple[float, str, str]]]] = {}
        self._chat_pending_live: deque[tuple[float, str, str]] = deque(maxlen=CHAT_BUFFER * 2)
        self._load_log_history()

    def _load_log_history(self) -> None:
        latest = self.dir / "logs" / "latest.log"
        try:
            if latest.exists() and latest.stat().st_size < 50 * 1024 * 1024:
                with open(latest, "rb") as fh:
                    fh.seek(max(0, latest.stat().st_size - 64 * 1024))
                    lines = fh.read().decode("utf-8", "replace").splitlines()[-200:]
                for line in lines:
                    self._append(line, "out")
                self._append("— powyżej: historia z logs/latest.log —", "panel")
        except OSError:
            pass
        with contextlib.suppress(Exception):  # startup must not fail on a half-written stats file
            playerstats.sync_known(self)
        self.refresh_chat_history(force=True)

    def _is_running_safe(self) -> bool:
        proc = getattr(self, "proc", None)
        return proc is not None and proc.returncode is None

    def _known_player_names(self) -> set[str]:
        names = {str(n).lower() for n in (getattr(self, "online", None) or {})}
        store = getattr(self, "players_store", None)
        if store is not None:
            known = store.data.get("known") or {}
            names.update(str(k).lower() for k in known)
            names.update(
                str(v.get("name", "")).lower()
                for v in known.values()
                if isinstance(v, dict) and v.get("name")
            )
        names.discard("")
        return names

    @staticmethod
    def _chat_entry(
        path: Path, plain: str, mtime: float, known_names: set[str] | None = None
    ) -> tuple[float, str, str] | None:
        """One chat message from a log line, or None for anything else."""
        parsed = _parse_chat_line(plain, known_names)
        if not parsed:
            return None
        at = _parse_log_timestamp(path, plain, mtime)
        return at, parsed[0], parsed[1]

    def _chat_log_files(self) -> list[tuple[Path, os.stat_result]]:
        """Logs that may contain player chat, newest first; latest.log always comes first."""
        rotated: list[tuple[tuple[int, int, int, int, int], Path, os.stat_result]] = []
        latest_item: tuple[Path, os.stat_result] | None = None
        try:
            for path in (self.dir / "logs").iterdir():
                try:
                    st = path.stat()
                except OSError:
                    continue
                if path.name == "latest.log":
                    latest_item = (path, st)
                elif m := ROTATED_LOG_RE.match(path.name):
                    order = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), st.st_mtime_ns)
                    rotated.append((order, path, st))
        except OSError:
            return []
        rotated.sort(key=lambda item: item[0], reverse=True)
        files = [(path, st) for _, path, st in rotated]
        if latest_item:
            files.insert(0, latest_item)
        return files[: CHAT_LOG_FILES + 1]

    @classmethod
    def _chat_log_entries(
        cls, path: Path, mtime: float, known_names: set[str] | None = None
    ) -> list[tuple[float, str, str]]:
        """The last chat messages in one log file."""
        entries: deque[tuple[float, str, str]] = deque(maxlen=CHAT_BUFFER)
        names = set(known_names) if known_names else set()
        opener = gzip.open if path.name.lower().endswith(".gz") else open
        try:
            with opener(path, "rb") as fh:
                for raw in fh:
                    text = raw.decode("utf-8", "replace")
                    entry = cls._chat_entry(path, text, mtime, names)
                    if entry:
                        entries.append(entry)
                    else:
                        _extract_log_known(text, names)
        except (OSError, EOFError, zlib.error):
            return []
        return list(entries)

    def _scan_rotated_chat(
        self, path: Path, st: os.stat_result, known_names: set[str]
    ) -> list[tuple[float, str, str]]:
        cache = getattr(self, "_chat_rot_cache", None)
        if cache is None:
            cache = {}
            self._chat_rot_cache = cache
        sig = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        cached = cache.get(str(path))
        if cached and cached[0] == sig:
            return cached[1]
        entries = self._chat_log_entries(path, st.st_mtime, known_names)
        cache[str(path)] = (sig, entries)
        return entries

    def _scan_latest_chat(
        self, path: Path, st: os.stat_result, known_names: set[str], is_running: bool
    ) -> tuple[list[tuple[float, str, str]], list[tuple[float, str, str]], bool]:
        """Read latest.log incrementally when appended to; reset cleanly on rotation/overwrite."""
        state = getattr(self, "_chat_latest_state", None)
        inode = (st.st_dev, st.st_ino)
        if state and (state["inode"], state["size"], state["mtime_ns"]) == (inode, st.st_size, st.st_mtime_ns):
            return list(state["entries"]), [], False
        try:
            with path.open("rb") as fh:
                same_file = bool(state and state["inode"] == inode and st.st_size >= state["offset"])
                if same_file and state["head"]:
                    same_file = fh.read(len(state["head"])) == state["head"]
                if same_file:
                    entries: deque[tuple[float, str, str]] = state["entries"]
                    file_known: set[str] = state["file_known"] | known_names
                    offset: int = state["offset"]
                    head: bytes = state["head"]
                    was_reset = False
                    fh.seek(offset)
                else:
                    fh.seek(0)
                    head = fh.read(64)
                    fh.seek(0)
                    entries = deque(maxlen=CHAT_BUFFER)
                    file_known = set(known_names)
                    offset = 0
                    was_reset = True
                new_entries: list[tuple[float, str, str]] = []
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if is_running and not raw.endswith(b"\n"):
                        break
                    offset = fh.tell()
                    text = raw.decode("utf-8", "replace")
                    entry = self._chat_entry(path, text, st.st_mtime, file_known)
                    if entry:
                        entries.append(entry)
                        new_entries.append(entry)
                    else:
                        _extract_log_known(text, file_known)
                if len(head) < 64 and offset > 0:
                    fh.seek(0)
                    head = fh.read(64)
        except OSError:
            return [], [], False
        self._chat_latest_state = {
            "inode": inode,
            "head": head,
            "offset": offset,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "entries": entries,
            "file_known": file_known,
        }
        return list(entries), new_entries, was_reset

    def _consume_pending_live(self, name: str, text: str, at: float) -> bool:
        pending = getattr(self, "_chat_pending_live", None)
        if not pending:
            return False
        cutoff = time.time() - 120
        while pending and pending[0][0] < cutoff:
            pending.popleft()
        for idx, (live_t, live_name, live_text) in enumerate(pending):
            if live_name == name and live_text == text and abs(live_t - at) <= 30:
                del pending[idx]
                return True
        return False

    def _reconcile_pending_live(self, entries: list[tuple[float, str, str]]) -> list[tuple[float, str, str]]:
        """Drop live entries already present in logs and keep unflushed recent live lines."""
        pending = getattr(self, "_chat_pending_live", None)
        if not pending:
            return entries
        now = time.time()
        cutoff = now - 120
        while pending and pending[0][0] < cutoff:
            pending.popleft()
        matched: set[int] = set()
        unmatched: deque[tuple[float, str, str]] = deque(maxlen=CHAT_BUFFER * 2)
        extras: list[tuple[float, str, str]] = []
        for live_t, live_name, live_text in pending:
            hit = next(
                (
                    i for i, (at, d_name, d_text) in enumerate(entries)
                    if i not in matched and d_name == live_name and d_text == live_text and abs(live_t - at) <= 30
                ),
                None,
            )
            if hit is not None:
                matched.add(hit)
            elif live_t >= now - 15:
                unmatched.append((live_t, live_name, live_text))
                extras.append((live_t, live_name, live_text))
        self._chat_pending_live = unmatched
        return (entries + extras)[-CHAT_BUFFER:] if extras else entries

    def _apply_chat_entries(self, entries: list[tuple[float, str, str]]) -> None:
        """Update self.chat cleanly, preserving existing sequence numbers when appending/sliding."""
        old = list(self.chat)
        if old:
            matches = lambda item, entry: item["name"] == entry[1] and item["text"] == entry[2] and abs(item["t"] - entry[0]) <= 30
            if len(entries) >= len(old) and all(matches(old[i], entries[i]) for i in range(len(old))):
                for at, name, text in entries[len(old):]:
                    self._add_chat(name, text, at, live=False)
                return
            if len(old) == CHAT_BUFFER and len(entries) == CHAT_BUFFER:
                for start in range(1, CHAT_BUFFER):
                    overlap = CHAT_BUFFER - start
                    if all(matches(old[start + i], entries[i]) for i in range(overlap)):
                        for at, name, text in entries[overlap:]:
                            self._add_chat(name, text, at, live=False)
                        return
        self.chat.clear()
        for at, name, text in entries[-CHAT_BUFFER:]:
            self._add_chat(name, text, at, live=False)

    def refresh_chat_history(self, force: bool = False, clear_empty: bool = False) -> None:
        """Load and incrementally sync chat messages from server logs.

        Keeps latest.log read incrementally while running, fills older slots from cached rotated
        logs backwards until CHAT_BUFFER messages are collected, and avoids duplicating live lines."""
        is_running = self._is_running_safe()
        files = self._chat_log_files()
        signature = tuple((str(p), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) for p, st in files)
        rot_cache = getattr(self, "_chat_rot_cache", None)
        if rot_cache:
            alive = {str(p) for p, _ in files if p.name != "latest.log"}
            for stale in [k for k in rot_cache if k not in alive]:
                rot_cache.pop(stale, None)
        if not force and getattr(self, "_chat_history_loaded", False) and signature == getattr(self, "_chat_log_sig", None):
            return
        latest_item = files[0] if files and files[0][0].name == "latest.log" else None
        rotated_files = files[1:] if latest_item else files
        rotated_sig = tuple((str(p), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) for p, st in rotated_files)
        prev_rot_sig = getattr(self, "_chat_rot_sig", None)
        known = self._known_player_names()
        if latest_item:
            latest_entries, new_latest, latest_reset = self._scan_latest_chat(
                latest_item[0], latest_item[1], known, is_running
            )
        else:
            latest_entries, new_latest, latest_reset = [], [], True
            self._chat_latest_state = None
        if (
            not force
            and getattr(self, "_chat_history_loaded", False)
            and not latest_reset
            and rotated_sig == prev_rot_sig
            and self.chat
        ):
            self._chat_log_sig = signature
            for at, name, text in new_latest:
                if not self._consume_pending_live(name, text, at):
                    self._add_chat(name, text, at, live=False)
            return
        entries = list(latest_entries)
        for path, st in rotated_files:
            if len(entries) >= CHAT_BUFFER:
                break
            file_entries = self._scan_rotated_chat(path, st, known)
            if file_entries:
                entries = file_entries + entries
        entries = entries[-CHAT_BUFFER:]
        if is_running:
            entries = self._reconcile_pending_live(entries)
        self._chat_log_sig = signature
        self._chat_rot_sig = rotated_sig
        self._chat_history_loaded = True
        if not entries:
            if clear_empty and not is_running:
                self.chat.clear()
            return
        self._apply_chat_entries(entries)

    # ------------------------------------------------------------ helpers
    @property
    def map_render(self):
        """Full web-map renders started from the panel (webmap.MapRender)."""
        if getattr(self, "_map_render", None) is None:
            from .webmap import MapRender
            self._map_render = MapRender(self)
        return self._map_render

    @property
    def chunky_runner(self):
        """World pre-generation with Chunky (chunky.ChunkyRunner)."""
        if getattr(self, "_chunky", None) is None:
            from .chunky import ChunkyRunner
            self._chunky = ChunkyRunner(self)
        return self._chunky

    def is_generating(self) -> bool:
        """True while a map render (BlueMap, squaremap, Dynmap) or Chunky world generation is active."""
        mr = getattr(self, "_map_render", None)
        if mr is not None and mr.running:
            return True
        cr = getattr(self, "_chunky", None)
        if cr is None and getattr(getattr(self, "cfg", None), "id", None):
            if (self.dir / ".varden" / "chunky.json").is_file():
                cr = self.chunky_runner
        if cr is not None and (cr.running or cr._pending is not None or (cr.active and not cr.task.get("user_paused"))):
            return True
        return False

    @property
    def dir(self) -> Path:
        return SERVERS_DIR / self.cfg.id

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def spawn(self, coro: Awaitable) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def props(self) -> dict:
        p = self.dir / "server.properties"
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return {}
        if self._props_cache and self._props_cache[0] == mtime:
            return self._props_cache[1]
        data = read_properties(p)
        self._props_cache = (mtime, data)
        return data

    @property
    def online_mode(self) -> bool:
        return self.props().get("online-mode", "true").lower() == "true"

    def main_domain(self) -> str | None:
        """The main domain this server shows: its own choice if the panel still has it, else the first one."""
        domains = self.m.settings.domains()
        chosen = (getattr(self.cfg, "domain", None) or "").strip().lower()
        if chosen in domains:
            return chosen
        return domains[0] if domains else None

    def domain_hosts(self) -> list[str]:
        """<subdomain>.<domain> for every main domain, the one this server shows first."""
        main = self.main_domain()
        domains = self.m.settings.domains()
        ordered = ([main] if main else []) + [d for d in domains if d != main]
        return [f"{self.cfg.subdomain}.{d}" for d in ordered]

    def address(self) -> str:
        s = self.m.settings
        port = "" if PROXY_PORT == 25565 else f":{PROXY_PORT}"
        if self.cfg.aliases:
            return self.cfg.aliases[0] + port
        domain = self.main_domain()
        if domain:
            return f"{self.cfg.subdomain}.{domain}{port}"
        host = s.public_address or "twoja-domena"
        return f"{self.cfg.subdomain}.{host}{port}"

    def addresses(self) -> list[str]:
        """Every address players can use for this server (aliases, then each main domain)."""
        port = "" if PROXY_PORT == 25565 else f":{PROXY_PORT}"
        out = [a + port for a in self.cfg.aliases] + [h + port for h in self.domain_hosts()]
        return out or [self.address()]

    # ------------------------------------------------------------ pub/sub
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def broadcast(self, msg: dict) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _append(self, text: str, src: str) -> None:
        self._seq += 1
        entry = {"seq": self._seq, "t": time.time(), "text": text, "src": src}
        self.console.append(entry)
        self.broadcast({"type": "log", "entry": entry})

    def panel_log(self, text: str) -> None:
        self._append(text, "panel")

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.broadcast({"type": "state", "state": state, "summary": self.summary()})
            vp = getattr(self.m, "voice", None)
            if vp is not None:
                vp.sync()

    def set_job(self, title: str | None, stage: str = "", progress: float | None = None) -> None:
        self.job = None if title is None else {"title": title, "stage": stage, "progress": progress}
        self.broadcast({"type": "job", "job": self.job})

    async def wait_for_log(self, pattern: str, timeout: float) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (re.compile(pattern), fut)
        self._waiters.append(entry)
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(entry)

    # ------------------------------------------------------------ summary
    def summary(self) -> dict:
        props = self.props()
        last = self.stats[-1] if self.stats and self.running else None
        return {
            "id": self.cfg.id,
            "name": self.cfg.name,
            "type": self.cfg.type,
            "mc_version": self.cfg.mc_version,
            "loader_version": self.cfg.loader_version,
            "subdomain": self.cfg.subdomain,
            "aliases": self.cfg.aliases,
            "address": self.address(),
            "addresses": self.addresses(),
            "domain": self.main_domain(),
            "state": self.state,
            "installed": self.cfg.installed,
            "players_online": len(self.online),
            "players": sorted(self.online.keys(), key=str.lower),
            "max_players": int(mp) if (mp := props.get("max-players", "20")).isdigit() else 20,
            "motd": props.get("motd", "A Minecraft Server"),
            "whitelist": props.get("white-list", "false") == "true",
            "online_mode": props.get("online-mode", "true") == "true",
            "cpu": last["cpu"] if last else 0,
            "mem": last["mem"] if last else 0,
            "max_ram_mb": self.cfg.java.max_ram_mb,
            "cpu_limit_percent": self.cfg.java.cpu_limit_percent,
            "started_at": self.started_at if self.running else None,
            "job": self.job,
            "required_java": self.cfg.required_java,
            "java_feature": self.java_feature,
            "internal_port": self.cfg.internal_port,
            "modpack": self.cfg.modpack.model_dump() if self.cfg.modpack else None,
            "has_icon": (self.dir / "server-icon.png").exists(),
            **(self._custom_summary() if self.cfg.type == "custom" else {}),
            "icon_version": self.icon_version(),
            "bedrock_enabled": self.cfg.bedrock_enabled,
            "exit_code": self.exit_code,
            "created_at": self.cfg.created_at,
        }

    # ------------------------------------------------------------ installation
    async def install(self, *, eula: bool, prepare: Callable[[Callable[[str], None]], Awaitable[Any]] | None = None) -> None:
        """Install (or reinstall) the server engine. `prepare` resolves a modpack plan first."""
        if self.running:
            raise ServerError("Zatrzymaj serwer przed instalacją")
        if self.state == "installing":
            raise ServerError("Instalacja już trwa")
        self.set_state("installing")
        self.spawn(self._install(eula, prepare))

    async def _install(self, eula: bool, prepare) -> None:
        log = self.panel_log
        started = time.time()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            plan = None
            if prepare:
                self.set_job("Instalacja modpacka", "Pobieranie paczki", None)
                plan = await prepare(log)
                self.cfg.type = plan.engine
                self.cfg.mc_version = plan.mc_version
                self.cfg.loader_version = plan.loader_version
                self.m.save_server(self.cfg)
                log(f"Modpack: {plan.name} → {providers.ENGINES[plan.engine]['name']} {plan.mc_version}"
                    + (f" (loader {plan.loader_version})" if plan.loader_version else ""))

            self.set_job("Instalacja", "Sprawdzanie wymagań Javy", 0.1)
            required = await providers.required_java(self.cfg.mc_version)
            self.cfg.required_java = required
            log(f"Minecraft {self.cfg.mc_version} wymaga Java {required}+")
            self.set_job("Instalacja", "Przygotowanie środowiska Java", 0.2)
            feature, java = await javamod.ensure_java(self.cfg.java.version or required, log)
            self.java_feature = feature

            self.set_job("Instalacja", f"Instalacja silnika {providers.ENGINES[self.cfg.type]['name']}", 0.45)
            ctx = providers.InstallContext(self.dir, self.cfg.mc_version, self.cfg.loader_version, java, log)
            launch, loader = await providers.install(self.cfg.type, ctx)
            self.cfg.launch = launch
            self.cfg.loader_version = loader
            from . import lod, webmap
            if self.cfg.webmap.enabled and not webmap.engine_supported(self, self.cfg.webmap.provider):
                self.cfg.webmap.enabled = False
                log(f"Mapa {webmap.INFO[self.cfg.webmap.provider]['title']} wyłączona – nowy silnik jej nie obsługuje.")
            for key in ("distant_horizons", "voxy"):
                if getattr(self.cfg.lod, key) and not lod.supported(self, key):
                    setattr(self.cfg.lod, key, False)
                    log(f"{lod.jars(self)[key].title} wyłączony – nowy silnik go nie obsługuje.")
            from . import skins
            if self.cfg.skins_enabled and not skins.supported(self):
                self.cfg.skins_enabled = False
                log("SkinsRestorer wyłączony – nowy silnik go nie obsługuje.")

            if plan:
                self.set_job("Instalacja modpacka", "Pobieranie modów", 0.6)

                def progress(frac: float, stage: str) -> None:
                    self.set_job("Instalacja modpacka", stage, 0.6 + 0.38 * frac)

                await plan.apply(self.dir, log, progress)
                self.set_job("Instalacja modpacka", "Wyłączanie modów tylko dla klienta", 0.98)
                from . import clientmods
                try:
                    if plan.meta.get("server_pack"):
                        log("Oficjalna paczka serwerowa – pomijam automatyczne wyłączanie modów (autor przygotował ją pod serwer).")
                    else:
                        await clientmods.prune(self.dir, log)
                except Exception as exc:  # noqa: BLE001 – a failed check must not fail the install
                    log(f"Nie udało się sprawdzić modów klienckich: {exc}")
                self.set_job("Instalacja modpacka", "Sprawdzanie brakujących zależności", 0.99)
                try:
                    await clientmods.repair_dependencies(self.dir, self.cfg.type, self.cfg.mc_version, log)
                except Exception as exc:  # noqa: BLE001
                    log(f"Nie udało się sprawdzić zależności modów: {exc}")

            if eula:
                (self.dir / "eula.txt").write_text(
                    "# Zaakceptowano przez Varden (https://aka.ms/MinecraftEULA)\neula=true\n")
            self._enforce_properties(initial=True)
            self.ensure_default_icon()
            self.cfg.installed = True
            self.m.save_server(self.cfg)
            log(f"Instalacja zakończona w {time.time() - started:.0f} s. Serwer jest gotowy do uruchomienia.")
            self.set_job(None)
            self.set_state("offline")
        except Exception as exc:
            self.panel_log(f"BŁĄD instalacji: {exc}")
            logging.getLogger("varden").exception("install failed")
            self.set_job(None)
            self.set_state("offline" if self.cfg.installed else "install_failed")

    def icon_version(self) -> int:
        """Changes whenever server-icon.png changes – lets the browser drop a cached icon."""
        try:
            return (self.dir / "server-icon.png").stat().st_mtime_ns // 1_000_000
        except OSError:
            return 0

    def set_icon(self, data: bytes) -> None:
        """Replace server-icon.png with an already validated 64×64 PNG (atomic write)."""
        icon = self.dir / "server-icon.png"
        tmp = icon.with_name(".server-icon.png.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, icon)

    def remove_icon(self) -> None:
        (self.dir / "server-icon.png").unlink(missing_ok=True)

    def ensure_default_icon(self) -> bool:
        """Give a server without its own server-icon.png the Varden icon (64×64 PNG).

        An icon that is already there – uploaded, or shipped by a modpack – is never replaced."""
        icon = self.dir / "server-icon.png"
        if icon.exists() or not DEFAULT_ICON.is_file():
            return False
        try:
            shutil.copyfile(DEFAULT_ICON, icon)
        except OSError as exc:
            logging.getLogger("varden").warning("Could not write the default server icon: %s", exc)
            return False
        return True

    def _enforce_properties(self, initial: bool = False) -> None:
        path = self.dir / "server.properties"
        changes: dict[str, Any] = {"server-port": self.cfg.internal_port}
        props = read_properties(path)
        if initial:  # a world generator chosen at creation may already have written a few keys
            if "motd" not in props:
                changes["motd"] = f"{self.cfg.name}"
            if "enable-status" not in props:
                changes["enable-status"] = "true"
        # Players always come in through the Varden proxy (which talks to 127.0.0.1). Listening on
        # loopback only means nobody can reach the server port directly and skip the proxy's
        # /login check (e.g. with host networking or a bare-metal install).
        if props.get("server-ip") != BACKEND_HOST:
            changes["server-ip"] = BACKEND_HOST
        if props.get("query.port") == "25565" or (initial and "query.port" not in props):
            changes["query.port"] = self.cfg.internal_port
        update_properties(path, changes)
        if self.cfg.voice_enabled:
            from . import voice
            voice.write_config(self, VOICE_PORT)

    def _custom_summary(self) -> dict:
        try:
            jar = self.custom_jar()
        except ServerError:
            jar = None
        return {"custom_jar": jar, "custom_jars": self.custom_jars()}

    def custom_jars(self) -> list[str]:
        """Server JARs a "Custom JAR" server can start: *.jar files in the server's root folder."""
        try:
            return sorted(p.name for p in self.dir.glob("*.jar") if p.is_file())
        except OSError:
            return []

    def custom_jar(self) -> str:
        """The JAR to start: the one picked in the panel, or the only JAR in the folder."""
        jars = self.custom_jars()
        chosen = self.cfg.launch.target if self.cfg.launch else ""
        if chosen in jars:
            return chosen
        if len(jars) == 1:
            return jars[0]
        if not jars:
            raise ServerError("Wgraj plik .jar silnika do głównego folderu serwera (zakładka Pliki)")
        raise ServerError(f"W folderze serwera jest kilka plików .jar ({', '.join(jars)}) – wybierz silnik w zakładce Java")

    # ------------------------------------------------------------ lifecycle
    def build_command(self, java: Path | str, java_feature: int | None) -> list[str]:
        j = self.cfg.java
        if not self.cfg.launch:
            raise ServerError("Serwer nie jest zainstalowany")
        # Aikar: "set your Xms and Xmx to the same value" – a fixed heap, so AlwaysPreTouch and G1's
        # region sizing work on the whole heap and the GC never resizes it under load.
        xms = j.max_ram_mb if j.flags_preset == "aikar" else min(j.min_ram_mb, j.max_ram_mb)
        cmd = [str(java), f"-Xms{xms}M", f"-Xmx{j.max_ram_mb}M"]
        cmd += jvm_flags(j.flags_preset, j.max_ram_mb, java_feature)
        cmd += cpulimit.jvm_cpu_flags(j.cpu_limit_percent)
        cmd += ["-Dterminal.jline=false", "-Dterminal.ansi=true", "-Dlog4j2.formatMsgNoLookups=true",
                "-Dfile.encoding=UTF-8", "-Djava.awt.headless=true"]
        try:
            cmd += shlex.split(j.extra_jvm_args)
            extra_server = shlex.split(j.extra_server_args)
        except ValueError as exc:
            raise ServerError(f"Błędne dodatkowe argumenty: {exc}") from exc
        if self.cfg.type == "custom":
            cmd += ["-jar", self.custom_jar()]
        elif self.cfg.launch.kind == "jar":
            cmd += ["-jar", self.cfg.launch.target]
        else:
            cmd.append(f"@{self.cfg.launch.target}")
        cmd.append("nogui")
        cmd += extra_server
        return cpulimit.wrap_command(cmd, j.cpu_limit_percent)

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            if self.state == "installing":
                raise ServerError("Trwa instalacja")
            if not self.cfg.installed:
                raise ServerError("Serwer nie jest zainstalowany")
            eula = self.dir / "eula.txt"
            if not eula.exists() or "eula=true" not in eula.read_text(errors="ignore").replace(" ", "").lower():
                raise ServerError("Nie zaakceptowano EULA Minecrafta (eula.txt)")
            if self.cfg.bedrock_enabled:
                from . import bedrock_compat
                try:
                    compat = await bedrock_compat.ensure(
                        self, getattr(self.m.bedrock, "geyser_java_version", None))
                    if not compat.get("supported"):
                        self.cfg.bedrock_enabled = False
                        self.m.save_server(self.cfg)
                        if self.m.bedrock:
                            self.m.bedrock.request_apply()
                        self.panel_log(f"Bedrock wyłączony: {compat.get('reason', 'brak zgodności')}")
                except Exception as exc:  # Bedrock must not prevent Java startup
                    self.cfg.bedrock_enabled = False
                    self.m.save_server(self.cfg)
                    if self.m.bedrock:
                        self.m.bedrock.request_apply()
                    self.panel_log(f"Bedrock wyłączony – nie udało się przygotować zgodności: {exc}")
            from . import lod, webmap
            try:
                if self.cfg.webmap.enabled and webmap.INFO[self.cfg.webmap.provider]["proxy"]:
                    self.m.ensure_map_port(self)
                result = await webmap.prepare(self)
                if result and result.get("updated"):
                    self.panel_log(f"{webmap.INFO[self.cfg.webmap.provider]['title']} zaktualizowany do {result.get('version')}.")
            except Exception as exc:  # the map must never prevent the server from starting
                self.panel_log(f"Mapa: nie udało się przygotować – {exc}")
            if self.cfg.lod.distant_horizons or self.cfg.lod.voxy or lod.DH_MOD.jars(self) or lod.DH_PLUGIN.jars(self) or lod.VOXY.jars(self) or lod.LEGACY_VSS.jars(self):
                try:
                    for key, r in (await lod.prepare(self)).items():
                        if r.get("updated"):
                            self.panel_log(f"{lod.jars(self)[key].title} zaktualizowany do {r.get('version')}.")
                except Exception as exc:  # noqa: BLE001 – LODs must never prevent the server from starting
                    self.panel_log(f"LOD: nie udało się przygotować – {exc}")
            from . import skins
            if self.cfg.skins_enabled or skins.SKINSRESTORER.jars(self):
                try:
                    r = await skins.prepare(self)
                    if r and r.get("updated"):
                        self.panel_log(f"SkinsRestorer zaktualizowany do {r.get('version')}.")
                except Exception as exc:  # noqa: BLE001 – skins must never prevent the server from starting
                    self.panel_log(f"SkinsRestorer: nie udało się przygotować – {exc}")
            from . import chunky
            if chunky.engine_supported(self) and chunky.installed_info(self):
                try:
                    await asyncio.to_thread(chunky.prepare, self)
                except Exception as exc:  # noqa: BLE001 – never block a start
                    self.panel_log(f"Chunky: nie udało się przygotować ustawień – {exc}")
            self.set_state("starting")
            try:
                self._enforce_properties()
                required = self.cfg.required_java or await providers.required_java(self.cfg.mc_version)
                feature, java = await javamod.ensure_java(self.cfg.java.version or required, self.panel_log)
                self.java_feature = feature
                cmd = self.build_command(java, feature)
                if self.cfg.java.cpu_limit_percent:
                    self.panel_log(
                        f"Uruchamianie serwera (Java {feature}, RAM {self.cfg.java.min_ram_mb}–{self.cfg.java.max_ram_mb} MB, "
                        f"limit CPU {self.cfg.java.cpu_limit_percent}%)…"
                    )
                else:
                    self.panel_log(f"Uruchamianie serwera (Java {feature}, RAM {self.cfg.java.min_ram_mb}–{self.cfg.java.max_ram_mb} MB)…")
                self.online.clear()
                self._stop_requested = False
                self.exit_code = None
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(self.dir),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    limit=1024 * 1024, start_new_session=True,
                    env=server_env(java),
                )
            except Exception as exc:
                self.panel_log(f"Nie udało się uruchomić: {exc}")
                self.set_state("crashed")
                raise ServerError(str(exc)) from exc
            self._cpu_limiter = None
            if self.cfg.java.cpu_limit_percent:
                try:
                    self._cpu_limiter = await cpulimit.attach(self.cfg.id, self.proc.pid, self.cfg.java.cpu_limit_percent)
                except Exception as exc:  # noqa: BLE001 – CPU limiter must never prevent server startup
                    self._cpu_limiter = None
                    self.panel_log(f"Limit CPU: nie udało się zastosować – {exc}")
            self.started_at = time.time()
            self._start_seq = self._seq
            vp = getattr(self.m, "voice", None)
            if vp is not None:
                vp.sync()
            try:
                self._ps = psutil.Process(self.proc.pid)
                self._ps.cpu_percent(None)
            except psutil.Error:
                self._ps = None
            self.stats.clear()
            reader = self.spawn(self._read_output(self.proc))
            self.spawn(self._wait_exit(self.proc, reader))
            from . import content  # local import: content depends on manager types
            self.spawn(content.warn_incompatible(self))

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:  # extremely long line – read a chunk instead
                raw = await proc.stdout.read(65536)
            if not raw:
                break
            line = CTRL_RE.sub("", raw.decode("utf-8", "replace").rstrip("\n"))
            while line.startswith("> "):
                line = line[2:]
            plain = ANSI_RE.sub("", line).lstrip("\ufeff")
            while plain.startswith("> "):
                plain = plain[2:]
            if not plain.strip() or plain.strip() == ">":
                continue
            self._append(line, "out")
            self._parse_line(plain)

    def _parse_line(self, plain: str) -> None:
        # Text a player typed must never be read as a server event: a chat line saying
        # "There are 9 of a max of 20 players online: …" or "Saved the game" would otherwise
        # change the player list, finish a backup's save wait or confuse map/Chunky progress.
        if chat := _parse_chat_line(plain, self._known_player_names()):
            self._add_chat(chat[0], chat[1], live=True)
            return
        if _is_emote_line(plain):
            return
        for pattern, fut in list(self._waiters):
            if not fut.done() and pattern.search(plain):
                fut.set_result(True)
        if self.state == "starting" and DONE_RE.search(plain):
            self.set_state("online")
            self._autofix_count = 0
            self.empty_since = time.time()
            self.panel_log(f"Serwer działa – adres: {self.address()}")
            if self.cfg.idle_shutdown_minutes:
                self.panel_log(f"Auto-wyłączanie: serwer zatrzyma się po {self.cfg.idle_shutdown_minutes} min bez graczy.")
            if self.cfg.webmap.enabled and self.cfg.webmap.provider == "bluemap":
                from . import webmap
                self.spawn(webmap.on_online(self))
            return
        if m := UUID_RE.search(plain):
            self._remember(m.group(1), m.group(2))
        if m := (JOIN_RE.search(plain) or LOGIN_RE.search(plain)):
            name = m.group(1)
            if not self._is_online(name):
                self.online[name] = time.time()
                self._players_changed()
            self._remember(name, None)
        elif m := (LEAVE_RE.search(plain) or LOST_RE.search(plain)):
            name = m.group(1)
            if self._drop_online(name):
                self._touch_known([name])
                self._players_changed()
        elif m := LIST_RE.search(plain):
            names = [n.strip() for n in m.group(3).split(",") if n.strip()]
            self.online = {n: self.online.get(n, time.time()) for n in names if playersmod.NAME_RE.match(n)}
            self._players_changed()
        elif "white" in plain.lower():
            for r in WHITELIST_DENY_RES:
                if m := r.search(plain):
                    self.record_attempt(m.group(1), None, "log")
                    break
        elif "Task " in plain or "No tasks" in plain:
            self.chunky_runner.on_line(plain)
        elif (
            (mr := getattr(self, "_map_render", None)) is not None
            and (mr.running or "render" in plain.lower() or "Chunks:" in plain or "tiles rendered" in plain)
        ) or (
            (wm := getattr(getattr(self, "cfg", None), "webmap", None)) is not None
            and wm.enabled
            and (
                "render" in plain.lower()
                or "Chunks:" in plain
                or "tiles rendered" in plain
                or "currently being updated" in plain
                or "currently being purged" in plain
                or "progress:" in plain
            )
        ):
            self.map_render.on_line(plain)

    def _add_chat(self, name: str, text: str, at: float | None = None, *, live: bool = False) -> None:
        name = MC_CODE_RE.sub("", name).strip()[:40]
        text = MC_CODE_RE.sub("", text).strip()[:500]
        if not name or not text:
            return
        ts = at if at is not None else time.time()
        self._chat_seq += 1
        self.chat.append({"seq": self._chat_seq, "t": ts, "name": name, "text": text})
        if live:
            pending = getattr(self, "_chat_pending_live", None)
            if pending is None:
                pending = deque(maxlen=CHAT_BUFFER * 2)
                self._chat_pending_live = pending
            pending.append((ts, name, text))

    def _is_online(self, name: str) -> bool:
        key = name.lower()
        return any(n.lower() == key for n in self.online)

    def _drop_online(self, name: str) -> bool:
        key = name.lower()
        found = [n for n in self.online if n.lower() == key]
        for n in found:
            del self.online[n]
        return bool(found)

    # The Varden proxy sees every game connection (Java and Bedrock through Geyser), so it is the
    # reliable source of who is online: join/leave log lines can be renamed or switched off by plugins,
    # and Bedrock names (".Steve", spaces) never match the log patterns.
    PROXY_PLAYED = 15  # seconds a connection must last to count as "played" (not a whitelist kick)

    def proxy_player_joined(self, name: str) -> None:
        key = name.lower()
        entry = self._proxy_players.setdefault(key, [name, 0, time.time()])
        entry[1] += 1
        if not self._is_online(name):
            self.online[name] = time.time()
            self._players_changed()

    def proxy_player_left(self, name: str) -> None:
        key = name.lower()
        entry = self._proxy_players.get(key)
        if not entry:
            return
        entry[1] -= 1
        if entry[1] > 0:  # a reconnect is already on its way in
            return
        del self._proxy_players[key]
        if self._drop_online(name):
            if time.time() - entry[2] >= self.PROXY_PLAYED:
                self._touch_known([name])
            self._players_changed()

    def _players_changed(self) -> None:
        if self.online:
            self.empty_since = None
        elif self.empty_since is None:
            self.empty_since = time.time()
        self.broadcast({"type": "players", "players": sorted(self.online), "count": len(self.online)})
        if self.online and self._chunky is not None:
            self._chunky.on_players(len(self.online))

    def _remember(self, name: str, uid: str | None) -> None:
        data = self.players_store.data
        known = data.setdefault("known", {})
        k = name.lower()
        entry = known.get(k, {"name": name})
        entry["name"] = name
        entry["last_seen"] = time.time()
        if uid:
            entry["uuid"] = uid
            canonical = playerstats.norm_uuid(uid)
            if canonical:  # a premium player may have changed their nickname
                for old_key, old in list(known.items()):
                    if old_key != k and playerstats.norm_uuid(old.get("uuid")) == canonical:
                        entry["last_seen"] = max(entry["last_seen"], old.get("last_seen", 0))
                        known.pop(old_key, None)
        known[k] = entry
        self.players_store.save()
        self.bind_voice(name, entry.get("uuid"))

    def _touch_known(self, names: list[str]) -> None:
        """Refresh last_seen in one write (leaving the game / server shutdown)."""
        data = self.players_store.data
        known = data.setdefault("known", {})
        now = time.time()
        changed = False
        for name in names:
            if not playersmod.NAME_RE.match(name):
                continue
            k = name.lower()
            entry = known.get(k, {"name": name})
            entry["name"] = name
            entry["last_seen"] = now
            known[k] = entry
            changed = True
        if changed:
            self.players_store.save()

    def bind_voice(self, name: str, uid: str | None) -> None:
        """Tell the voice proxy which server this player is on (UUID is in every SVC packet)."""
        vp = getattr(self.m, "voice", None)
        if not vp or not self.cfg.voice_enabled:
            return
        if not self.online_mode:
            uid = playersmod.offline_uuid(name)
        vp.bind(uid, self.cfg.id)

    def record_attempt(self, name: str, uid: str | None, source: str) -> None:
        if not playersmod.NAME_RE.match(name):
            return
        data = self.players_store.data
        att = data.setdefault("attempts", {})
        e = att.get(name.lower(), {"name": name, "count": 0})
        e.update({"name": name, "ts": time.time(), "source": source, "count": e.get("count", 0) + 1})
        if uid:
            e["uuid"] = uid
        att[name.lower()] = e
        # keep the 200 most recent attempts
        if len(att) > 200:
            for k, _ in sorted(att.items(), key=lambda kv: kv[1].get("ts", 0))[: len(att) - 200]:
                att.pop(k, None)
        self.players_store.save()
        self.broadcast({"type": "attempt", "name": name})

    async def _release_cpu_limiter(self, *, clear: bool = False) -> None:
        limiter = getattr(self, "_cpu_limiter", None)
        if limiter is None:
            return
        with contextlib.suppress(Exception):
            await limiter.release()
        if clear or not limiter.has_pending_cleanup():
            self._cpu_limiter = None

    async def _wait_exit(self, proc: asyncio.subprocess.Process, reader: asyncio.Task) -> None:
        code = await proc.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader, 5)
        await self._release_cpu_limiter(clear=True)
        self.exit_code = code
        with contextlib.suppress(Exception):
            self.refresh_chat_history()
        if self._map_render is not None:
            self._map_render.on_stop()
        if self._chunky is not None:
            self._chunky.on_stop()
        self._touch_known(list(self.online))
        self.online.clear()
        self._proxy_players.clear()
        self._players_changed()
        self._ps = None
        uptime = time.time() - (self.started_at or time.time())
        if self._stop_requested:
            self.panel_log(f"Serwer zatrzymany (kod {code}).")
            self.set_state("offline")
            return
        self.panel_log(f"Serwer zakończył działanie nieoczekiwanie (kod {code}) po {uptime:.0f} s.")
        self.set_state("crashed")
        if await self._auto_fix_client_mod() or await self._auto_fix_missing_deps():
            return
        now = time.time()
        self._crashes.append(now)
        recent = [t for t in self._crashes if now - t < 600]
        if self.cfg.auto_restart and len(recent) <= 3 and uptime > 15:
            delay = 5 * len(recent)
            self.panel_log(f"Automatyczny restart za {delay} s (próba {len(recent)}/3)…")
            await asyncio.sleep(delay)
            if self.state == "crashed":
                with contextlib.suppress(Exception):
                    await self.start()
        elif self.cfg.auto_restart:
            self.panel_log("Automatyczny restart wstrzymany – zbyt wiele awarii lub awaria przy starcie. Sprawdź konsolę.")

    async def _auto_fix_client_mod(self) -> bool:
        """If a client-only mod crashed the startup, disable it and start again (bounded)."""
        from . import clientmods
        lines = [e["text"] for e in self.console if e["seq"] > self._start_seq and e["src"] == "out"]
        jar = await asyncio.to_thread(clientmods.find_crashing_client_mod, [ANSI_RE.sub("", l) for l in lines], self.dir)
        if not jar or self._autofix_count >= 10:
            return False
        self._autofix_count += 1
        await asyncio.to_thread(clientmods.disable, self.dir, jar, "crash")
        self.panel_log(f"Mod {jar.name} działa tylko po stronie klienta i wysypał serwer – został wyłączony "
                       "(możesz go włączyć z powrotem w zakładce Mody). Ponowne uruchamianie…")
        await asyncio.sleep(2)
        if self.state == "crashed":
            with contextlib.suppress(Exception):
                await self.start()
        return True

    async def _auto_fix_missing_deps(self) -> bool:
        """If startup died on a missing class, install required libraries the modpack forgot (bounded)."""
        from . import clientmods
        if self._autofix_count >= 10 or self.cfg.type not in ("fabric", "quilt", "forge", "neoforge"):
            return False
        lines = [ANSI_RE.sub("", e["text"]) for e in self.console if e["seq"] > self._start_seq and e["src"] == "out"]
        if not clientmods.MISSING_CLASS.search("\n".join(lines[-600:])):
            return False
        self.panel_log("Serwer nie znalazł wymaganej biblioteki – sprawdzam brakujące zależności modów…")
        try:
            added = await clientmods.repair_dependencies(self.dir, self.cfg.type, self.cfg.mc_version, self.panel_log)
        except Exception as exc:  # noqa: BLE001
            self.panel_log(f"Nie udało się sprawdzić zależności: {exc}")
            return False
        if not added:
            self.panel_log("Nie znaleziono brakujących zależności do doinstalowania – sprawdź konsolę.")
            return False
        self._autofix_count += 1
        self.panel_log("Ponowne uruchamianie…")
        await asyncio.sleep(2)
        if self.state == "crashed":
            with contextlib.suppress(Exception):
                await self.start()
        return True

    async def send_command(self, command: str, echo: bool = True) -> None:
        command = command.strip().lstrip("/")
        if not command:
            return
        if not self.running or not self.proc or not self.proc.stdin:
            raise ServerError("Serwer nie działa")
        if echo:
            self._append(command, "cmd")
        self.proc.stdin.write((command + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def stop(self, timeout: float = 90) -> None:
        async with self._lock:
            if not self.running or not self.proc:
                if self.state in ("crashed", "starting"):
                    self.set_state("offline")
                return
            proc = self.proc
            self._stop_requested = True
            self.set_state("stopping")
            self.panel_log("Zatrzymywanie serwera…")
            await self._release_cpu_limiter()
            try:
                await self.send_command("stop", echo=False)
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                self.panel_log("Serwer nie odpowiada – wysyłanie SIGTERM…")
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 15)
                except asyncio.TimeoutError:
                    self.panel_log("Wymuszanie zakończenia (SIGKILL)…")
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            await asyncio.sleep(0.2)

    async def kill(self) -> None:
        if self.running and self.proc:
            self._stop_requested = True
            self.panel_log("Wymuszone zatrzymanie (kill).")
            await self._release_cpu_limiter()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else self.proc.kill()
            await self.proc.wait()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def request_start(self) -> bool:
        """Called by the proxy for start-on-join."""
        if self.cfg.start_on_join and self.cfg.installed and self.state in ("offline", "crashed"):
            self.panel_log("Gracz próbuje dołączyć – uruchamiam serwer (start on join).")
            self.spawn(self.start())
            return True
        return False

    # ------------------------------------------------------------ metrics
    def sample(self) -> None:
        if not self.running or not self._ps:
            return
        try:
            with self._ps.oneshot():
                cpu = self._ps.cpu_percent(None)
                mem = self._ps.memory_info().rss
            for child in self._ps.children(recursive=True):
                with contextlib.suppress(psutil.Error):
                    mem += child.memory_info().rss
        except psutil.Error:
            return
        point = {"t": time.time(), "cpu": round(cpu, 1), "mem": mem}
        self.stats.append(point)
        self.broadcast({"type": "stats", "point": point, "players": len(self.online)})


class Manager:
    def __init__(self) -> None:
        self.servers_store = JsonStore(PANEL_DIR / "servers.json", dict)
        self.settings_store = JsonStore(PANEL_DIR / "settings.json", dict)
        self.instances: dict[str, ServerInstance] = {}
        self._tasks: list[asyncio.Task] = []
        self.backups = None  # set in main
        self.proxy = None
        self.voice = None
        self.bedrock = None
        self.auth_lobby = None
        for sid, raw in self.servers_store.data.items():
            try:
                cfg = ServerConfig.model_validate(raw)
                self.instances[sid] = ServerInstance(self, cfg)
            except Exception:
                log.exception("Invalid server config %s – skipped", sid)

    # ------------------------------------------------------------ settings
    @property
    def settings(self) -> PanelSettings:
        return PanelSettings.model_validate(self.settings_store.data or {})

    def migrate_setup_state(self, has_users: bool) -> None:
        """Panels set up before the first-run wizard existed skip its panel steps."""
        data = self.settings_store.data or {}
        if has_users and "setup_completed" not in data:
            s = self.settings
            s.setup_completed = True
            self.save_settings(s)

    def save_settings(self, s: PanelSettings) -> None:
        self.settings_store.save(s.model_dump())

    def save_server(self, cfg: ServerConfig) -> None:
        self.servers_store.data[cfg.id] = cfg.model_dump()
        self.servers_store.save()

    # ------------------------------------------------------------ CRUD
    def get(self, sid: str) -> ServerInstance:
        inst = self.instances.get(sid)
        if not inst:
            raise KeyError(sid)
        return inst

    def allocate_voice_port(self) -> int:
        used = {i.cfg.voice_port for i in self.instances.values() if i.cfg.voice_port}
        port = INTERNAL_VOICE_PORT_START
        while port in used or port == VOICE_PORT:
            port += 1
        return port

    def ensure_map_port(self, inst: "ServerInstance") -> int:
        """Loopback port for the web server of Dynmap/BlueMap (proxied by the panel under /map/<id>/).

        A copied or imported server may carry another server's port, so a taken port is replaced."""
        used = {i.cfg.webmap.port for i in self.instances.values() if i is not inst and i.cfg.webmap.port}
        port = inst.cfg.webmap.port
        if not port or port in used:
            port = INTERNAL_MAP_PORT_START
            while port in used:
                port += 1
            inst.cfg.webmap.port = port
            self.save_server(inst.cfg)
        return port

    def allocate_port(self) -> int:
        used = {i.cfg.internal_port for i in self.instances.values()}
        port = INTERNAL_PORT_START
        while port in used or port == PROXY_PORT:
            port += 1
        return port

    def check_unique(self, subdomain: str, aliases: list[str], exclude: str | None = None) -> None:
        for i in self.instances.values():
            if i.cfg.id == exclude:
                continue
            if i.cfg.subdomain == subdomain:
                raise ServerError(f"Subdomena „{subdomain}” jest już zajęta przez {i.cfg.name}")
            clash = set(aliases) & set(i.cfg.aliases)
            if clash:
                raise ServerError(f"Host {', '.join(clash)} jest już używany przez {i.cfg.name}")

    def create(self, *, name: str, type: str, mc_version: str, loader_version: str | None, subdomain: str,
               java: dict | None = None, modpack: dict | None = None) -> ServerInstance:
        sid = uuid.uuid4().hex[:10]
        cfg = ServerConfig(id=sid, name=name.strip() or "Serwer", type=type, mc_version=mc_version,
                           loader_version=loader_version, subdomain=subdomain,
                           internal_port=self.allocate_port(), java=java or {}, modpack=modpack)
        self.check_unique(cfg.subdomain, cfg.aliases)
        self.save_server(cfg)
        inst = ServerInstance(self, cfg)
        self.instances[sid] = inst
        # The default server (for bare IPs and unknown hostnames) is only ever chosen by the owner.
        return inst

    async def delete(self, sid: str, delete_backups: bool = True) -> None:
        inst = self.get(sid)
        if inst.state == "installing":
            raise ServerError("Poczekaj na zakończenie instalacji")
        if inst.running:
            await inst.stop()
        self.instances.pop(sid, None)
        self.servers_store.data.pop(sid, None)
        self.servers_store.save()
        if self.bedrock:
            self.bedrock.request_apply()
        if self.voice:
            self.voice.sync()
        await asyncio.to_thread(shutil.rmtree, inst.dir, True)
        (PANEL_DIR / "players" / f"{sid}.json").unlink(missing_ok=True)
        if delete_backups and self.backups:
            await asyncio.to_thread(self.backups.delete_all, sid)
        s = self.settings
        if s.default_server_id == sid:  # no other server is picked automatically
            s.default_server_id = None
            self.save_settings(s)

    # ------------------------------------------------------------ routing
    def resolve_host(self, host: str) -> Optional[ServerInstance]:
        host = host.strip().lower().rstrip(".")
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
        s = self.settings
        for inst in self.instances.values():
            if host in inst.cfg.aliases:
                return inst
        sub = None
        # The longest matching main domain wins (mc.example.com before example.com).
        domains = sorted(s.domains(), key=len, reverse=True)
        root = next((d for d in domains if host == d or host.endswith("." + d)), None)
        if root:
            sub = host[: -len(root) - 1] if host != root else None
        elif "." in host and not re.match(r"^[\d.]+$", host):
            sub = host.split(".", 1)[0]
        if sub:
            for inst in self.instances.values():
                if inst.cfg.subdomain == sub:
                    return inst
        if s.default_server_id:
            return self.instances.get(s.default_server_id)
        return None

    # ------------------------------------------------------------ background
    async def startup(self) -> None:
        for inst in self.instances.values():
            if inst.cfg.installed and inst.cfg.auto_start:
                inst.spawn(self._safe_start(inst))
        self._tasks.append(asyncio.create_task(self._sampler()))
        self._tasks.append(asyncio.create_task(self._map_ticker()))
        self._tasks.append(asyncio.create_task(self._scheduler()))

    async def _safe_start(self, inst: ServerInstance) -> None:
        try:
            await inst.start()
        except Exception as exc:
            inst.panel_log(f"Autostart nieudany: {exc}")

    async def _sampler(self) -> None:
        while True:
            await asyncio.sleep(2)
            for inst in list(self.instances.values()):
                try:
                    inst.sample()
                except Exception:
                    log.exception("sample failed")

    async def _map_ticker(self) -> None:
        """Poll map render progress (BlueMap only answers /bluemap, the others log progress)."""
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for inst in list(self.instances.values()):
                if inst._map_render is None:
                    continue
                try:
                    inst._map_render.tick(now)
                except Exception:
                    log.exception("map render tick failed for %s", inst.cfg.id)

    async def _scheduler(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            hhmm = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            for inst in list(self.instances.values()):
                try:
                    b = inst.cfg.backup
                    if (b.enabled and inst.cfg.installed and inst.state != "installing" and self.backups
                            and not self.backups.busy(inst.cfg.id)
                            and now - (inst.cfg.last_backup_at or inst.cfg.created_at) >= b.interval_hours * 3600):
                        inst.spawn(self.backups.create(inst, note="Automatyczna kopia", auto=True))
                    if inst._map_render is not None:
                        inst._map_render.tick(now)
                    if inst.state == "online" and inst.chunky_runner.active:
                        inst.chunky_runner.tick(now)
                    idle = inst.cfg.idle_shutdown_minutes
                    proxy_conns = self.proxy.per_server.get(inst.cfg.id, 0) if self.proxy else 0
                    generating = inst.is_generating()
                    if generating and not inst.online and not proxy_conns and inst.empty_since is not None:
                        inst.empty_since = max(inst.empty_since, now - 60)
                    if (idle and inst.state == "online" and not inst.online and not proxy_conns
                            and inst.empty_since and now - inst.empty_since >= idle * 60
                            and not generating
                            and not (self.backups and self.backups.busy(inst.cfg.id))):
                        inst.empty_since = None
                        inst.panel_log(f"Brak graczy od {idle} min – automatyczne wyłączanie serwera."
                                       + (" Uruchomi się sam, gdy ktoś spróbuje dołączyć." if inst.cfg.start_on_join else ""))
                        inst.spawn(inst.stop())
                    if (inst.cfg.restart_daily_at == hhmm and inst.state == "online"
                            and inst._last_daily_restart != today):
                        inst._last_daily_restart = today
                        inst.spawn(self._daily_restart(inst))
                except Exception:
                    log.exception("scheduler failed for %s", inst.cfg.id)

    async def _daily_restart(self, inst: ServerInstance) -> None:
        with contextlib.suppress(Exception):
            await inst.send_command("say Planowany restart serwera za 30 sekund.", echo=False)
            await asyncio.sleep(20)
            await inst.send_command("say Restart za 10 sekund…", echo=False)
            await asyncio.sleep(10)
        inst.panel_log("Planowany codzienny restart.")
        with contextlib.suppress(Exception):
            await inst.restart()

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        running = [i for i in self.instances.values() if i.running]
        if running:
            log.info("Stopping %d server(s)…", len(running))
            await asyncio.gather(*(i.stop(timeout=60) for i in running), return_exceptions=True)
