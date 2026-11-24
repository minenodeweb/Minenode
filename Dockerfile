# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------- frontend build
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- Bedrock router extension (Java)
# The extension is compiled here, not in the runtime image and not taken from
# the repository: a stale committed JAR must never hide broken Java sources.
FROM eclipse-temurin:21-jdk AS bedrock_ext
WORKDIR /ext
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Compile against the same Geyser release the panel downloads at runtime.
RUN curl -fsSL -o /tmp/geyser-standalone.jar \
      https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/standalone
COPY backend/bedrock_ext/ ./
RUN sh build.sh /tmp/geyser-standalone.jar

# ---------------------------------------------------------------- Rust transport proxy (TCP + Voice UDP)
FROM rust:1-slim-bookworm AS rust_proxy
WORKDIR /proxy
COPY backend/proxy/Cargo.toml backend/proxy/Cargo.lock* ./
COPY backend/proxy/src ./src
RUN cargo build --release

# ---------------------------------------------------------------- PicoLimbo auth lobby (Rust)
# Compiled at image build time from the author's latest stable release + MineNode's login patch;
# no PicoLimbo source files are stored in this repository.
# Pass --build-arg PICOLIMBO_TAG=<tag> (done automatically by deploy.cmd) or --no-cache-filter picolimbo
# to pick up a newly published upstream release when rebuilding.
FROM rust:1-slim-bookworm AS picolimbo
WORKDIR /picolimbo
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates patch binutils \
 && rm -rf /var/lib/apt/lists/* \
 && rustup set profile minimal
COPY backend/app/assets/picolimbo/minenode-auth.patch /picolimbo/minenode-auth.patch
ARG PICOLIMBO_REPO=Quozul/PicoLimbo
ARG PICOLIMBO_TAG=latest
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/picolimbo/target \
    set -eu; \
    unset RUSTUP_TOOLCHAIN; \
    sed -i 's/\r$//' /picolimbo/minenode-auth.patch; \
    if ! printf '%s' "$PICOLIMBO_REPO" | grep -Eq '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'; then \
      echo "Invalid PICOLIMBO_REPO: $PICOLIMBO_REPO" >&2; exit 1; \
    fi; \
    TAG="$PICOLIMBO_TAG"; \
    if [ -z "$TAG" ] || [ "$TAG" = "latest" ]; then \
      LOC=$(curl -fsSI -o /dev/null -w '%{redirect_url}' "https://github.com/${PICOLIMBO_REPO}/releases/latest" || true); \
      TAG=$(printf '%s' "$LOC" | sed -n 's|.*/releases/tag/\([^/?#]*\).*|\1|p'); \
      if [ -z "$TAG" ]; then \
        TAG=$(curl -fsSL -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
          "https://api.github.com/repos/${PICOLIMBO_REPO}/releases/latest" \
          | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1); \
      fi; \
    fi; \
    TAG=$(printf '%s' "$TAG" | sed 's/%2B/+/g; s/%2b/+/g'); \
    if [ -z "$TAG" ] || ! printf '%s' "$TAG" | grep -Eq '^[A-Za-z0-9._+-]{1,80}$'; then \
      echo "Could not resolve a valid PicoLimbo release tag (got: '$TAG')" >&2; exit 1; \
    fi; \
    echo "==> Building PicoLimbo ${TAG} from ${PICOLIMBO_REPO}"; \
    ENCODED_TAG=$(printf '%s' "$TAG" | sed 's/+/%2B/g'); \
    rm -rf /picolimbo/src /out; \
    mkdir -p /picolimbo/src /out/licenses; \
    curl -fsSL -o /tmp/picolimbo.tar.gz "https://github.com/${PICOLIMBO_REPO}/archive/refs/tags/${ENCODED_TAG}.tar.gz"; \
    tar -xzf /tmp/picolimbo.tar.gz --strip-components=1 -C /picolimbo/src; \
    rm -f /tmp/picolimbo.tar.gz; \
    test -f /picolimbo/src/Cargo.toml && test -d /picolimbo/src/pico_limbo; \
    cd /picolimbo/src; \
    patch -p1 --forward --batch -i /picolimbo/minenode-auth.patch; \
    cargo add --quiet --package pico_limbo hex@0.4 getrandom@0.3; \
    rm -f /picolimbo/target/release/pico_limbo; \
    CARGO_TARGET_DIR=/picolimbo/target cargo build --release --bin pico_limbo; \
    cp /picolimbo/target/release/pico_limbo /out/pico_limbo; \
    strip /out/pico_limbo; \
    chmod 0755 /out/pico_limbo; \
    for lic in LICENSE LICENSE.md LICENSE.txt LICENSE-MIT; do \
      if [ -f "$lic" ]; then cp "$lic" /out/licenses/PICOLIMBO-LICENSE; break; fi; \
    done; \
    PATCH_ID=$(sha256sum /picolimbo/minenode-auth.patch | cut -c1-8); \
    BIN_SHA=$(sha256sum /out/pico_limbo | awk '{print $1}'); \
    VERSION="${TAG#v}"; \
    BUILT_AT=$(date +%s); \
    printf '{"tag":"%s","version":"%s","patch":"%s","repo":"%s","source":"image","sha256":"%s","built_at":%s}\n' \
      "$TAG" "$VERSION" "$PATCH_ID" "$PICOLIMBO_REPO" "$BIN_SHA" "$BUILT_AT" > /out/picolimbo-version.json

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="MineNode" \
      org.opencontainers.image.description="Minecraft server management panel with built-in reverse proxy"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINENODE_DATA=/data \
    MINENODE_STATIC=/app/static \
    MINENODE_PORT=8080 \
    MINENODE_PROXY_PORT=25565 \
    MINENODE_VOICE_PORT=24454 \
    MINENODE_BEDROCK_PORT=19132 \
    TZ=Europe/Warsaw

# tini: proper PID 1 / signal forwarding; fontconfig+freetype: needed by some mods (maps, image rendering)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates tzdata fontconfig libfreetype6 procps \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 minenode && useradd -u 1000 -g 1000 -m -d /home/minenode minenode

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY --from=rust_proxy /proxy/target/release/minenode-proxy ./bin/minenode-proxy
COPY --from=picolimbo /out/pico_limbo ./bin/pico_limbo
COPY --from=picolimbo /out/picolimbo-version.json ./bin/picolimbo-version.json
COPY --from=picolimbo /out/licenses ./licenses
COPY --from=bedrock_ext /ext/MineNodeRouter.jar ./bedrock_ext/MineNodeRouter.jar
COPY --from=web /web/dist ./static
COPY docker/entrypoint.sh /entrypoint.sh
COPY reset_password.sh /usr/local/bin/reset_password.sh
# strip Windows line endings (a CRLF checkout breaks the shebang: "exec /entrypoint.sh failed")
RUN sed -i 's/\r$//' /entrypoint.sh /usr/local/bin/reset_password.sh && chmod +x /entrypoint.sh /usr/local/bin/reset_password.sh \
 && mkdir -p /data && chown minenode:minenode /data

VOLUME ["/data"]
EXPOSE 8080 25565 24454/udp 19132/tcp 19132/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MINENODE_PORT\",\"8080\")}/api/health',timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
# X-Forwarded-For is only trusted from MINENODE_TRUSTED_PROXIES (your reverse proxy). Trusting it from
# anyone would let clients pick their own IP and bypass the login attempt limit.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${MINENODE_PORT} --proxy-headers --forwarded-allow-ips=\"${MINENODE_TRUSTED_PROXIES:-127.0.0.1}\" --no-server-header"]
