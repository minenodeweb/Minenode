"""SkinsRestorer: skins for players on offline-mode (non-premium) servers.

On an offline-mode server Minecraft shows every player as Steve/Alex. SkinsRestorer looks up the
Minecraft Java account with the player's name and gives the player that account's skin; players
without such an account keep a default skin and can pick one themselves (``/skin set <name>``,
``/skin url <link>``). Its default settings already do this, so the panel only installs, updates and
switches it on or off (plugin on Paper/Purpur, mod on Fabric/Quilt/NeoForge – no Forge build).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from .managedjar import ManagedJar

if TYPE_CHECKING:
    from .manager import ServerInstance

SKINSRESTORER = ManagedJar(
    "skinsrestorer", "SkinsRestorer", "TsLS8Py5", r"^SkinsRestorer(?:-Mod-(?:Fabric|NeoForge))?(?:-\d[\w.\-+]*)?\.jar$",
    {"paper": ["paper"], "purpur": ["paper"], "fabric": ["fabric"], "quilt": ["fabric"], "neoforge": ["neoforge"]},
    fallback="plugin")


def supported(inst: "ServerInstance") -> bool:
    return SKINSRESTORER.engine_supported(inst)


def unsupported_reason(inst: "ServerInstance") -> Optional[str]:
    if supported(inst):
        return None
    if inst.cfg.type in ("vanilla", "custom"):
        return "Wymaga silnika z pluginami lub modami (Paper, Purpur, Fabric, Quilt, NeoForge)."
    if inst.cfg.type == "forge":
        return "SkinsRestorer nie ma wersji dla Forge – użyj NeoForge, Fabric albo Paper."
    return "Ten silnik nie jest obsługiwany."


def enabled(inst: "ServerInstance") -> bool:
    return bool(getattr(inst.cfg, "skins_enabled", False))


async def prepare(inst: "ServerInstance", refresh: bool = False) -> Optional[dict]:
    """Before a start (or when switched): install/update SkinsRestorer, or keep its jar disabled."""
    if enabled(inst) and supported(inst):
        return await SKINSRESTORER.ensure(inst, refresh=refresh)
    await asyncio.to_thread(SKINSRESTORER.disable, inst)
    return None


async def state(inst: "ServerInstance", refresh: bool = False) -> dict:
    ok = supported(inst)
    out = {"enabled": enabled(inst), "supported": ok, "reason": unsupported_reason(inst),
           "kind": "plugin" if inst.cfg.type in ("paper", "purpur") else "mod",
           "online_mode": bool(getattr(inst, "online_mode", False)), "restart_required": False}
    if ok:
        out.update(await SKINSRESTORER.status(inst, refresh=refresh))
    return out
