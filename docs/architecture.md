# Architecture, development and security

[← Back to README](../README.md)

## Overview

```
                 ┌──────────────────────────── varden container ───────────────────────────┐
 browser ───────▶│ :8080  FastAPI ── REST /api/* ── WebSocket /api/servers/{id}/ws            │
                 │          │           Vue 3 SPA (static files)                              │
                 │          ▼                                                                 │
                 │      Manager ── ServerInstance × N ── java -Xmx… -jar …  (:25600, :25601…)  │
                 │          ▲                                                                 │
                 │          │ IPC (Unix socket, auth/routing/MOTD/events only)                │
                 │          ▼                                                                 │
 Minecraft ─────▶│ :25565 varden-proxy (Rust/tokio) ── TCP tunnel / HTTP resource packs     │
 voice (UDP) ───▶│ :24454 varden-proxy (Rust/tokio) ── UUID → internal SVC port (:24500…)   │
 Bedrock ───────▶│ :19132 Geyser + VardenRouter ── hostname ──▶ :25565 proxy                │
                 │   Java (Temurin, auto)   Providers (Mojang/Paper/Purpur/Fabric/…)          │
                 │   Backups (zip)          Modpacks & content (Modrinth / CurseForge)        │
                 └──────────────────────────────────── /data ─────────────────────────────────┘
```

## Backend

`backend/app` – Python 3.14, FastAPI, asyncio. `backend/proxy` – Rust (`tokio`). `backend/bedrock_ext` – Java Geyser extension.

| Module | Role |
|--------|------|
| `main.py` | App and lifecycle (graceful server shutdown on `docker stop`), CSRF, SPA serving |
| `api.py` | REST + WebSocket endpoints |
| `manager.py` | Server processes, console, metrics (psutil), log parsing (players, chat, whitelist rejections), scheduler, idle shutdown |
| `backend/proxy/` | Rust TCP + UDP transport proxy (`25565/tcp`, `24454/udp`): PROXY protocol v2, Handshake/Login Start framing, TCP tunnelling, RTT sampling, HTTP resource-pack streaming, UDP voice routing by player UUID |
| `proxy.py` | Proxy control plane & IPC: host routing, offline/starting/waking MOTD and kick messages, whitelist & start-on-join triggers, login lobby gate, resource-pack tokens |
| `fingerprint.py` | Passive Client Information / Plugin Message sniffer used by the session fingerprint |
| `auth_sessions.py`, `auth_lobby.py`, `picolimbo.py` | Player accounts and sessions, the hidden PicoLimbo login lobby and its Docker-built binary |
| `voice.py` | Simple Voice Chat control plane |
| `bedrock.py` | Shared Geyser gateway: download, `config.yml`, hostname routes, global auth mode |
| `providers.py`, `java.py` | Version discovery and engine installation; Java selection and download from Adoptium |
| `modpacks.py`, `content.py`, `datapacks.py`, `resourcepacks.py` | Modpacks, mods/plugins (search, dependencies, compatibility checks), datapacks, resource pack hosting |
| `updates.py` | The Updates tab: panel components and per-server updates from Modrinth |
| `managedjar.py` | Shared logic for plugins/mods the panel installs and keeps updated |
| `webmap.py`, `squaremap.py` | squaremap / Dynmap / BlueMap setup, serving and 24/7 preview |
| `lod.py`, `skins.py`, `chunky.py` | Distant Horizons / LOD Server Support, SkinsRestorer, Chunky pre-generation |
| `playerstats.py`, `blockrender.py`, `itemicons.py` | Player statistics and 3D block / item icons for the public page |
| `backups.py`, `storage.py` | Consistent ZIP backups; storage backends (disk, folder, S3, WebDAV) |
| `players.py`, `files.py` | whitelist/ops/bans; safe paths and streamed ZIP downloads |
| `auth.py`, `reset_password.py` | scrypt passwords, HMAC-signed sessions, login rate limiting; terminal password reset |

## Frontend

`frontend/` – Vue 3 + Vite, no UI framework: a custom design system with light and dark themes, Lucide icons, and Inter / JetBrains Mono / Pixelify Sans fonts bundled locally (no external CDNs).

**Translations**: Polish source strings are the keys (`$t('…')`), and `frontend/src/locales/en.js` holds the English dictionary. Messages coming from the backend are matched against templates in the same file. A missing translation falls back to Polish, so new features never break the UI.

## `/data` layout

```
/data
├── servers/<id>/          # server files (world, mods/, plugins/, server.properties…)
│   └── .varden/         # panel metadata: content.json (mods/plugins), resourcepacks/
├── backups/<id>/*.zip     # backups + .json metadata
├── java/temurin-<N>/      # downloaded Java runtimes
├── panel/                 # servers.json, settings.json, users.json, secret.key, players/
├── bedrock/               # Geyser jar, config.yml, logs, extensions/VardenRouter
├── auth-lobby/            # hidden PicoLimbo authentication lobby and config
├── auth/                  # player credentials, sessions and informational client fingerprints
└── tmp/                   # temporary install files
```

## Local development

```bash
# backend
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
VARDEN_DATA=./data VARDEN_PROXY_PORT=25599 VARDEN_VOICE_PORT=24499 VARDEN_BEDROCK_PORT=19199 uvicorn app.main:app --reload --port 8080

# frontend (proxies /api to :8080, hot reload)
cd frontend && npm install && npm run dev

# tests
cd backend && python -m pytest
```

API docs (OpenAPI): `http://localhost:8080/api/docs`.

Building the image yourself:

```bash
docker build -t varden:dev .
```

## Security

- The panel runs servers as an unprivileged user (`PUID`/`PGID`), never as root.
- There is no default administrator account: the first person to open a fresh panel creates it in the setup wizard, so finish the wizard right after starting the container.
- State-changing requests require an `X-Requested-With` header (CSRF protection), and the WebSocket checks `Origin`.
- Logins are limited to 8 attempts per IP and 20 per account every 5 minutes, counted before the password is checked. Unknown usernames take as long as known ones, and an open console closes when its session is no longer valid (password change, deleted account).
- Logging out revokes that session cookie on the server, and cookies are tied to the account they were issued for (a deleted and re-created user does not inherit old sessions).
- Game servers listen on `127.0.0.1` only. Players always come in through the Varden proxy, which refuses logins whose player name it cannot read instead of passing them through.
- Paths in the file manager, modpack archives and backups are checked against directory traversal (zip-slip).
- Resource pack URLs contain an unguessable token, and the voice ports of individual servers are bound to `127.0.0.1`.
- Servers start with `-Dlog4j2.formatMsgNoLookups=true` (Log4Shell protection for older versions).
