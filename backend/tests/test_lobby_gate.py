"""Login lobby gate: one owner per name, limited failed visits, fail-closed name parsing."""
import time
from types import SimpleNamespace

import pytest

from app import auth_sessions, proxy
from app.proxy import LobbyGate, MinecraftProxy, safe_player_name


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_sessions, "SESSION_FILE", tmp_path / "auth" / "sessions.db")
    monkeypatch.setattr(auth_sessions, "PENDING_FILE", tmp_path / "auth" / "pending.db")


def test_names_that_could_forge_auth_rows_are_refused():
    assert safe_player_name("Steve") and safe_player_name("._Bedrock_Gamer")
    for bad in (None, "", "a\nadmin", "a\tb", "x\r", "a" * 17, "nul\x00"):
        assert not safe_player_name(bad)
    with pytest.raises(ValueError):
        auth_sessions.set_pending("a\nadmin", "203.0.113.9")


def test_second_address_cannot_take_over_a_name_that_is_logging_in():
    gate = LobbyGate()
    assert gate.admit("Steve", "203.0.113.7") is None          # the real Steve is in the lobby
    assert "Ktoś inny" in gate.admit("steve", "198.51.100.66")  # attacker would overwrite pending.db
    assert gate.admit("Steve", "::ffff:203.0.113.7") is None    # Steve reconnecting is fine
    gate.release("Steve", "203.0.113.7")
    gate.release("Steve", "203.0.113.7")
    assert gate.admit("steve", "198.51.100.66") is None         # free again once Steve has left


def test_failed_visits_are_limited_per_address():
    gate = LobbyGate()
    for _ in range(gate.IP_FAILS):
        assert gate.admit("Alex", "198.51.100.66") is None
        gate.release("Alex", "198.51.100.66", failed=True)
    assert "Zbyt wiele" in gate.admit("Notch", "198.51.100.66")
    assert gate.admit("Alex", "203.0.113.1") is None  # other players are not affected


def test_name_limit_never_locks_out_the_accounts_own_address():
    gate = LobbyGate()
    for n in range(gate.NAME_FAILS):
        ip = f"198.51.100.{n}"
        assert gate.admit("Admin", ip) is None
        gate.release("Admin", ip, failed=True)
    assert "Zbyt wiele" in gate.admit("Admin", "198.51.100.200")
    assert gate.admit("Admin", "203.0.113.7", account_ip="203.0.113.7") is None


def test_old_failures_expire(monkeypatch):
    gate = LobbyGate()
    now = time.time()
    for _ in range(gate.IP_FAILS):
        gate.release("Alex", "198.51.100.66", failed=True)
    monkeypatch.setattr(proxy.time, "time", lambda: now + gate.WINDOW + 1)
    assert gate.admit("Alex", "198.51.100.66") is None


def test_backend_listens_on_loopback_only(tmp_path, monkeypatch):
    from app import manager
    from app.properties import read_properties
    (tmp_path / "server.properties").write_text("server-ip=0.0.0.0\nserver-port=25565\n")
    inst = SimpleNamespace(dir=tmp_path, cfg=SimpleNamespace(internal_port=25601, name="x", voice_enabled=False))
    manager.ServerInstance._enforce_properties(inst)
    props = read_properties(tmp_path / "server.properties")
    assert props["server-ip"] == "127.0.0.1" and props["server-port"] == "25601"


# ---------------------------------------------------------------- routing (fail closed)

def test_login_with_an_unreadable_name_is_refused_not_passed_through():
    inst = SimpleNamespace(online_mode=False, cfg=SimpleNamespace(auth_enabled=True, id="s1"),
                           state="online", request_start=lambda: False)
    lobby = SimpleNamespace(running=True, port=25999)
    m = SimpleNamespace(resolve_host=lambda h: inst, auth_lobby=lobby, settings=SimpleNamespace())
    p = MinecraftProxy(m, "127.0.0.1", 0)

    for bad_name in (None, "", "a" * 40, "evil\nadmin"):
        dec = p.handle_mc_handshake({
            "conn_id": 1, "peer_ip": "198.51.100.66", "protocol": 767,
            "hostname": "survival.example.com", "next_state": 2,
            "player": bad_name, "edition": "java",
        })
        assert dec == {"action": "close"}


# ---------------------------------------------------------------- who is online

def _instance(tmp_path, monkeypatch):
    from app import manager
    from app.models import ServerConfig
    from app.store import JsonStore
    monkeypatch.setattr(manager, "SERVERS_DIR", tmp_path)
    monkeypatch.setattr(manager, "JsonStore", lambda path, default: JsonStore(tmp_path / "players.json", default))
    cfg = ServerConfig(id="s1", name="S", type="purpur", mc_version="26.3", subdomain="s", internal_port=25600)
    inst = manager.ServerInstance(SimpleNamespace(voice=None), cfg)
    inst.broadcast = lambda msg: None
    return inst


def test_new_system_chat_join_and_leave_lines_are_recognised(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    inst._parse_line("[17:00:49 INFO]: System chat: zlotyzabek joined the game")
    assert list(inst.online) == ["zlotyzabek"]
    inst._parse_line("[17:10:02 INFO]: System chat: zlotyzabek left the game")
    assert inst.online == {}


def test_custom_join_messages_still_count_through_the_login_line(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    inst._parse_line("[12:00:00 INFO]: Steve[/203.0.113.7:50000] logged in with entity id 123 at ([world]0, 64, 0)")
    inst._parse_line("[12:00:00 INFO]: §6Witaj §eSteve§6 na serwerze!")  # EssentialsX custom message
    assert list(inst.online) == ["Steve"]
    inst._parse_line("[12:30:00 INFO]: Steve lost connection: Disconnected")
    assert inst.online == {}


def test_player_chat_is_collected_and_system_lines_are_ignored(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    inst._parse_line("[12:00:00 INFO]: <§6Steve§r> Cześć §awszystkim§r!")
    inst._parse_line("[12:00:01 INFO]: [Not Secure] <Alex> hello")
    inst._parse_line("[12:00:02] [Async Chat Thread - #0/INFO]: [Not Secure] <.Bedrock Kid> hi there")
    inst._parse_line("[12:00:03 INFO]: Steve joined the game")
    inst._parse_line("[12:00:04 INFO]: <Steve>")  # empty message
    assert [m["name"] for m in inst.chat] == ["Steve", "Alex", ".Bedrock Kid"]
    assert inst.chat[0]["text"] == "Cześć wszystkim!"
    assert inst.chat[2]["text"] == "hi there"
    assert [m["seq"] for m in inst.chat] == [1, 2, 3]
    assert isinstance(inst.chat[0]["t"], float)


def test_chat_buffer_keeps_the_latest_fifty_messages(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    for i in range(60):
        inst._parse_line(f"[12:00:00 INFO]: <Steve> message {i}")
    assert len(inst.chat) == 50
    assert inst.chat[0]["text"] == "message 10"
    assert inst.chat[-1]["text"] == "message 59"
    assert [m["seq"] for m in inst.chat] == list(range(11, 61))


def test_proxy_connections_define_who_is_online(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    inst.proxy_player_joined(".BedrockKid")          # Geyser name: never matches the log patterns
    inst._parse_line("[12:00:01 INFO]: .BedrockKid joined the game")
    assert list(inst.online) == [".BedrockKid"]
    inst.proxy_player_joined(".bedrockkid")          # quick reconnect before the old socket closed
    inst.proxy_player_left(".BedrockKid")
    assert list(inst.online) == [".BedrockKid"]
    inst.proxy_player_left(".bedrockkid")
    assert inst.online == {}


def test_a_whitelist_kick_is_not_remembered_as_a_player(tmp_path, monkeypatch):
    inst = _instance(tmp_path, monkeypatch)
    inst.proxy_player_joined("Stranger")
    inst.proxy_player_left("Stranger")  # rejected within a second
    assert "stranger" not in inst.players_store.data.get("known", {})


def test_geyser_connections_are_checked_as_bedrock_and_direct_ones_as_java(monkeypatch):
    from app.models import PanelSettings
    seen = []
    monkeypatch.setattr(auth_sessions, "valid", lambda name, ip, protocol=None, edition=None: seen.append((ip, edition)) or False)
    monkeypatch.setattr(auth_sessions, "set_pending", lambda *a: None)

    inst = SimpleNamespace(
        online_mode=False,
        cfg=SimpleNamespace(auth_enabled=True, id="s1", name="S", mc_version="1.21.4",
                            start_on_join=False, installed=True, proxy_protocol=False),
        state="online",
        props=lambda: {},
        address=lambda: "survival.example.com",
        bind_voice=lambda *a: None,
    )
    lobby = SimpleNamespace(running=True, port=25999)
    m = SimpleNamespace(resolve_host=lambda h: inst, auth_lobby=lobby, settings=PanelSettings())
    p = MinecraftProxy(m, "127.0.0.1", 0)

    p.handle_mc_handshake({
        "conn_id": 1, "peer_ip": "203.0.113.7", "protocol": 767,
        "hostname": "survival.example.com", "next_state": 2,
        "player": "Steve", "edition": "java",
    })
    p.handle_mc_handshake({
        "conn_id": 2, "peer_ip": "198.51.100.20", "protocol": 767,
        "hostname": "survival.example.com", "next_state": 2,
        "player": "Steve", "edition": "bedrock",
    })
    assert seen == [("203.0.113.7", "java"), ("198.51.100.20", "bedrock")]
