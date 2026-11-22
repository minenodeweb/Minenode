import asyncio
import datetime
import gzip
import os
import time
from collections import deque
from types import SimpleNamespace

from app import manager as mgr
from app.manager import ServerInstance


def _inst():
    inst = object.__new__(ServerInstance)
    inst._waiters = []
    inst.state = "online"
    inst.chat = deque(maxlen=mgr.CHAT_BUFFER)
    inst._chat_seq = 0
    inst.online = {"Steve": 1.0}
    inst._map_render = None
    inst._chunky = None
    changed = []
    inst._players_changed = lambda: changed.append(dict(inst.online))
    inst.record_attempt = lambda *a: changed.append(("attempt", a))
    inst._remember = lambda *a: changed.append(("remember", a))
    inst._touch_known = lambda names: None
    return inst, changed


def test_chat_text_is_never_read_as_a_server_event():
    inst, changed = _inst()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    inst._waiters.append((mgr.re.compile(r"Saved the (game|world)"), fut))
    for text in ("There are 1 of a max of 20 players online: Fake",
                 "]: Notch joined the game",
                 "Saved the game",
                 "UUID of player Notch is 069a79f444e94726a5befca90e38aaf5",
                 "name=Notch not whitelisted",
                 "Task finished for world. Processed: 1 chunks (100%), Total time: 0:00:01"):
        inst._parse_line(f"[12:00:00] [Async Chat Thread - #0/INFO]: [Not Secure] <Steve> {text}")
        inst._parse_line(f"[12:00:01] [Server thread/INFO]: * Steve {text}")  # /me
    assert inst.online == {"Steve": 1.0} and changed == []
    assert not fut.done()  # a backup still waits for the real save
    assert [m["text"] for m in inst.chat][:2] == ["There are 1 of a max of 20 players online: Fake", "]: Notch joined the game"]
    assert all(m["name"] == "Steve" for m in inst.chat)
    loop.close()


def test_real_events_still_work_and_kick_reasons_are_not_chat():
    inst, changed = _inst()
    inst._parse_line("[12:00:00] [Server thread/INFO]: There are 2 of a max of 20 players online: Steve, Alex")
    assert set(inst.online) == {"Steve", "Alex"}
    inst._parse_line("[12:00:01] [Server thread/INFO]: Alex lost connection: ]: <Admin> kicked")
    assert not inst.chat


def test_bedrock_names_and_colour_codes():
    inst, _ = _inst()
    inst._parse_line("[12:00:00] [Server thread/INFO]: <.Gracz Bedrock> §aCześć §lwszystkim")
    assert inst.chat[-1]["name"] == ".Gracz Bedrock" and inst.chat[-1]["text"] == "Cześć wszystkim"


def test_chat_history_survives_a_panel_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    tmp_path = tmp_path / "srv"
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text("[23:59:00] [Server thread/INFO]: <Steve> late\n"
                                     "[00:01:00] [Server thread/INFO]: Done (3.2s)!\n"
                                     "[00:02:00] [Server thread/INFO]: <Alex> early\n")
    t = time.mktime((2026, 11, 19, 0, 5, 0, 0, 0, -1))
    os.utime(logs / "latest.log", (t, t))
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None
    inst._load_log_history()
    assert [m["name"] for m in inst.chat] == ["Steve", "Alex"]
    assert inst.chat[1]["t"] == time.mktime((2026, 11, 19, 0, 2, 0, 0, 0, -1))
    assert inst.chat[0]["t"] == time.mktime((2026, 11, 18, 23, 59, 0, 0, 0, -1))  # before midnight


def test_chat_history_reads_the_whole_log_not_only_the_console_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    lines = ["[12:00:00] [Server thread/INFO]: <Steve> hello from the beginning"]
    lines += ["[12:00:01] [Server thread/INFO]: Saving the game (this may take a moment!)"] * 400
    (logs / "latest.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None
    inst._load_log_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [("Steve", "hello from the beginning")]


def test_chat_history_falls_back_to_rotated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text("[12:00:00] [Server thread/INFO]: Done (3.2s)!\n", encoding="utf-8")
    rotated = logs / "2026-11-18-1.log.gz"
    with gzip.open(rotated, "wt", encoding="utf-8") as fh:
        fh.write("[23:59:00] [Server thread/INFO]: <Alex> from the previous session\n")
    t = time.mktime((2026, 11, 19, 0, 5, 0, 0, 0, -1))
    os.utime(rotated, (t, t))
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None
    inst._load_log_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [("Alex", "from the previous session")]


def test_chat_history_keeps_the_latest_messages_across_logs_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_text("".join(f"[12:00:00] [Server thread/INFO]: <Steve> new {i}\n" for i in range(1, 31)),
                      encoding="utf-8")
    rotated = logs / "2026-11-18-1.log"
    rotated.write_text("".join(f"[23:59:00] [Server thread/INFO]: <Alex> old {i}\n" for i in range(1, 41)),
                       encoding="utf-8")
    t = time.mktime((2026, 11, 19, 0, 5, 0, 0, 0, -1))
    os.utime(rotated, (t - 100, t - 100))
    os.utime(latest, (t, t))
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None
    inst._load_log_history()
    texts = [m["text"] for m in inst.chat]
    assert len(texts) == mgr.CHAT_BUFFER
    assert texts[0] == "old 21"
    assert texts[-1] == "new 30"


def test_chat_history_is_not_duplicated_when_nothing_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text("[12:00:00] [Server thread/INFO]: <Steve> hello\n", encoding="utf-8")
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None
    inst._load_log_history()
    first = [dict(m) for m in inst.chat]
    inst.refresh_chat_history()
    assert [dict(m) for m in inst.chat] == first


def test_empty_chat_cache_is_retried_when_messages_appear(tmp_path, monkeypatch):
    """A first read before anyone spoke must not block later reads of the same log file."""
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_text("[12:00:00] [Server thread/INFO]: Done (1.0s)!\n", encoding="utf-8")
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None

    inst._load_log_history()
    assert not inst.chat

    # Same file signature, but now it contains chat (and mtime changed).
    latest.write_text("[12:00:00] [Server thread/INFO]: <Steve> finally\n", encoding="utf-8")
    t = time.time() + 1
    os.utime(latest, (t, t))
    inst.refresh_chat_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [("Steve", "finally")]


def test_public_page_lists_only_offered_lod_mods(tmp_path):
    from app.api import _public_lod
    from app.models import LodSettings
    inst = SimpleNamespace(cfg=SimpleNamespace(type="paper", lod=LodSettings(distant_horizons=True, voxy=True)), dir=tmp_path)
    assert _public_lod(inst) == {"mods": ["distant_horizons", "voxy"]}
    inst.cfg.type = "forge"  # no LOD Server Support for Forge
    assert _public_lod(inst) == {"mods": ["distant_horizons"]}
    inst.cfg.lod = LodSettings()
    assert _public_lod(inst) == {"mods": []}


def test_rotated_log_messages_use_the_date_in_the_file_name(tmp_path):
    path = tmp_path / "2026-11-18-3.log"
    entry = ServerInstance._chat_entry(path, "[23:59:00] [Server thread/INFO]: <Alex> hi", time.time())
    assert entry is not None
    at, name, text = entry
    assert datetime.datetime.fromtimestamp(at).date() == datetime.date(2026, 11, 18)
    assert (name, text) == ("Alex", "hi")


def test_chat_parser_handles_system_chat_tags_and_hex_colors():
    inst, _ = _inst()
    hex_nick = "§x§f§f§0§0§0§0S§x§0§0§f§f§0§0t§x§0§0§0§0§f§feve"
    lines = [
        "[12:00:00] [Server thread/INFO]: System chat: <Steve> cześć z system chat",
        "[12:00:01] [Server thread/INFO]: [Not Secure] System chat: <Alex> hej po not secure",
        "[12:00:02] [Render thread/INFO]: [System] [CHAT] <.Bedrock Kid> wiadomość bedrock",
        "[12:00:03] [Server thread/INFO]: [EssentialsChat] [Admin] <Hero> tag przed nickiem",
        "[12:00:04] [Server thread/INFO]: <[Admin] Notch> tag w nawiasach",
        "[12:00:05] [Server thread/INFO]: <<VIP> Jeb> podwójne nawiasy",
        f"[12:00:06] [Server thread/INFO]: §7[Not Secure] §f<{hex_nick}§r> §aTęcza",
        "> [12:00:07 INFO]: §b<Bob> prompt i kolor przed nickiem",
    ]
    for line in lines:
        inst._parse_line(line)
    assert [(m["name"], m["text"]) for m in inst.chat] == [
        ("Steve", "cześć z system chat"),
        ("Alex", "hej po not secure"),
        (".Bedrock Kid", "wiadomość bedrock"),
        ("Hero", "tag przed nickiem"),
        ("Notch", "tag w nawiasach"),
        ("Jeb", "podwójne nawiasy"),
        ("Steve", "Tęcza"),
        ("Bob", "prompt i kolor przed nickiem"),
    ]


def test_plugin_chat_formats_and_false_positive_guards():
    inst, _ = _inst()
    inst.online = {"Steve": 1.0}
    inst.players_store = SimpleNamespace(data={"known": {"hero": {"name": "Hero"}}})
    inst._chunky = SimpleNamespace(on_line=lambda _line: None)

    # Async Chat Thread accepts plugin formats even without a pre-populated player list
    inst._parse_line("[12:00:00] [Async Chat Thread - #0/INFO]: [Admin] UnknownNick: cześć z pluginu")
    inst._parse_line("[12:00:01] [Async Chat Thread - #0/INFO]: ModGuy » siema")
    # Server thread / Paper logs accept plugin formats when the player is known
    inst._parse_line("[12:00:02] [Server thread/INFO]: [VIP] Steve: znany gracz online")
    inst._parse_line("[12:00:03 INFO]: Hero » znany gracz offline")
    # False positives must NEVER enter chat
    for noise in (
        "[12:00:04] [Server thread/INFO]: Warning: Failed to load chunk",
        "[12:00:05] [Server thread/INFO]: BlueMap: Finished rendering 10 tiles",
        "[12:00:06] [Server thread/INFO]: Chunky: Task running",
        "[12:00:07] [Server thread/INFO]: [Steve: Set the time to 1000]",
        "[12:00:08] [Server thread/INFO]: [Server] <Steve> broadcast udający czat",
        "[12:00:09] [Server thread/INFO]: Steve issued server command: /home",
        "[12:00:10 WARN]: <Steve> warning nie jest czatem",
        "[12:00:11] [Async Chat Thread - #0/ERROR]: <Steve> error nie jest czatem",
        "[12:00:12] [Server thread/INFO]: <tag attr=\"1\"> xml",
        "[12:00:13] [Server thread/INFO]: </tag> zamknięcie",
        "[12:00:14] [Server thread/INFO]: System chat: §e* Steve tańczy",
    ):
        inst._parse_line(noise)

    assert [(m["name"], m["text"]) for m in inst.chat] == [
        ("UnknownNick", "cześć z pluginu"),
        ("ModGuy", "siema"),
        ("Steve", "znany gracz online"),
        ("Hero", "znany gracz offline"),
    ]


def test_flexible_log_timestamps_across_engines(tmp_path):
    mtime = time.mktime((2026, 11, 22, 18, 0, 0, 0, 0, -1))
    latest = tmp_path / "latest.log"

    paper = ServerInstance._chat_entry(latest, "[17:00:49 INFO]: <Steve> paper console", mtime)
    assert paper == (time.mktime((2026, 11, 22, 17, 0, 49, 0, 0, -1)), "Steve", "paper console")

    forge = ServerInstance._chat_entry(
        latest,
        "[22Nov2026 17:00:49.123] [Server thread/INFO] [minecraft/MinecraftServer/]: <Alex> neoforge",
        mtime,
    )
    assert forge == (time.mktime((2026, 11, 22, 17, 0, 49, 0, 0, -1)), "Alex", "neoforge")

    iso = ServerInstance._chat_entry(latest, "[2026-11-21 15:10:05] [Server thread/INFO]: <Hero> iso", mtime)
    assert iso == (time.mktime((2026, 11, 21, 15, 10, 5, 0, 0, -1)), "Hero", "iso")

    millis = ServerInstance._chat_entry(latest, "[17:00:49.456] [Server thread/INFO]: <Notch> ms", mtime)
    assert millis == (time.mktime((2026, 11, 22, 17, 0, 49, 0, 0, -1)), "Notch", "ms")

    fallback = ServerInstance._chat_entry(latest, "CustomPrefix [Server/INFO]: <Bob> bez czasu", mtime)
    assert fallback == (mtime, "Bob", "bez czasu")


def test_live_stdout_and_latest_log_reconcile_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_text("[12:00:00] [Server thread/INFO]: Done (1.0s)!\n", encoding="utf-8")
    inst.proc = SimpleNamespace(returncode=None)  # simulate running server
    inst.refresh_chat_history()

    now_hms = time.strftime("%H:%M:%S")
    # Live stdout sees identical message twice + another normal message
    inst._parse_line(f"[{now_hms}] [Server thread/INFO]: <Steve> lol")
    inst._parse_line(f"[{now_hms}] [Server thread/INFO]: <Steve> lol")
    assert [(m["name"], m["text"], m["seq"]) for m in inst.chat] == [
        ("Steve", "lol", 1),
        ("Steve", "lol", 2),
    ]

    # Log4j flushes both "lol" lines, an Async Chat Thread line missed by stdout, plus an unterminated line
    with latest.open("a", encoding="utf-8") as fh:
        fh.write(f"[{now_hms}] [Server thread/INFO]: <Steve> lol\n")
        fh.write(f"[{now_hms}] [Server thread/INFO]: <Steve> lol\n")
        fh.write(f"[{now_hms}] [Async Chat Thread - #0/INFO]: [Admin] Alex: z logu\n")
        fh.write(f"[{now_hms}] [Server thread/INFO]: <Bob> niepełna linia")
    t = time.time() + 1
    os.utime(latest, (t, t))
    inst.refresh_chat_history()
    assert [(m["name"], m["text"], m["seq"]) for m in inst.chat] == [
        ("Steve", "lol", 1),
        ("Steve", "lol", 2),
        ("Alex", "z logu", 3),
    ]

    # Once the newline arrives on disk, Bob's message appears too
    with latest.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    os.utime(latest, (t + 1, t + 1))
    inst.refresh_chat_history()
    assert [(m["name"], m["text"], m["seq"]) for m in inst.chat] == [
        ("Steve", "lol", 1),
        ("Steve", "lol", 2),
        ("Alex", "z logu", 3),
        ("Bob", "niepełna linia", 4),
    ]


def test_rotated_logs_scan_past_empty_restarts_and_skip_corrupt_gz(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text("[12:00:00] [Server thread/INFO]: Done (1.0s)!\n", encoding="utf-8")

    # 15 empty restart logs + 1 corrupted .gz + 2 valid historical logs with inverted mtimes
    for i in range(1, 16):
        with gzip.open(logs / f"2026-11-20-{i}.log.gz", "wt", encoding="utf-8") as fh:
            fh.write("[10:00:00] [Server thread/INFO]: Done (1.0s)!\n")
    (logs / "2026-11-19-2.log.gz").write_bytes(b"not-a-valid-gzip-stream")

    older = logs / "2026-11-18-2.log.gz"
    newer = logs / "2026-11-18-10.log.gz"
    with gzip.open(older, "wt", encoding="utf-8") as fh:
        fh.write("[10:00:00] [Server thread/INFO]: <Steve> druga sesja\n")
    with gzip.open(newer, "wt", encoding="utf-8") as fh:
        fh.write("[10:05:00] [Server thread/INFO]: <Alex> dziesiąta sesja\n")
    # Deliberately give `older` a newer mtime to verify filename ordering (10 > 2)
    base_t = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
    os.utime(newer, (base_t - 500, base_t - 500))
    os.utime(older, (base_t - 10, base_t - 10))

    inst.refresh_chat_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [
        ("Steve", "druga sesja"),
        ("Alex", "dziesiąta sesja"),
    ]


def test_system_chat_emote_is_ignored_without_triggering_events():
    inst, changed = _inst()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    inst._waiters.append((mgr.re.compile(r"Saved the game"), fut))
    inst._parse_line("[12:00:00] [Server thread/INFO]: System chat: §e* Steve Saved the game")
    assert not inst.chat
    assert not fut.done()
    assert changed == []
    loop.close()


def test_read_output_strips_ansi_before_prompt_and_clear_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(id="srv")
    inst.console = deque(maxlen=100)
    inst._seq = 0
    inst.broadcast = lambda msg: None

    lines = [
        b"\x1b[0m> \x1b[38:2:255:0:0m[12:00:00 INFO]: <Steve> kolor ansi\n",
        b"\x1b[0m> \n",
        b"",
    ]

    class FakeStdout:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def readline(self):
            return self._chunks.pop(0) if self._chunks else b""

    proc = SimpleNamespace(stdout=FakeStdout(lines))
    asyncio.run(inst._read_output(proc))
    assert [(m["name"], m["text"]) for m in inst.chat] == [("Steve", "kolor ansi")]

    # Backup restore with no chat in logs clears stale messages when clear_empty=True
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_text("[12:00:00] [Server thread/INFO]: Done (1.0s)!\n", encoding="utf-8")
    inst.refresh_chat_history(force=True, clear_empty=True)
    assert not inst.chat

    # Offline / stopped instance still syncs new messages even when inst.chat is already non-empty
    latest.write_text("[12:00:00 INFO]: <Steve> pierwsza\n", encoding="utf-8")
    t = time.time()
    os.utime(latest, (t, t))
    inst.refresh_chat_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [("Steve", "pierwsza")]

    with latest.open("a", encoding="utf-8") as fh:
        fh.write("[12:01:00 INFO]: System chat: <Alex> druga z logu\n")
    os.utime(latest, (t + 1, t + 1))
    inst.refresh_chat_history()
    assert [(m["name"], m["text"]) for m in inst.chat] == [
        ("Steve", "pierwsza"),
        ("Alex", "druga z logu"),
    ]


def test_public_stats_syncs_new_log_messages_even_when_chat_is_non_empty(tmp_path, monkeypatch):
    from app import api
    from app.models import PublicPageSettings, WebMapSettings

    monkeypatch.setattr(mgr, "SERVERS_DIR", tmp_path)
    monkeypatch.setattr(api, "manager", SimpleNamespace(proxy=None), raising=False)
    inst, _ = _inst()
    inst.cfg = SimpleNamespace(
        id="srv", name="S", subdomain="s", type="paper", mc_version="1.21.4",
        loader_version="1.21.4", webmap=WebMapSettings(), public_page=PublicPageSettings(),
    )
    inst.dir  # property uses SERVERS_DIR / cfg.id
    logs = tmp_path / "srv" / "logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_text("[12:00:00] [Server thread/INFO]: <Steve> pierwsza\n", encoding="utf-8")
    inst.state = "offline"
    inst.started_at = None
    inst.online = {}
    inst.players_store = SimpleNamespace(data={"known": {}})
    inst.props = lambda: {}
    inst.address = lambda: "s.example.com"
    inst.online_mode = True
    inst.icon_version = lambda: 0
    inst.refresh_chat_history()
    assert len(inst.chat) == 1

    with latest.open("a", encoding="utf-8") as fh:
        fh.write("[12:01:00 INFO]: System chat: <Alex> druga tylko z logu\n")
    t = time.time() + 1
    os.utime(latest, (t, t))

    stats = api._public_stats(inst)
    assert [(m["name"], m["text"]) for m in stats["chat"]] == [
        ("Steve", "pierwsza"),
        ("Alex", "druga tylko z logu"),
    ]
