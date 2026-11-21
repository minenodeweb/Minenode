"""Packet framing/parsing tests for the informational client fingerprint."""
import zlib

from app.fingerprint import PacketSniffer, parse_client_information, parse_plugin_message


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def mc_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return varint(len(data)) + data


def client_info(appended: bytes = bytes([0, 1])) -> bytes:
    # locale, view distance, chat mode, chat colours, skin parts, main hand,
    # text filtering, server listings (the last two are absent on old clients).
    return mc_string("en_us") + bytes([12, 0, 1, 0x7F]) + varint(0) + appended


def frame(body: bytes) -> bytes:
    return varint(len(body)) + body


def test_parse_client_information_for_current_and_legacy_clients():
    current = parse_client_information(client_info())
    assert current == {
        "locale": "en_us",
        "view_distance": 12,
        "chat_mode": 0,
        "chat_colors": True,
        "skin_parts": 0x7F,
        "skin_parts_enabled": ["cape", "jacket", "left_sleeve", "right_sleeve",
                               "left_pants", "right_pants", "hat"],
        "main_hand": 0,
        "text_filtering": False,
        "allow_listings": True,
        "particle_status": None,
    }
    legacy = parse_client_information(client_info(b""))
    assert legacy["locale"] == "en_us"
    assert legacy["text_filtering"] is None
    assert legacy["allow_listings"] is None


def test_parse_client_information_for_1_7_and_1_8_clients():
    # 1.8: locale, view distance, chat mode, colours, skin parts – no main hand.
    v18 = mc_string("pl_pl") + bytes([8, 0, 1, 0x02])
    parsed = parse_client_information(v18, protocol=47)
    assert parsed["main_hand"] is None
    assert parsed["skin_parts_enabled"] == ["jacket"]

    # 1.7: chat flags (visibility + colour bit), difficulty, show cape.
    v17 = mc_string("de_de") + bytes([6, 2 | 4, 2, 1])
    parsed = parse_client_information(v17, protocol=5)
    assert parsed["chat_mode"] == 2
    assert parsed["chat_colors"] is True
    assert parsed["skin_parts_enabled"] == ["cape"]
    assert parsed["main_hand"] is None
    assert parse_client_information(v17 + b"\x00", protocol=5) is None


def test_parse_plugin_message_requires_a_channel_and_matching_payload_length():
    brand = mc_string("vanilla")
    payload = mc_string("minecraft:brand") + varint(len(brand)) + brand
    assert parse_plugin_message(payload) == ("minecraft:brand", brand)
    broken = mc_string("minecraft:brand") + varint(99) + brand
    assert parse_plugin_message(broken) is None
    assert parse_plugin_message(mc_string("en_us") + varint(0)) is None


def test_sniffer_understands_plain_and_compressed_frames():
    plain = PacketSniffer(769)
    plain.feed(frame(varint(0) + client_info()))
    assert plain.client_info is None  # deferred until the session boundary
    plain.flush()
    assert plain.client_info["locale"] == "en_us"

    body = varint(0) + client_info()
    compressed_body = varint(len(body)) + zlib.compress(body)
    compressed = PacketSniffer(769)
    compressed.feed(frame(compressed_body))
    compressed.flush()
    assert compressed.client_info["locale"] == "en_us"

    brand = mc_string("vanilla")
    plugin_body = varint(1) + mc_string("minecraft:brand") + varint(len(brand)) + brand
    plugin = PacketSniffer(769)
    plugin.feed(frame(plugin_body))
    plugin.flush()
    assert plugin.brand == "vanilla"
    assert plugin.plugin_messages["minecraft:brand"]["count"] == 1


def test_sniffer_survives_malformed_and_split_frames():
    sniffer = PacketSniffer()
    good = frame(varint(0) + client_info())
    sniffer.feed(good[:3])
    assert sniffer.client_info is None
    sniffer.feed(good[3:])
    sniffer.flush()
    assert sniffer.client_info["locale"] == "en_us"

    bad = PacketSniffer()
    bad.feed(varint(50) + b"\x00" * 50)  # complete garbage frame
    bad.feed(frame(varint(0) + client_info()))
    bad.flush()
    assert bad.client_info["locale"] == "en_us"


def test_sniffer_coalesces_capture_writes_and_flushes_on_close(monkeypatch):
    from app import fingerprint

    now = [100.0]
    monkeypatch.setattr(fingerprint.time, "monotonic", lambda: now[0])
    snapshots = []
    sniffer = PacketSniffer(769, on_capture=snapshots.append)
    data = mc_string("vanilla")
    for _ in range(20):
        payload = varint(1) + mc_string("minecraft:brand") + varint(len(data)) + data
        sniffer.feed(frame(payload))
        now[0] += 0.05

    sniffer.flush()
    assert len(snapshots) == 1  # one analysis/write at the session boundary
    assert snapshots[-1]["plugin_messages"][0]["count"] == 20


def test_sniffer_ignores_gameplay_plugin_messages_after_join_window(monkeypatch):
    from app import fingerprint

    now = [100.0]
    monkeypatch.setattr(fingerprint.time, "monotonic", lambda: now[0])
    snapshots = []
    sniffer = PacketSniffer(769, on_capture=snapshots.append, capture_seconds=5)

    brand = mc_string("vanilla")
    sniffer.feed(frame(varint(1) + mc_string("minecraft:brand") + varint(len(brand)) + brand))
    now[0] += 6
    voice = mc_string("voicechat")
    sniffer.feed(frame(varint(1) + mc_string("voicechat:secret") + varint(len(voice)) + voice))

    sniffer.flush()
    assert len(snapshots) == 1
    assert [m["channel"] for m in snapshots[0]["plugin_messages"]] == ["minecraft:brand"]
    assert "voicechat:secret" not in sniffer.plugin_messages


def test_sniffer_does_not_publish_a_partial_first_chunk(monkeypatch):
    from app import fingerprint

    now = [100.0]
    monkeypatch.setattr(fingerprint.time, "monotonic", lambda: now[0])
    snapshots = []
    sniffer = PacketSniffer(769, on_capture=snapshots.append, capture_seconds=30)

    sniffer.feed(frame(varint(0) + client_info()))
    now[0] += 1
    brand = mc_string("vanilla")
    sniffer.feed(frame(varint(1) + mc_string("minecraft:brand") + varint(len(brand)) + brand))
    now[0] += 1
    sniffer.flush()

    assert len(snapshots) == 1
    assert snapshots[0]["client_info"]["locale"] == "en_us"
    assert snapshots[0]["brand"] == "vanilla"
