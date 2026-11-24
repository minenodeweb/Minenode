# Varden

**A modern, self-hosted panel for Minecraft servers – in a single Docker container.**
Java and Bedrock players, mods and plugins from Modrinth and CurseForge, live web maps, voice chat, and automated backups – all behind one port.

[![GitHub](https://img.shields.io/badge/GitHub-Varden--app%2FVarden-181717?logo=github&logoColor=white)](https://github.com/Varden-app/Varden)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/Varden-app/Varden/blob/main/LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/zzabek/varden?logo=docker&logoColor=white&cacheSeconds=300)](https://hub.docker.com/r/zzabek/varden)
[![Docker Image Size](https://img.shields.io/docker/image-size/zzabek/varden/latest?logo=docker&logoColor=white&label=image)](https://hub.docker.com/r/zzabek/varden)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Vue 3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)
![Rust proxy](https://img.shields.io/badge/Rust-proxy-000000?logo=rust&logoColor=white)

**[GitHub Repository](https://github.com/Varden-app/Varden)** · **[Documentation](https://github.com/Varden-app/Varden/tree/main/docs)** · **[Installation Guide](https://github.com/Varden-app/Varden/blob/main/docs/installation.md)** · **[Report an Issue](https://github.com/Varden-app/Varden/issues)** · **[License](https://github.com/Varden-app/Varden/blob/main/LICENSE)**

> **Note:** Varden is under active development. Back up your worlds before major updates.
> *(Varden was previously called MineNode – existing `/data` volumes upgrade automatically, see [Upgrading from MineNode](https://github.com/Varden-app/Varden/blob/main/docs/installation.md#upgrading-from-minenode).)*

![Varden dashboard – servers, host resources and proxy](https://raw.githubusercontent.com/Varden-app/Varden/main/docs/screenshots/dashboard.webp)

---

## Key features

- **7 server engines + Custom JAR**: Vanilla, Paper, Purpur, Fabric, Quilt, Forge, NeoForge, or any uploaded `.jar`.
- **Automatic Java**: Detects the required Java version per server and downloads Eclipse Temurin (`8`–`25`) automatically.
- **Built-in Rust reverse proxy (`25565`)**: Run multiple Minecraft servers behind one port routed by subdomain (`survival.mc.example.com`), with resource packs served straight from the game port, start-on-join, and auto-stop when empty.
- **Bedrock Edition (`19132`)**: Integrated Geyser + `VardenRouter` gateway using the same hostnames.
- **Simple Voice Chat (`24454/udp`)**: Single shared UDP port routed by player UUID to each internal server.
- **Non-premium login lobby**: Built-in hidden [PicoLimbo](https://github.com/Quozul/PicoLimbo) lobby with `/register`, `/login`, and session management.
- **Mods, plugins, modpacks & 1-click updates**: Install from Modrinth and CurseForge with dependency resolution and compatibility checks; update everything per server with one click from Modrinth.
- **Live web maps, LOD & pre-generation**: Integrated **squaremap**, **Dynmap**, and **BlueMap** (with 24/7 preview while stopped), **Distant Horizons** / **Voxy** support, and **Chunky** world pre-generation that pauses while players are online.
- **Public stats page**: Per-server public page with online players, recent chat, and per-player statistics with 3D block icons.
- **Backups & portability**: Consistent scheduled backups to local disk, NAS folder, S3-compatible storage, or WebDAV, plus `.vardenpack` server export/import.
- **Modern UI**: English and Polish interface with light and dark themes.

---

## Quick start

### Docker Compose

Create `docker-compose.yml`:

```yaml
services:
  varden:
    image: zzabek/varden:latest
    container_name: varden
    restart: unless-stopped
    ports:
      - "8080:8080"           # Web panel, API and WebSocket (HTTP)
      - "25565:25565"         # Minecraft Java – one shared port for every server
      - "24454:24454/udp"     # Simple Voice Chat – shared UDP port
      - "19132:19132/tcp"     # Bedrock Edition (Geyser)
      - "19132:19132/udp"     # Bedrock Edition (Geyser)
    volumes:
      - varden-data:/data
    environment:
      TZ: Europe/Warsaw
      PUID: "1000"
      PGID: "1000"
      VARDEN_SECURE_COOKIES: "false"
    stop_grace_period: 2m     # Lets servers save their worlds on stop
    ulimits:
      nofile: { soft: 65535, hard: 65535 }

volumes:
  varden-data:
```

Start the container:

```bash
docker compose up -d
```

Open `http://<your-server-ip>:8080` and follow the first-start setup wizard.

### `docker run`

```bash
docker run -d --name varden --restart unless-stopped \
  -p 8080:8080 -p 25565:25565 -p 24454:24454/udp -p 19132:19132/tcp -p 19132:19132/udp \
  -v varden-data:/data -e TZ=Europe/Warsaw \
  --stop-timeout 120 --ulimit nofile=65535:65535 \
  zzabek/varden:latest
```

### Updating

```bash
docker compose pull && docker compose up -d
```

---

## Ports

| Port | Purpose |
|------|---------|
| `8080/tcp` | Web panel, REST API, WebSocket, public stats and maps |
| `25565/tcp` | Minecraft Java reverse proxy (routing by subdomain) & resource packs |
| `24454/udp` | Simple Voice Chat UDP proxy |
| `19132/tcp` + `19132/udp` | Bedrock Edition via Geyser (only used when Bedrock is enabled) |

---

## Documentation

Full guides, environment variables (`VARDEN_*`, `PUID`/`PGID`), HTTPS reverse proxy examples, DNS wildcard setup, and password reset instructions are on GitHub:

- **[README on GitHub](https://github.com/Varden-app/Varden)**
- **[Installation & Configuration](https://github.com/Varden-app/Varden/blob/main/docs/installation.md)**
- **[Features in Detail](https://github.com/Varden-app/Varden/blob/main/docs/features.md)**
- **[Architecture & Security](https://github.com/Varden-app/Varden/blob/main/docs/architecture.md)**

---

## Authors & Contributors

Varden is created by **Bernard Wieczorek** and **Piotr Janaszak**.

- Source code: **[github.com/Varden-app/Varden](https://github.com/Varden-app/Varden)**
- Bug reports & feature requests: **[GitHub Issues](https://github.com/Varden-app/Varden/issues)**

---

## License

Varden is open-source software licensed under the **[Apache License 2.0](https://github.com/Varden-app/Varden/blob/main/LICENSE)**.
See the **[NOTICE](https://github.com/Varden-app/Varden/blob/main/NOTICE)** file for attribution details and third-party component notices.

*Varden is not an official Minecraft product. It is not approved by or associated with Mojang or Microsoft.*
