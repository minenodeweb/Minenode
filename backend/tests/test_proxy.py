import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest

from app import auth_sessions
from app.models import PanelSettings
from app.proxy import MinecraftProxy, plain_text
from app.voice import VoiceProxy


def test_kick_messages_lose_formatting_codes():
    assert plain_text("§csurvival jest wyłączony.") == "survival jest wyłączony."
    assert plain_text("§x§1§2§3§4§5§6Hi\n§7Dołącz ponownie") == "Hi\nDołącz ponownie"
    assert plain_text("zwykły tekst") == "zwykły tekst"


def _make_inst(tmp_path, *, sid="a1b2c3d4e5", state="online", online_mode=True, proxy_protocol=False, voice_enabled=False, voice_port=24500):
    (tmp_path / "server.properties").write_text("motd=Hello\n", encoding="utf-8")
    joined = []
    left = []
    voice_bound = []
    started = []
    attempts = []

    inst = SimpleNamespace(
        dir=tmp_path,
        cfg=SimpleNamespace(
            id=sid,
            name="Survival",
            mc_version="1.21.4",
            internal_port=25600,
            start_on_join=True,
            installed=True,
            auth_enabled=True,
            proxy_protocol=proxy_protocol,
            voice_enabled=voice_enabled,
            voice_port=voice_port,
        ),
        state=state,
        running=(state in ("starting", "online", "stopping")),
        online_mode=online_mode,
        props=lambda: {"motd": "Hello"},
        address=lambda: "survival.mc.example.com",
        request_start=lambda: started.append(1) or True,
        record_attempt=lambda name, uid, src: attempts.append((name, src)),
        bind_voice=lambda name, uid: voice_bound.append((name, uid)),
        proxy_player_joined=lambda name: joined.append(name),
        proxy_player_left=lambda name: left.append(name),
        _joined=joined,
        _left=left,
        _voice_bound=voice_bound,
        _started=started,
        _attempts=attempts,
    )
    return inst


def _make_manager(insts=None, settings=None, auth_lobby=None):
    instances = {i.cfg.id: i for i in (insts or [])}
    m = SimpleNamespace(
        instances=instances,
        settings=settings or PanelSettings(base_domain="mc.example.com"),
        auth_lobby=auth_lobby,
        resolve_host=lambda h: instances.get("a1b2c3d4e5") if h == "survival.mc.example.com" else None,
        voice=None,
        proxy=None,
    )
    m.voice = VoiceProxy(m, "127.0.0.1", 24454)
    m.proxy = MinecraftProxy(m, "127.0.0.1", 25565, "127.0.0.1", 24454)
    return m


def test_handshake_forward_and_connection_lifecycle(tmp_path):
    inst = _make_inst(tmp_path, proxy_protocol=True)
    m = _make_manager([inst])

    decision = m.proxy.handle_mc_handshake({
        "conn_id": 7,
        "peer_ip": "203.0.113.5",
        "protocol": 769,
        "hostname": "Survival.MC.Example.COM.\x00FML",
        "next_state": 2,
        "player": "Steve",
        "player_uuid": "069a79f444e94726a5befca90e38aaf5",
        "edition": "java",
    })
    assert decision == {
        "action": "forward",
        "target_port": 25600,
        "send_proxy_v2": True,
        "track_sid": "a1b2c3d4e5",
        "player": "Steve",
        "timeout_secs": None,
        "sniff_seconds": 0,
        "sniff_max_bytes": 0,
    }
    assert inst._voice_bound == [("Steve", "069a79f444e94726a5befca90e38aaf5")]

    m.proxy.handle_conn_opened(7)
    assert m.proxy.active == 1
    assert m.proxy.per_server == {"a1b2c3d4e5": 1}
    assert inst._joined == ["Steve"]

    m.proxy.handle_conn_closed(7)
    assert m.proxy.active == 0
    assert m.proxy.per_server == {}
    assert inst._left == ["Steve"]


def test_handshake_offline_waking_and_unknown_responses(tmp_path):
    inst = _make_inst(tmp_path, state="offline")
    m = _make_manager([inst])

    # Unknown host -> status & plain-text disconnect
    unk_status = m.proxy.handle_mc_handshake({
        "conn_id": 1, "peer_ip": "1.2.3.4", "protocol": 769,
        "hostname": "nope.example.com", "next_state": 1,
    })
    assert unk_status["action"] == "status"
    assert "Nieznany serwer" in json.loads(unk_status["json"])["description"]["text"]

    unk_kick = m.proxy.handle_mc_handshake({
        "conn_id": 2, "peer_ip": "1.2.3.4", "protocol": 769,
        "hostname": "nope.example.com", "next_state": 2, "player": "Steve",
    })
    assert unk_kick["action"] == "disconnect"
    assert "§" not in json.loads(unk_kick["json"])["text"]

    # Stopped server with start_on_join triggers waking on login
    wake = m.proxy.handle_mc_handshake({
        "conn_id": 3, "peer_ip": "1.2.3.4", "protocol": 769,
        "hostname": "survival.mc.example.com", "next_state": 2, "player": "Steve",
    })
    assert wake["action"] == "disconnect"
    assert inst._started == [1]
    assert "uruchamia" in json.loads(wake["json"])["text"]


def test_upstream_failed_releases_lobby_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_sessions, "SESSION_FILE", tmp_path / "auth" / "sessions.db")
    monkeypatch.setattr(auth_sessions, "PENDING_FILE", tmp_path / "auth" / "pending.db")
    monkeypatch.setattr(auth_sessions, "FINGERPRINT_FILE", tmp_path / "auth" / "fp.json")

    inst = _make_inst(tmp_path, state="online", online_mode=False)
    lobby = SimpleNamespace(running=True, port=25999, touch=lambda *a: None)
    m = _make_manager([inst], auth_lobby=lobby)

    dec = m.proxy.handle_mc_handshake({
        "conn_id": 42, "peer_ip": "203.0.113.9", "protocol": 769,
        "hostname": "survival.mc.example.com", "next_state": 2, "player": "Steve",
    })
    assert dec["action"] == "forward" and dec["target_port"] == 25999
    assert "steve" in m.proxy.lobby_gate.owners

    fallback = m.proxy.handle_mc_upstream_failed(42)
    assert fallback["action"] == "disconnect"
    assert "steve" not in m.proxy.lobby_gate.owners


def test_http_request_serves_resource_pack_file_and_rejects_bad_tokens(tmp_path):
    inst = _make_inst(tmp_path)
    rp_dir = tmp_path / ".minenode" / "resourcepacks"
    rp_dir.mkdir(parents=True)
    pack_id = "0123456789ab"
    token = "secret_token_123"
    zip_path = rp_dir / f"{pack_id}.zip"
    zip_path.write_bytes(b"PK\x03\x04fakezip")
    (rp_dir / "index.json").write_text(json.dumps([{
        "id": pack_id,
        "token": token,
        "sha1": "deadbeef" * 5,
    }]), encoding="utf-8")

    m = _make_manager([inst])

    ok = m.proxy.handle_http_request("GET", f"/rp/a1b2c3d4e5/{token}/{pack_id}.zip?v=1")
    assert ok["action"] == "file"
    assert ok["status"] == "200 OK"
    assert ok["path"] == str(zip_path)
    assert ok["size"] == len(b"PK\x03\x04fakezip")

    bad_tok = m.proxy.handle_http_request("GET", f"/rp/a1b2c3d4e5/wrongtoken_1/{pack_id}.zip")
    assert bad_tok["action"] == "body" and bad_tok["status"] == "404 Not Found"

    bad_method = m.proxy.handle_http_request("POST", f"/rp/a1b2c3d4e5/{token}/{pack_id}.zip")
    assert bad_method["action"] == "body" and bad_method["status"] == "405 Method Not Allowed"


def test_voice_proxy_bind_and_sync_commands(tmp_path):
    inst = _make_inst(tmp_path, state="online", voice_enabled=True, voice_port=24501)
    m = _make_manager([inst])
    sent = []
    m.proxy.send_command = lambda msg: sent.append(msg) or True

    m.voice.bind("069a79f4-44e9-4726-a5be-fca90e38aaf5", "a1b2c3d4e5")
    m.voice.sync()
    m.voice.sync()  # unchanged -> deduplicated

    assert sent == [
        {"t": "voice_bind", "uuid": "069a79f444e94726a5befca90e38aaf5", "sid": "a1b2c3d4e5"},
        {"t": "voice_sync", "servers": {"a1b2c3d4e5": 24501}, "players": {"069a79f444e94726a5befca90e38aaf5": "a1b2c3d4e5"}},
    ]


def test_ipc_loopback_roundtrip_auth_and_fingerprint_disconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_sessions, "SESSION_FILE", tmp_path / "auth" / "sessions.db")
    monkeypatch.setattr(auth_sessions, "PENDING_FILE", tmp_path / "auth" / "pending.db")
    monkeypatch.setattr(auth_sessions, "FINGERPRINT_FILE", tmp_path / "auth" / "fingerprints.json")

    # Prepare an active session bound to a different fingerprint hash so sniffer flush triggers disconnect.
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    future = int(time.time()) + 1800
    auth_sessions.SESSION_FILE.write_text(
        f"Steve\tsalt\thash\t203.0.113.5\t{future}\told_hash_123\n", encoding="utf-8"
    )
    auth_sessions.FINGERPRINT_FILE.write_text(json.dumps({
        "steve": {"hash": "old_hash_123", "protocol": 769, "edition": "java", "ip": "203.0.113.5"}
    }), encoding="utf-8")

    inst = _make_inst(tmp_path, state="online", online_mode=False, voice_enabled=True, voice_port=24502)
    m = _make_manager([inst])

    async def scenario():
        await m.proxy._start_ipc()
        try:
            scheme, addr = m.proxy._ipc_addr.split(":", 1)
            if scheme == "unix":
                bad_r, bad_w = await asyncio.open_unix_connection(addr)
            else:
                host, port = addr.rsplit(":", 1)
                bad_r, bad_w = await asyncio.open_connection(host, int(port))
            bad_w.write(b'{"t":"hello","version":1,"token":"wrong"}\n')
            await bad_w.drain()
            assert await bad_r.readline() == b""
            bad_w.close()

            if scheme == "unix":
                r, w = await asyncio.open_unix_connection(addr)
            else:
                host, port = addr.rsplit(":", 1)
                r, w = await asyncio.open_connection(host, int(port))

            async def send(obj):
                w.write((json.dumps(obj) + "\n").encode("utf-8"))
                await w.drain()

            async def recv():
                line = await asyncio.wait_for(r.readline(), 3)
                return json.loads(line.decode("utf-8"))

            await send({"t": "hello", "version": 1, "token": m.proxy._ipc_token})
            await send({
                "t": "ready", "tcp_listening": True, "tcp_error": None, "tcp_port": 25565,
                "voice_listening": True, "voice_error": None, "voice_port": 24454,
            })
            sync_cmd = await recv()
            assert sync_cmd["t"] == "voice_sync" and sync_cmd["servers"] == {"a1b2c3d4e5": 24502}
            assert m.proxy.listening is True and m.voice.listening is True

            await send({
                "t": "mc_handshake", "id": 10, "conn_id": 99,
                "peer_ip": "203.0.113.5", "peer_port": 50000,
                "local_ip": "127.0.0.1", "local_port": 25565,
                "tcp_loopback": False, "edition": "java",
                "protocol": 769, "hostname": "survival.mc.example.com",
                "next_state": 2, "player": "Steve", "player_uuid": None,
            })
            resp = await recv()
            assert resp["t"] == "resp" and resp["id"] == 10
            assert resp["action"] == "forward" and resp["sniff_seconds"] > 0

            await send({"t": "conn_opened", "conn_id": 99})
            await asyncio.sleep(0.05)

            # Craft a valid Plugin Message (minecraft:brand = "fabric") packet frame
            def varint(n):
                out = bytearray()
                while True:
                    b = n & 0x7F
                    n >>= 7
                    if n:
                        out.append(b | 0x80)
                    else:
                        out.append(b)
                        return bytes(out)

            def mc_str(s):
                d = s.encode("utf-8")
                return varint(len(d)) + d

            brand = mc_str("fabric")
            frame_body = varint(1) + mc_str("minecraft:brand") + varint(len(brand)) + brand
            pkt = varint(len(frame_body)) + frame_body

            await send({
                "t": "sniff_chunk",
                "conn_id": 99,
                "data_b64": base64.b64encode(pkt).decode("ascii"),
            })
            await asyncio.sleep(0.05)
            # Closing or freezing flushes the sniffer -> detects changed fingerprint -> issues disconnect!
            await send({"t": "conn_closed", "conn_id": 99})
            ctrl = await recv()
            assert ctrl == {"t": "conn_control", "conn_id": 99, "op": "disconnect"}

            await send({
                "t": "telemetry",
                "tcp_listening": True, "tcp_error": None,
                "active": 0, "total": 1, "bytes_up": 100, "bytes_down": 200, "packs_served": 1,
                "per_server": {}, "pings": {},
                "voice_listening": True, "voice_error": None,
                "voice_sessions": 2, "voice_bytes_up": 300, "voice_bytes_down": 400,
                "voice_dropped": 0, "voice_per_server": {"a1b2c3d4e5": 2},
            })
            await asyncio.sleep(0.05)
            assert m.proxy.total == 1 and m.proxy.http_served == 1
            assert m.voice.stats()["sessions"] == 2

            w.close()
        finally:
            await m.proxy._close_ipc()

    asyncio.run(scenario())


def test_ipc_start_on_join_runs_on_event_loop_thread(tmp_path):
    spawned = []
    inst = _make_inst(tmp_path, state="offline", online_mode=True)

    async def fake_start():
        spawned.append("started")

    def request_start():
        # Must execute on the running asyncio event loop thread (raises RuntimeError otherwise).
        asyncio.get_running_loop()
        asyncio.create_task(fake_start())
        return True

    inst.request_start = request_start
    m = _make_manager([inst])

    async def scenario():
        await m.proxy._start_ipc()
        try:
            scheme, addr = m.proxy._ipc_addr.split(":", 1)
            if scheme == "unix":
                r, w = await asyncio.open_unix_connection(addr)
            else:
                host, port = addr.rsplit(":", 1)
                r, w = await asyncio.open_connection(host, int(port))

            w.write((json.dumps({"t": "hello", "version": 1, "token": m.proxy._ipc_token}) + "\n").encode("utf-8"))
            w.write((json.dumps({
                "t": "mc_handshake", "id": 1, "conn_id": 1,
                "peer_ip": "203.0.113.5", "peer_port": 50000,
                "local_ip": "127.0.0.1", "local_port": 25565,
                "tcp_loopback": False, "edition": "java",
                "protocol": 769, "hostname": "survival.mc.example.com",
                "next_state": 2, "player": "Steve", "player_uuid": None,
            }) + "\n").encode("utf-8"))
            await w.drain()

            resp = json.loads((await asyncio.wait_for(r.readline(), 3)).decode("utf-8"))
            assert resp["t"] == "resp" and resp["id"] == 1
            assert resp["action"] == "disconnect"
            assert "uruchamia" in json.loads(resp["json"])["text"]
            await asyncio.sleep(0)
            assert spawned == ["started"]
            w.close()
        finally:
            await m.proxy._close_ipc()

    asyncio.run(scenario())
