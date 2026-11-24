"""Everything that can be updated, in one place (the "Aktualizacje" tab).

* Panel components: what the panel itself runs next to the game servers (Geyser, PicoLimbo, the
  Bedrock router extension). New components are added to ``COMPONENTS``.
* Server content: plugins/mods, datapacks and resource packs of each server. New versions always
  come from Modrinth and must be tagged for the server's exact Minecraft version and loader.

Only files the panel installed (from Modrinth or CurseForge) are ever replaced. A file a player
uploaded – or one that came inside a modpack, which the panel cannot tell apart from an upload – is
listed but never changed. Updates keep each file's state: a disabled plugin stays disabled, a
disabled datapack stays disabled, the resource pack sent to players stays the active one.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Optional

from . import content, datapacks, net, picolimbo, resourcepacks
from .modpacks import MODRINTH, ModpackError

if TYPE_CHECKING:
    from .manager import Manager, ServerInstance

log = logging.getLogger("varden.updates")

CACHE_TTL = 600.0
PANEL_SOURCES = ("modrinth", "curseforge")  # installed by the panel; anything else counts as uploaded
_pack_cache: dict[tuple, tuple[float, Optional[dict]]] = {}
_upstream_cache: dict[str, tuple[float, Optional[str]]] = {}


# ====================================================================== panel components

async def _github_latest(repo: str, refresh: bool = False) -> Optional[str]:
    """Newest release tag of a GitHub repository (the release page redirect: no API rate limit)."""
    hit = _upstream_cache.get(repo)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    tag = None
    try:
        r = await net.client().head(f"https://github.com/{repo}/releases/latest", follow_redirects=False)
        m = re.search(r"/releases/tag/([^/?#]+)$", r.headers.get("location", ""))
        tag = m.group(1) if m else None
    except Exception as exc:  # noqa: BLE001 – an unreachable GitHub must not break the page
        log.info("GitHub release check for %s failed: %s", repo, exc)
        if hit:
            return hit[1]
    _upstream_cache[repo] = (time.time(), tag)
    return tag


async def _geyser_latest(project: str, version: str, refresh: bool = False) -> Optional[str]:
    from .bedrock import DOWNLOAD_API
    key = f"geyser:{project}:{version}"
    hit = _upstream_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    latest = None
    try:
        meta = await net.get_json(f"{DOWNLOAD_API}/{project}/versions/{version}/builds/latest")
        latest = f"{meta['version']}-b{meta['build']}"
    except Exception as exc:  # noqa: BLE001
        log.info("Geyser version check failed: %s", exc)
        if hit:
            return hit[1]
    _upstream_cache[key] = (time.time(), latest)
    return latest


async def components(m: "Manager", refresh: bool = False) -> list[dict]:
    out = []
    bedrock = getattr(m, "bedrock", None)
    if bedrock is not None:
        from .bedrock import GEYSER_PREVIEW_PROJECT, ROUTER_VERSION
        project, version = bedrock.geyser_source()
        latest = await _geyser_latest(project, version, refresh)
        channel = bedrock.geyser_channel_info()
        installed = bedrock.geyser_version or None
        out.append({
            "id": "geyser", "title": "Geyser", "about": "Bramka dla graczy Minecraft Bedrock (telefony, konsole, Windows).",
            "url": "https://geysermc.org", "version": installed, "latest": latest,
            "update_available": bool(installed and latest and installed != latest),
            "installed": bool(installed), "action": "update",
            "note": None if bedrock.enabled() else "Geyser pobiera się i aktualizuje, gdy któryś serwer ma włączony Bedrock.",
            "channel": channel["channel"], "preview": channel["preview"],
            "testing": project == GEYSER_PREVIEW_PROJECT,
        })
        out.append({
            "id": "bedrock_router", "title": "Router Bedrock Varden", "url": None,
            "about": "Rozszerzenie Geysera kierujące graczy Bedrock na właściwy serwer.",
            "version": ROUTER_VERSION, "latest": ROUTER_VERSION, "update_available": False, "installed": True,
            "action": "image", "note": "Budowane razem z obrazem Varden – aktualizuje się z nowym obrazem.",
        })
    pico = picolimbo.status()
    upstream = await _github_latest(pico.get("repo") or picolimbo.REPO, refresh)
    current = pico.get("tag")
    out.append({
        "id": "picolimbo", "title": "PicoLimbo", "url": f"https://github.com/{pico.get('repo') or picolimbo.REPO}",
        "about": "Lobby logowania (/login, /register) dla serwerów offline-mode.",
        "version": pico.get("version"), "latest": picolimbo.display_version(upstream),
        "update_available": bool(current and upstream and current != upstream),
        "installed": bool(pico.get("version")), "action": "local" if pico.get("source") == "local" else "image",
        "note": ("Lokalna binarka (VARDEN_PICOLIMBO_BINARY) – panel jej nie zmienia." if pico.get("source") == "local"
                 else "Kompilowane z kodu autora przy budowaniu obrazu Varden – nowa wersja trafia do panelu "
                      "razem z nowym obrazem."),
    })
    return out


async def update_component(m: "Manager", cid: str) -> dict:
    if cid == "geyser":
        bedrock = getattr(m, "bedrock", None)
        if bedrock is None:
            raise ModpackError("Bramka Bedrock nie działa w tym panelu")
        if not bedrock.enabled():
            raise ModpackError("Włącz Bedrock dla któregoś serwera – Geyser pobiera się dopiero wtedy")
        bedrock.recheck_geyser()  # downloads the newest build of the chosen channel and restarts Geyser
        return {"started": True}
    raise ModpackError("Ten składnik aktualizuje się razem z obrazem Docker Varden")


# ====================================================================== server content

def _panel_installed(source: Optional[str]) -> bool:
    return source in PANEL_SOURCES


async def _content_items(inst: "ServerInstance", refresh: bool) -> list[dict]:
    kind = content.kind_for(inst.cfg.type)
    if not kind:
        return []
    if refresh:
        content._verify_cache.clear()
    # The origin is read from the panel's own records before the Modrinth check fills in sources.
    meta = content.load_meta(inst).get("files", {})
    data = await content.inventory(inst, check=True)
    out = []
    for it in data["items"]:
        origin = meta.get(it["file"], {}).get("source")
        item = {"id": f"{kind}:{it['file']}", "kind": kind, "file": it["file"], "title": it.get("title") or it["file"],
                "icon": it.get("icon"), "version": it.get("version_number"), "enabled": it.get("enabled", True),
                "origin": origin or "upload", "latest": None, "update_available": False, "skip": None}
        if origin == "managed":
            item["skip"] = "Zarządzany przez panel (Bedrock) – aktualizuje się sam."
        elif not _panel_installed(origin):
            item["skip"] = ("Z modpacka – panel nie zmienia plików modpacka." if inst.cfg.modpack
                            else "Wgrany ręcznie – panel go nie zmienia.")
        up = it.get("update")
        if up and not item["skip"]:
            item.update({"latest": up["version_number"], "version_id": up["version_id"], "update_available": True,
                         "project_id": it.get("project_id")})
        elif up:
            item["latest"] = up["version_number"]
        out.append(item)
    return out


async def _pack_latest(project_id: str, mc: str, loaders: list[str], refresh: bool) -> Optional[dict]:
    """Newest release of a pack tagged for exactly this Minecraft version (Modrinth)."""
    key = (project_id, mc, tuple(loaders))
    hit = _pack_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    versions = await content.modrinth_versions(project_id, mc, loaders)
    rel = [v for v in versions if v.get("version_type") == "release"]
    latest = (rel or versions or [None])[0]
    _pack_cache[key] = (time.time(), latest)
    return latest


async def _datapack_items(inst: "ServerInstance", refresh: bool) -> list[dict]:
    out = []
    for p in await asyncio.to_thread(datapacks.list_packs, inst):
        item = {"id": f"datapack:{p['file']}", "kind": "datapack", "file": p["file"], "title": p["name"],
                "icon": p.get("icon_url"), "version": p.get("version"), "enabled": p["enabled"],
                "origin": p.get("source") or "upload", "latest": None, "update_available": False, "skip": None}
        if p.get("source") != "modrinth" or not p.get("project_id"):
            item["skip"] = "Wgrany ręcznie – panel go nie zmienia."
        elif p.get("folder"):
            item["skip"] = "Folder, nie plik ZIP – panel go nie zmienia."
        else:
            await _check_pack(item, p["project_id"], p.get("version"), p.get("version_id"), inst.cfg.mc_version,
                              ["datapack"], refresh)
        out.append(item)
    return out


async def _resourcepack_items(inst: "ServerInstance", refresh: bool) -> list[dict]:
    out = []
    active = resourcepacks.active_id(inst, inst.m) if hasattr(inst, "m") else None
    for p in await asyncio.to_thread(resourcepacks.load_index, inst):
        item = {"id": f"resourcepack:{p['id']}", "kind": "resourcepack", "file": p["id"], "title": p.get("name"),
                "icon": p.get("icon_url"), "version": p.get("version"), "enabled": p["id"] == active,
                "origin": p.get("source") or "upload", "latest": None, "update_available": False, "skip": None}
        if not _panel_installed(p.get("source")) or not p.get("project_id"):
            item["skip"] = "Wgrana ręcznie – panel jej nie zmienia."
        elif p.get("source") == "curseforge":
            item["skip"] = "Z CurseForge – nie ma jej odpowiednika do sprawdzenia w Modrinth."
        else:
            await _check_pack(item, p["project_id"], p.get("version"), p.get("version_id"), inst.cfg.mc_version,
                              ["minecraft"], refresh)
        out.append(item)
    return out


async def _check_pack(item: dict, project_id: str, version: Optional[str], version_id: Optional[str], mc: str,
                      loaders: list[str], refresh: bool) -> None:
    try:
        latest = await _pack_latest(project_id, mc, loaders, refresh)
    except Exception as exc:  # noqa: BLE001
        item["error"] = f"Nie udało się sprawdzić w Modrinth: {exc}"
        return
    if not latest:
        item["note"] = f"Brak wersji dla Minecrafta {mc} w Modrinth."
        return
    item["latest"] = latest["version_number"]
    newer = latest["id"] != version_id if version_id else latest["version_number"] != version
    if newer:
        item.update({"update_available": True, "version_id": latest["id"], "project_id": project_id})


async def server_updates(inst: "ServerInstance", refresh: bool = False) -> dict:
    """Every updatable file of a server with its current and newest Modrinth version."""
    groups = await asyncio.gather(_content_items(inst, refresh), _datapack_items(inst, refresh),
                                  _resourcepack_items(inst, refresh), return_exceptions=True)
    items, errors = [], []
    for g in groups:
        if isinstance(g, Exception):
            errors.append(str(g))
        else:
            items.extend(g)
    return {
        "id": inst.cfg.id, "name": inst.cfg.name, "type": inst.cfg.type, "mc_version": inst.cfg.mc_version,
        "running": bool(inst.running), "modpack": bool(inst.cfg.modpack),
        "items": items, "available": sum(1 for i in items if i["update_available"]),
        "errors": errors, "checked_at": time.time(),
    }


async def apply(inst: "ServerInstance", ids: Optional[list[str]] = None, api_key: str = "") -> dict:
    """Install the newest version for the chosen items (all available updates when ``ids`` is None)."""
    state = await server_updates(inst)
    wanted = [i for i in state["items"] if i["update_available"] and (ids is None or i["id"] in ids)]
    done, failed = [], []
    for item in wanted:
        try:
            await _apply_one(inst, item, api_key)
            done.append({"id": item["id"], "title": item["title"], "from": item["version"], "to": item["latest"]})
            inst.panel_log(f"Zaktualizowano {item['title']}: {item['version'] or '?'} → {item['latest']}")
        except Exception as exc:  # noqa: BLE001 – one broken download must not stop the rest
            failed.append({"id": item["id"], "title": item["title"], "error": str(exc)})
            inst.panel_log(f"Aktualizacja {item['title']} nieudana: {exc}")
    content._verify_cache.clear()
    return {"updated": done, "failed": failed, "restart_required": bool(done and inst.running)}


async def _apply_one(inst: "ServerInstance", item: dict, api_key: str) -> None:
    kind = item["kind"]
    if kind in ("plugin", "mod"):
        await _update_content(inst, item, api_key)
    elif kind == "datapack":
        version = await net.get_json(f"{MODRINTH}/version/{item['version_id']}")
        await datapacks.update_modrinth(inst, item["file"], version)
    elif kind == "resourcepack":
        await resourcepacks.update_modrinth(inst, inst.m, item["file"], item["version_id"])


async def _update_content(inst: "ServerInstance", item: dict, api_key: str) -> None:
    """Install the new Modrinth version of a plugin/mod and remove the old jar (also one from CurseForge)."""
    old = item["file"]
    ins = content.Installer(inst, api_key)
    await ins.modrinth(item["project_id"], item["version_id"])
    meta = content.load_meta(inst)
    new = next((name for name, info in meta.get("files", {}).items() if info.get("version_id") == item["version_id"]),
               None)
    if new and new != old:
        content.remove(inst, old)  # e.g. the old copy came from CurseForge under another project id
    if new and not item.get("enabled", True):
        content.toggle(inst, new, False)  # a disabled plugin stays disabled after the update

