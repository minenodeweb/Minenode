#!/bin/sh
# Runs the panel as an unprivileged user (PUID/PGID), fixing /data ownership on first start.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Settings from before the rename to Varden (MINENODE_*) keep working: each one fills its VARDEN_* name
# unless that name was given a value of its own (anything other than this image's default).
for legacy in $(env | sed -n 's/^\(MINENODE_[A-Z0-9_]*\)=.*/\1/p'); do
  suffix=${legacy#MINENODE_}
  case "$suffix" in
    DATA) default=/data ;; STATIC) default=/app/static ;; PORT) default=8080 ;;
    PROXY_PORT) default=25565 ;; VOICE_PORT) default=24454 ;; BEDROCK_PORT) default=19132 ;;
    *) default= ;;
  esac
  eval "current=\${VARDEN_$suffix-}"
  eval "value=\${$legacy}"
  if [ -z "$current" ] || [ "$current" = "$default" ]; then
    export "VARDEN_$suffix=$value"
  fi
  echo "[varden] $legacy is deprecated – rename it to VARDEN_$suffix"
done

if [ "$(id -u)" = "0" ]; then
  if [ "$(id -g varden)" != "$PGID" ]; then groupmod -o -g "$PGID" varden; fi
  if [ "$(id -u varden)" != "$PUID" ]; then usermod -o -u "$PUID" varden; fi
  mkdir -p /data
  if [ "$(stat -c %u /data)" != "$PUID" ] || [ "$(stat -c %g /data)" != "$PGID" ]; then
    echo "[varden] Setting owner of /data to $PUID:$PGID…"
    chown -R "$PUID:$PGID" /data
  fi
  export HOME=/home/varden
  exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
fi
exec "$@"
