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

# Best-effort cgroup v2 delegation for per-server CPU quotas when /sys/fs/cgroup is mounted writable.
setup_cgroup_v2() {
  cg="/sys/fs/cgroup"
  [ -w "$cg/cgroup.subtree_control" ] || return 0
  [ -r "$cg/cgroup.controllers" ] || return 0
  grep -qw cpu "$cg/cgroup.controllers" 2>/dev/null || return 0

  mkdir -p "$cg/varden/panel" "$cg/varden/servers" 2>/dev/null || return 0
  if [ -r "$cg/cgroup.procs" ] && [ -w "$cg/varden/panel/cgroup.procs" ]; then
    while IFS= read -r pid; do
      [ -z "$pid" ] || printf '%s\n' "$pid" > "$cg/varden/panel/cgroup.procs" 2>/dev/null || true
    done < "$cg/cgroup.procs"
  fi
  if [ -r "$cg/varden/cgroup.procs" ] && [ -w "$cg/varden/panel/cgroup.procs" ]; then
    while IFS= read -r pid; do
      [ -z "$pid" ] || printf '%s\n' "$pid" > "$cg/varden/panel/cgroup.procs" 2>/dev/null || true
    done < "$cg/varden/cgroup.procs"
  fi

  printf '+cpu\n' > "$cg/cgroup.subtree_control" 2>/dev/null || return 0
  printf '+cpu\n' > "$cg/varden/cgroup.subtree_control" 2>/dev/null || return 0
  printf '+cpu\n' > "$cg/varden/servers/cgroup.subtree_control" 2>/dev/null || return 0

  chown "$PUID:$PGID" \
    "$cg/varden" \
    "$cg/varden/cgroup.procs" \
    "$cg/varden/panel" \
    "$cg/varden/panel/cgroup.procs" \
    "$cg/varden/servers" \
    "$cg/varden/servers/cgroup.procs" \
    "$cg/varden/servers/cgroup.subtree_control" 2>/dev/null || return 0

  export VARDEN_CGROUP_SERVERS="$cg/varden/servers"
}

if [ "$(id -u)" = "0" ]; then
  if [ "$(id -g varden)" != "$PGID" ]; then groupmod -o -g "$PGID" varden; fi
  if [ "$(id -u varden)" != "$PUID" ]; then usermod -o -u "$PUID" varden; fi
  mkdir -p /data
  if [ "$(stat -c %u /data)" != "$PUID" ] || [ "$(stat -c %g /data)" != "$PGID" ]; then
    echo "[varden] Setting owner of /data to $PUID:$PGID…"
    chown -R "$PUID:$PGID" /data
  fi
  setup_cgroup_v2 || true
  export HOME=/home/varden
  exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
fi
exec "$@"
