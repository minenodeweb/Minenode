"""Varden – Minecraft server management panel (formerly MineNode)."""
import os as _os

# Settings from before the rename (MINENODE_*) keep working: each one fills its VARDEN_* name unless
# that name was set to something of its own (a value other than the Docker image's default).
_LEGACY_PREFIX = "MINE" + "NODE_"
_IMAGE_DEFAULTS = {"DATA": "/data", "STATIC": "/app/static", "PORT": "8080", "PROXY_PORT": "25565",
                   "VOICE_PORT": "24454", "BEDROCK_PORT": "19132"}
for _key, _value in list(_os.environ.items()):
    if _key.startswith(_LEGACY_PREFIX):
        _suffix = _key[len(_LEGACY_PREFIX):]
        _current = _os.environ.get("VARDEN_" + _suffix)
        if _current is None or _current == _IMAGE_DEFAULTS.get(_suffix):
            _os.environ["VARDEN_" + _suffix] = _value
