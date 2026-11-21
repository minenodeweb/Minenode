from app.proxy import MinecraftProxy


def test_player_pings_and_stats_come_from_rust_telemetry():
    p = MinecraftProxy(manager=None, host="127.0.0.1", port=25565)

    p.handle_telemetry({
        "tcp_listening": True,
        "tcp_error": None,
        "active": 3,
        "total": 10,
        "bytes_up": 1024,
        "bytes_down": 2048,
        "packs_served": 2,
        "per_server": {"abc": 3},
        "pings": {
            "abc": {
                "steve": 42,
                "bedrockguy": None,
                "v6local": None,
            }
        },
    })

    assert p.player_pings("abc") == {"steve": 42, "bedrockguy": None, "v6local": None}
    assert p.player_pings("missing") == {}
    assert p.stats() == {
        "host": "127.0.0.1",
        "port": 25565,
        "active": 3,
        "total": 10,
        "bytes_up": 1024,
        "bytes_down": 2048,
        "per_server": {"abc": 3},
        "listening": True,
        "error": None,
        "packs_served": 2,
    }


def test_player_pings_update_when_players_disconnect():
    p = MinecraftProxy(manager=None, host="127.0.0.1", port=25565)
    p.handle_telemetry({
        "tcp_listening": True,
        "active": 1,
        "per_server": {"srv": 1},
        "pings": {"srv": {"steve": 18}},
    })
    assert p.player_pings("srv") == {"steve": 18}

    p.handle_telemetry({
        "tcp_listening": True,
        "active": 0,
        "per_server": {},
        "pings": {},
    })
    assert p.player_pings("srv") == {}
    assert p.per_server == {}
