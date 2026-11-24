"""Minecraft-aware reverse proxy control plane and Rust data-plane supervisor.

The high-throughput TCP (:25565) and Simple Voice Chat UDP (:24454) transport runs in the native
``minenode-proxy`` Rust binary. Python keeps all routing, authentication, whitelist, start-on-join,
resource-pack token verification, MOTD rendering, and passive client-fingerprint logic, communicating
over an authenticated newline-delimited JSON IPC channel (Unix socket on Linux/macOS, loopback TCP
on Windows).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import PROXY_BIN, VOICE_HOST, VOICE_PORT
from .fingerprint import CAPTURE_SECONDS

if TYPE_CHECKING:
    from .fingerprint import PacketSniffer
    from .manager import Manager, ServerInstance

log = logging.getLogger("minenode.proxy")

FORMATTING_RE = re.compile("\u00a7.")
_RP_PATH_RE = re.compile(r"^/rp/([0-9a-f]{6,32})/([A-Za-z0-9_-]{8,64})/([0-9a-f]{12})\.zip(\?.*)?$")
_SNIFF_MAX_BYTES = 512 * 1024
_IPC_MAX_LINE = 1024 * 1024


def plain_text(text: str) -> str:
    """Drop Minecraft formatting codes: Bedrock kick screens would show them literally."""
    return FORMATTING_RE.sub("", text)


def safe_player_name(name: str | None) -> bool:
    """A login name that is safe to store in the tab-separated auth files and to route on.

    Tabs or line breaks in a name would add fake rows to sessions.db / pending.db (and with them a
    session for somebody else), so such names are refused before anything is written."""
    return bool(name) and len(name) <= 16 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name)


def _same_ip(a: str, b: str) -> bool:
    from .auth_sessions import _norm_ip
    return _norm_ip(a) == _norm_ip(b)


class LobbyGate:
    """Who may enter the hidden /login lobby under which name, and how often.

    * One owner per name: PicoLimbo reads the client IP for /login and /register from pending.db,
      which is keyed by name. While somebody is in the lobby as "Steve", a connection as "Steve"
      from another address is refused – otherwise it would replace the pending IP and the real
      Steve's /login would open a session for the attacker's address.
    * Failed visits (left the lobby without a valid session) are limited per IP and per name, so
      passwords cannot be guessed endlessly. The name limit never locks out the address the
      account last logged in from.
    """
    WINDOW = 15 * 60
    IP_FAILS = 10
    NAME_FAILS = 20
    LOGIN_TIMEOUT = 3 * 60  # seconds a player may stay in the lobby without logging in
    _MAX_TRACKED = 10_000

    def __init__(self) -> None:
        self.owners: dict[str, tuple[str, int]] = {}  # lowercase name -> (ip, open connections)
        self.fails: dict[str, deque] = {}

    def _recent(self, key: str, now: float) -> deque:
        q = self.fails.get(key)
        if q is None:
            if len(self.fails) >= self._MAX_TRACKED:
                for k in sorted(self.fails, key=lambda k: self.fails[k][-1] if self.fails[k] else 0)[: self._MAX_TRACKED // 2]:
                    del self.fails[k]
            q = self.fails[key] = deque()
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        return q

    def admit(self, name: str, ip: str, account_ip: str | None = None) -> str | None:
        """Reserve the lobby for this name and address; returns a kick message when refused."""
        key, now = name.lower(), time.time()
        if len(self._recent(f"ip:{ip}", now)) >= self.IP_FAILS or (
                len(self._recent(f"name:{key}", now)) >= self.NAME_FAILS
                and not (account_ip and _same_ip(account_ip, ip))):
            return "Zbyt wiele nieudanych prób logowania. Spróbuj ponownie za kilka minut."
        owner = self.owners.get(key)
        if owner and not _same_ip(owner[0], ip):
            return "Ktoś inny loguje się teraz na ten nick. Spróbuj ponownie za chwilę."
        self.owners[key] = (ip, (owner[1] if owner else 0) + 1)
        return None

    def release(self, name: str, ip: str, *, failed: bool = False) -> None:
        key = name.lower()
        owner = self.owners.get(key)
        if owner and _same_ip(owner[0], ip):
            if owner[1] <= 1:
                del self.owners[key]
            else:
                self.owners[key] = (owner[0], owner[1] - 1)
        if failed:
            now = time.time()
            self._recent(f"ip:{ip}", now).append(now)
            self._recent(f"name:{key}", now).append(now)


@dataclass
class _ConnState:
    conn_id: int
    inst: "ServerInstance"
    player: str | None
    track: bool
    auth_name: str | None
    auth_ip: str | None
    protocol: int
    edition: str
    hostname: str
    next_state: int
    is_lobby: bool
    sniffer: "PacketSniffer | None" = None
    capture_timer: asyncio.TimerHandle | None = None
    opened: bool = False


class MinecraftProxy:
    def __init__(
        self,
        manager: "Manager",
        host: str,
        port: int,
        voice_host: str | None = None,
        voice_port: int | None = None,
        *,
        binary: Path | None = None,
    ):
        self.m = manager
        self.host, self.port = host, port
        self.voice_host = voice_host if voice_host is not None else (VOICE_HOST or host)
        self.voice_port = voice_port if voice_port is not None else VOICE_PORT
        self.binary = binary or PROXY_BIN

        self.listening = False
        self.error: str | None = None
        self.active = 0
        self.total = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.http_served = 0
        self.per_server: dict[str, int] = {}
        self.started_at = time.time()
        self._pings: dict[str, dict[str, int | None]] = {}

        self.lobby_gate = LobbyGate()
        self._conns: dict[int, _ConnState] = {}

        self._ipc_server: asyncio.AbstractServer | None = None
        self._ipc_path: Path | None = None
        self._ipc_addr: str | None = None
        self._ipc_token = ""
        self._ipc_writer: asyncio.StreamWriter | None = None
        self._ipc_tasks: set[asyncio.Task] = set()
        self._proc: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
        self._stopping = False
        self._ready_event: asyncio.Event | None = None
        self._restart_delay = 1.0

    def player_pings(self, sid: str) -> dict[str, int | None]:
        """Network round-trip time (ms) of each player's connection, reported by the Rust proxy."""
        return dict(self._pings.get(sid, {}))

    def stats(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "active": self.active,
            "total": self.total,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "per_server": dict(self.per_server),
            "listening": self.listening,
            "error": self.error,
            "packs_served": self.http_served,
        }

    def _resolve_binary(self) -> Path | None:
        if self.binary.is_file():
            return self.binary
        if sys.platform == "win32":
            exe = self.binary.with_suffix(".exe")
            if exe.is_file():
                return exe
        return None

    async def start(self) -> None:
        self._stopping = False
        try:
            await self._start_ipc()
        except OSError as exc:
            self.error = f"Nie można uruchomić IPC proxy: {exc}"
            log.error("Proxy IPC bind failed: %s", exc)
            return

        bin_path = self._resolve_binary()
        if bin_path is None:
            self.listening = False
            self.error = f"Brak binarki proxy: {self.binary}"
            vp = getattr(self.m, "voice", None)
            if vp is not None:
                vp.listening = False
                vp.error = self.error
            log.error("Rust proxy binary not found at %s", self.binary)
            return

        try:
            await self._spawn_proc(bin_path)
            if self._ready_event is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._ready_event.wait(), 5)
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = asyncio.create_task(self._watch(), name="minenode-proxy-watch")
        except Exception as exc:  # noqa: BLE001
            self.listening = False
            self.error = str(exc)
            log.error("Failed to start minenode-proxy: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

        if self._ipc_writer is not None:
            self.send_command({"t": "shutdown"})
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._ipc_writer.drain(), 1)

        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()

        if self._log_task:
            self._log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_task
            self._log_task = None

        await self._close_ipc()
        self._cleanup_all_conns()
        self.listening = False
        self._pings.clear()

    async def _start_ipc(self) -> None:
        if self._ipc_server is not None:
            return
        self._ipc_token = secrets.token_hex(32)
        if sys.platform != "win32":
            sock_path = Path(tempfile.gettempdir()) / f"minenode-proxy-{os.getpid()}-{secrets.token_hex(6)}.sock"
            sock_path.unlink(missing_ok=True)
            self._ipc_path = sock_path
            self._ipc_addr = f"unix:{sock_path}"
            self._ipc_server = await asyncio.start_unix_server(
                self._accept_ipc, path=str(sock_path), limit=_IPC_MAX_LINE
            )
            os.chmod(sock_path, 0o600)
        else:
            self._ipc_server = await asyncio.start_server(
                self._accept_ipc, "127.0.0.1", 0, limit=_IPC_MAX_LINE
            )
            port = self._ipc_server.sockets[0].getsockname()[1]
            self._ipc_addr = f"tcp:127.0.0.1:{port}"

    async def _close_ipc(self) -> None:
        for task in list(self._ipc_tasks):
            task.cancel()
        self._ipc_tasks.clear()
        writer, self._ipc_writer = self._ipc_writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1)
        srv, self._ipc_server = self._ipc_server, None
        if srv is not None:
            srv.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(srv.wait_closed(), 2)
        if self._ipc_path is not None:
            with contextlib.suppress(OSError):
                self._ipc_path.unlink(missing_ok=True)
            self._ipc_path = None

    async def _spawn_proc(self, bin_path: Path) -> None:
        self._ready_event = asyncio.Event()
        env = os.environ.copy()
        env["MINENODE_IPC_ADDR"] = self._ipc_addr or ""
        env["MINENODE_IPC_TOKEN"] = self._ipc_token
        env["MINENODE_TCP_BIND"] = f"{self.host}:{self.port}"
        env["MINENODE_UDP_BIND"] = f"{self.voice_host}:{self.voice_port}"
        self._proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if self._log_task:
            self._log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_task
        self._log_task = asyncio.create_task(self._read_proc_output(self._proc), name="minenode-proxy-log")

    async def _read_proc_output(self, proc: asyncio.subprocess.Process) -> None:
        if not proc.stdout:
            return
        try:
            async for raw in proc.stdout:
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    log.info("[minenode-proxy] %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Reading minenode-proxy output failed")

    async def _watch(self) -> None:
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            rc = await proc.wait()
            if self._stopping or self._proc is not proc:
                return
            self._proc = None
            self.listening = False
            self.error = f"Proces proxy zakończył pracę (kod {rc}) – ponawianie za {self._restart_delay:.0f} s"
            vp = getattr(self.m, "voice", None)
            if vp is not None:
                vp.listening = False
                vp.error = self.error
            self._cleanup_all_conns()
            await asyncio.sleep(self._restart_delay)
            self._restart_delay = min(10.0, self._restart_delay * 2)
            if self._stopping:
                return
            bin_path = self._resolve_binary()
            if bin_path is None:
                return
            try:
                await self._spawn_proc(bin_path)
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed to restart minenode-proxy: %s", exc)

    def send_command(self, msg: dict[str, Any]) -> bool:
        writer = self._ipc_writer
        if writer is None or writer.is_closing():
            return False
        try:
            payload = (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            writer.write(payload)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def disconnect(self, conn_id: int) -> None:
        self.send_command({"t": "conn_control", "conn_id": conn_id, "op": "disconnect"})

    def stop_sniff(self, conn_id: int) -> None:
        self.send_command({"t": "conn_control", "conn_id": conn_id, "op": "stop_sniff"})

    async def _accept_ipc(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first_raw = await asyncio.wait_for(reader.readline(), 5)
            if not first_raw:
                return
            hello = json.loads(first_raw.decode("utf-8"))
            if (
                not isinstance(hello, dict)
                or hello.get("t") != "hello"
                or hello.get("version") != 1
                or not hmac.compare_digest(str(hello.get("token", "")), self._ipc_token)
            ):
                log.warning("Rejected unauthorized proxy IPC client")
                return
        except (asyncio.TimeoutError, UnicodeDecodeError, ValueError, OSError):
            with contextlib.suppress(Exception):
                writer.close()
            return

        old_writer, self._ipc_writer = self._ipc_writer, writer
        if old_writer is not None and old_writer is not writer:
            with contextlib.suppress(Exception):
                old_writer.close()
        self._restart_delay = 1.0

        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("t")
                if kind in ("mc_handshake", "mc_upstream_failed", "http_request"):
                    task = asyncio.create_task(self._dispatch_request(writer, msg))
                    self._ipc_tasks.add(task)
                    task.add_done_callback(self._ipc_tasks.discard)
                elif kind == "conn_opened":
                    self.handle_conn_opened(int(msg["conn_id"]))
                elif kind == "conn_closed":
                    self.handle_conn_closed(int(msg["conn_id"]))
                elif kind == "sniff_chunk":
                    data_b64 = msg.get("data_b64")
                    if isinstance(data_b64, str):
                        with contextlib.suppress(ValueError):
                            self.handle_sniff_chunk(int(msg["conn_id"]), base64.b64decode(data_b64))
                elif kind == "telemetry":
                    self.handle_telemetry(msg)
                elif kind == "ready":
                    self.handle_ready(msg)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            if self._ipc_writer is writer:
                self._ipc_writer = None
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            self._cleanup_all_conns()

    async def _dispatch_request(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        kind = msg.get("t")
        try:
            if kind == "mc_handshake":
                body = self.handle_mc_handshake(msg)
            elif kind == "mc_upstream_failed":
                body = self.handle_mc_upstream_failed(int(msg["conn_id"]))
            elif kind == "http_request":
                body = await asyncio.to_thread(
                    self.handle_http_request, str(msg.get("method", "")), str(msg.get("path", "/"))
                )
            else:
                return
        except Exception:  # noqa: BLE001
            log.exception("Error handling IPC request %s", kind)
            body = {"action": "close"} if kind != "http_request" else {
                "action": "body",
                "status": "500 Internal Server Error",
                "headers": [["Content-Type", "text/plain; charset=utf-8"], ["Content-Length", "0"]],
                "body_b64": "",
            }
        resp = {"t": "resp", "id": req_id, **body}
        if not writer.is_closing():
            with contextlib.suppress(OSError, RuntimeError, ConnectionError, ValueError):
                writer.write((json.dumps(resp, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()

    # ------------------------------------------------------------------ control-plane handlers

    def handle_ready(self, msg: dict[str, Any]) -> None:
        self.listening = bool(msg.get("tcp_listening", False))
        self.error = msg.get("tcp_error")
        if self.listening:
            log.info("Minecraft proxy listening on %s:%s", self.host, msg.get("tcp_port", self.port))
        elif self.error:
            log.error("Proxy could not bind %s:%s – %s", self.host, self.port, self.error)

        vp = getattr(self.m, "voice", None)
        if vp is not None:
            vp.apply_ready(msg)
            vp.sync(force=True)
        if self._ready_event is not None:
            self._ready_event.set()

    def handle_telemetry(self, msg: dict[str, Any]) -> None:
        self.listening = bool(msg.get("tcp_listening", self.listening))
        self.error = msg.get("tcp_error")
        self.active = int(msg.get("active", self.active))
        self.total = int(msg.get("total", self.total))
        self.bytes_up = int(msg.get("bytes_up", self.bytes_up))
        self.bytes_down = int(msg.get("bytes_down", self.bytes_down))
        self.http_served = int(msg.get("packs_served", self.http_served))
        per_server = msg.get("per_server")
        if isinstance(per_server, dict):
            self.per_server = {str(k): int(v) for k, v in per_server.items()}
        pings = msg.get("pings")
        if isinstance(pings, dict):
            self._pings = {
                str(sid): {str(p): (int(rtt) if rtt is not None else None) for p, rtt in players.items()}
                for sid, players in pings.items()
                if isinstance(players, dict)
            }
        vp = getattr(self.m, "voice", None)
        if vp is not None:
            vp.apply_telemetry(msg)

    def handle_mc_handshake(self, msg: dict[str, Any]) -> dict[str, Any]:
        conn_id = int(msg["conn_id"])
        peer_ip = str(msg.get("peer_ip") or "0.0.0.0")
        protocol = int(msg.get("protocol", -1))
        hostname = str(msg.get("hostname") or "").split("\x00", 1)[0].strip().rstrip(".").lower()
        next_state = int(msg.get("next_state", 0))
        player = msg.get("player")
        player_uuid = msg.get("player_uuid")
        edition = str(msg.get("edition") or "java")

        inst = self.m.resolve_host(hostname)
        if inst is None:
            return self._offline_response(next_state, "unknown", None, hostname)

        target: Any = inst
        auth_name: str | None = None
        auth_ip: str | None = None
        lobby = getattr(self.m, "auth_lobby", None)

        # Offline-mode servers use the hidden PicoLimbo lobby. Online-mode servers
        # retain Mojang's normal authentication and do not pass through it.
        needs_auth = next_state in (2, 3) and not inst.online_mode and getattr(inst.cfg, "auth_enabled", True)
        if needs_auth and not safe_player_name(player):
            # Fail closed: a login the proxy cannot read must never reach the server unchecked.
            return {"action": "close"}

        if needs_auth:
            auth_ip = peer_ip
            from .auth_sessions import account_ip, set_pending, touch, valid

            if not valid(player, auth_ip, protocol, edition):
                if not lobby or not lobby.running:
                    return self._offline_response(next_state, "auth", inst, hostname)
                refused = self.lobby_gate.admit(player, auth_ip, account_ip(player))
                if refused:
                    return self._offline_response(next_state, "auth", inst, hostname, message=refused)
                set_pending(player, auth_ip)
                target = lobby
                auth_name = player
            else:
                touch(player, auth_ip)
                auth_name = player

        if player and self._not_whitelisted(inst, player):
            inst.record_attempt(player, None, "proxy")
        if player and target is inst:
            inst.bind_voice(player, player_uuid if next_state in (2, 3) else None)

        if target is inst and inst.state != "online":
            if next_state in (2, 3) and inst.request_start():
                mode = "waking"
            elif inst.state in ("starting", "installing"):
                mode = "starting"
            else:
                mode = "offline"
            return self._offline_response(next_state, mode, inst, hostname)

        is_lobby = target is lobby
        target_port = target.port if is_lobby else target.cfg.internal_port
        track = not is_lobby
        send_proxy_v2 = bool(track and getattr(inst.cfg, "proxy_protocol", False))
        sniff_active = bool(auth_name and auth_ip)

        self._conns[conn_id] = _ConnState(
            conn_id=conn_id,
            inst=inst,
            player=player if next_state in (2, 3) else None,
            track=track,
            auth_name=auth_name,
            auth_ip=auth_ip,
            protocol=protocol,
            edition=edition,
            hostname=hostname,
            next_state=next_state,
            is_lobby=is_lobby,
        )
        return {
            "action": "forward",
            "target_port": int(target_port),
            "send_proxy_v2": send_proxy_v2,
            "track_sid": inst.cfg.id if track else None,
            "player": player if next_state in (2, 3) else None,
            "timeout_secs": None if track else self.lobby_gate.LOGIN_TIMEOUT,
            "sniff_seconds": CAPTURE_SECONDS if sniff_active else 0,
            "sniff_max_bytes": _SNIFF_MAX_BYTES if sniff_active else 0,
        }

    def handle_mc_upstream_failed(self, conn_id: int) -> dict[str, Any]:
        state = self._conns.pop(conn_id, None)
        if state is None:
            return {"action": "close"}
        if state.is_lobby and state.auth_name and state.auth_ip:
            self.lobby_gate.release(state.auth_name, state.auth_ip)
        return self._offline_response(state.next_state, "starting", state.inst, state.hostname)

    def handle_conn_opened(self, conn_id: int) -> None:
        state = self._conns.get(conn_id)
        if state is None or state.opened:
            return
        state.opened = True
        self.active += 1
        if state.track:
            sid = state.inst.cfg.id
            self.per_server[sid] = self.per_server.get(sid, 0) + 1
            if state.player:
                joined = getattr(state.inst, "proxy_player_joined", None)
                if joined:
                    joined(state.player)
        if state.auth_name and state.auth_ip:
            from .auth_sessions import record_fingerprint
            from .fingerprint import PacketSniffer

            def capture(snapshot: dict) -> None:
                if (
                    record_fingerprint(
                        state.auth_name,
                        state.auth_ip,
                        state.protocol,
                        snapshot,
                        bind=state.track,
                        edition=state.edition,
                    )
                    and state.track
                ):
                    log.info("Auth %s: client fingerprint changed – disconnecting", state.auth_name)
                    self.disconnect(conn_id)

            sniffer = PacketSniffer(state.protocol, on_capture=capture)
            state.sniffer = sniffer
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()

                def _freeze() -> None:
                    sniffer.freeze()
                    self.stop_sniff(conn_id)

                state.capture_timer = loop.call_later(sniffer.capture_seconds + 0.5, _freeze)

    def handle_sniff_chunk(self, conn_id: int, data: bytes) -> None:
        state = self._conns.get(conn_id)
        if state is not None and state.sniffer is not None and data:
            state.sniffer.feed(data)

    def handle_conn_closed(self, conn_id: int) -> None:
        state = self._conns.pop(conn_id, None)
        if state is None:
            return
        self._finalize_conn(state)

    def _finalize_conn(self, state: _ConnState) -> None:
        if state.capture_timer is not None:
            state.capture_timer.cancel()
            state.capture_timer = None
        if state.opened:
            self.active = max(0, self.active - 1)
            if state.track:
                sid = state.inst.cfg.id
                rem = max(0, self.per_server.get(sid, 1) - 1)
                if rem:
                    self.per_server[sid] = rem
                else:
                    self.per_server.pop(sid, None)
                if state.player:
                    left = getattr(state.inst, "proxy_player_left", None)
                    if left:
                        left(state.player)
        if state.auth_name and state.auth_ip:
            if state.sniffer is not None:
                state.sniffer.flush()
            if not state.track:
                from .auth_sessions import valid
                self.lobby_gate.release(
                    state.auth_name,
                    state.auth_ip,
                    failed=not valid(state.auth_name, state.auth_ip),
                )
            lobby_obj = getattr(self.m, "auth_lobby", None)
            if lobby_obj:
                lobby_obj.touch(state.auth_name, state.auth_ip)

    def _cleanup_all_conns(self) -> None:
        conns = list(self._conns.values())
        self._conns.clear()
        for state in conns:
            self._finalize_conn(state)
        self.active = 0
        self.per_server.clear()
        self._pings.clear()

    def handle_http_request(self, method: str, path: str) -> dict[str, Any]:
        """Validate hosted resource-pack requests (/rp/<server>/<token>/<pack>.zip)."""
        from . import resourcepacks as rpacks

        status = "404 Not Found"
        headers = [["Content-Type", "text/plain; charset=utf-8"]]
        body = b"MineNode resource pack host\n"
        m = _RP_PATH_RE.match(path)
        if method not in ("GET", "HEAD"):
            status = "405 Method Not Allowed"
            body = b"Method not allowed\n"
        elif m and (inst := self.m.instances.get(m.group(1))):
            try:
                pack, p = rpacks.get(inst, m.group(3))
                if hmac.compare_digest(pack["token"], m.group(2)) and p.exists():
                    size = p.stat().st_size
                    return {
                        "action": "file",
                        "status": "200 OK",
                        "headers": [
                            ["Content-Type", "application/zip"],
                            ["Content-Length", str(size)],
                            ["Cache-Control", "public, max-age=86400"],
                            ["ETag", f'"{pack["sha1"]}"'],
                        ],
                        "path": str(p),
                        "size": size,
                    }
            except FileNotFoundError:
                pass
        headers.append(["Content-Length", str(len(body))])
        return {
            "action": "body",
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
        }

    @staticmethod
    def _not_whitelisted(inst: "ServerInstance", player: str) -> bool:
        """Record join attempts of players missing from the whitelist (when it is enabled)."""
        from . import players as pl
        if inst.props().get("white-list", "false") != "true":
            return False
        return not pl.contains(pl.read_list(inst.dir / "whitelist.json"), player)

    def render(self, template: str, inst: "ServerInstance | None", hostname: str) -> str:
        if inst is None:
            values = {"name": hostname or "?", "motd": "", "address": hostname, "mc_version": "", "join_hint": ""}
        else:
            values = {
                "name": inst.cfg.name,
                "motd": inst.props().get("motd", inst.cfg.name),
                "address": inst.address(),
                "mc_version": inst.cfg.mc_version,
                "join_hint": (getattr(self.m.settings, "join_hint", None) or " §8(dołącz, aby uruchomić)")
                if inst.cfg.start_on_join and inst.cfg.installed else "",
            }
        out = template
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        return out.replace("\\n", "\n")

    def _offline_response(
        self,
        next_state: int,
        mode: str,
        inst: "ServerInstance | None",
        hostname: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        s = self.m.settings
        if next_state == 1:
            if mode == "unknown":
                motd, version = f"§cNieznany serwer\n§7{hostname or '?'}", "§c✕"
            elif mode in ("starting", "waking"):
                motd, version = s.motd_starting, s.version_starting
            else:
                motd, version = s.motd_offline, s.version_offline
            status: dict[str, Any] = {
                "version": {"name": self.render(version, inst, hostname), "protocol": -1},
                "players": {"max": 0, "online": 0},
                "description": {"text": self.render(motd, inst, hostname)},
            }
            if inst:
                icon = inst.dir / "server-icon.png"
                if icon.exists() and icon.stat().st_size < 64 * 1024:
                    status["favicon"] = "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode()
            return {"action": "status", "json": json.dumps(status, ensure_ascii=False)}

        if message:
            template = message
        elif mode == "waking":
            template = s.kick_waking
        elif mode == "starting":
            template = s.kick_starting
        elif mode == "auth":
            template = "Ten serwer wymaga logowania. Połącz się ponownie, aby się zarejestrować lub zalogować."
        else:
            template = s.kick_offline if inst else "§cNieznany serwer: {name}"
        msg = {"text": plain_text(self.render(template, inst, hostname))}
        return {"action": "disconnect", "json": json.dumps(msg, ensure_ascii=False)}
