"""Simple Voice Chat control plane (datagram forwarding runs in minenode-proxy).

Every SVC packet a client sends starts with a magic byte (0xFF) followed by the player's UUID in
plain text. Python synchronizes the active ``server_id -> voice_port`` and ``player_uuid ->
server_id`` routing tables over IPC so the Rust proxy forwards UDP datagrams on the hot path
without involving Python.
"""
from __future__ import annotations

import contextlib
import logging
import uuid as uuidlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .properties import read_properties, write_properties

if TYPE_CHECKING:
    from .manager import Manager, ServerInstance

log = logging.getLogger("minenode.voice")

SVC_PROJECT_ID = "9eGKb6K1"  # Simple Voice Chat on Modrinth


def config_path(inst: "ServerInstance") -> Path:
    base = "plugins" if inst.cfg.type in ("paper", "purpur") else "config"
    return inst.dir / base / "voicechat" / "voicechat-server.properties"


def is_installed(inst: "ServerInstance") -> bool:
    from .content import content_dir, kind_for, load_meta
    if not kind_for(inst.cfg.type):
        return False
    meta = load_meta(inst)["files"]
    if any(v.get("project_id") == SVC_PROJECT_ID for v in meta.values()):
        return True
    d = content_dir(inst)
    return d.exists() and any(p.name.lower().startswith("voicechat") and p.suffix == ".jar" for p in d.iterdir())


def public_host(inst: "ServerInstance") -> str | None:
    s = inst.m.settings
    if inst.cfg.aliases:
        return inst.cfg.aliases[0]
    domain = inst.main_domain() if hasattr(inst, "main_domain") else (s.base_domain or None)
    if domain:
        return f"{inst.cfg.subdomain}.{domain}"
    return s.public_address or None


def write_config(inst: "ServerInstance", public_port: int) -> None:
    """Point SVC at its internal port and tell clients to use the shared public port."""
    if not inst.cfg.voice_enabled or not inst.cfg.voice_port:
        return
    path = config_path(inst)
    path.parent.mkdir(parents=True, exist_ok=True)
    props = read_properties(path)
    host = public_host(inst)
    props.update({
        "port": str(inst.cfg.voice_port),
        "bind_address": "127.0.0.1",
        "voice_host": f"{host}:{public_port}" if host else "",
    })
    write_properties(path, props)


class VoiceProxy:
    def __init__(self, manager: "Manager", host: str, port: int):
        self.m = manager
        self.host, self.port = host, port
        self.listening = False
        self.error: str | None = None
        self.players: dict[str, str] = {}  # 32-char lowercase hex UUID -> server id
        self.sessions_count = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.dropped = 0
        self.per_server: dict[str, int] = {}
        self._last_servers: dict[str, int] | None = None

    def bind(self, player_uuid: str | None, sid: str) -> None:
        if not player_uuid:
            return
        with contextlib.suppress(ValueError):
            hex_uid = uuidlib.UUID(player_uuid).hex
            self.players[hex_uid] = sid
            proxy = getattr(self.m, "proxy", None)
            if proxy is not None:
                proxy.send_command({"t": "voice_bind", "uuid": hex_uid, "sid": sid})

    def sync(self, *, force: bool = False) -> None:
        """Push active voice-enabled server ports and known player bindings to the Rust proxy."""
        servers: dict[str, int] = {
            sid: int(inst.cfg.voice_port)
            for sid, inst in self.m.instances.items()
            if inst.cfg.voice_enabled and inst.running and inst.cfg.voice_port
        }
        if not force and self._last_servers == servers:
            return
        proxy = getattr(self.m, "proxy", None)
        if proxy is not None and proxy.send_command({
            "t": "voice_sync",
            "servers": servers,
            "players": dict(self.players),
        }):
            self._last_servers = servers

    def apply_ready(self, msg: dict[str, Any]) -> None:
        self.listening = bool(msg.get("voice_listening", False))
        self.error = msg.get("voice_error")
        if self.listening:
            log.info("Voice chat proxy listening on udp %s:%s", self.host, msg.get("voice_port", self.port))
        elif self.error:
            log.error("Voice proxy could not bind udp %s:%s – %s", self.host, self.port, self.error)

    def apply_telemetry(self, msg: dict[str, Any]) -> None:
        if "voice_listening" in msg:
            self.listening = bool(msg["voice_listening"])
        self.error = msg.get("voice_error")
        self.sessions_count = int(msg.get("voice_sessions", self.sessions_count))
        self.bytes_up = int(msg.get("voice_bytes_up", self.bytes_up))
        self.bytes_down = int(msg.get("voice_bytes_down", self.bytes_down))
        self.dropped = int(msg.get("voice_dropped", self.dropped))
        per = msg.get("voice_per_server")
        if isinstance(per, dict):
            self.per_server = {str(k): int(v) for k, v in per.items()}

    def stats(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "listening": self.listening,
            "error": self.error,
            "sessions": self.sessions_count,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "per_server": dict(self.per_server),
        }

    async def start(self) -> None:
        self.sync(force=True)

    async def stop(self) -> None:
        self.listening = False
        self.sessions_count = 0
        self.per_server.clear()
        self._last_servers = None
