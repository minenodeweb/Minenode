#!/bin/sh
# Reset the password of a MineNode panel user (e.g. a forgotten one).
#
# Lists the panel users, asks which one to reset and prints a new random 16-character password.
# Every session of that user is logged out; the panel picks up the change without a restart.
#
#   ./reset_password.sh              # on the Docker host: container "minenode"
#   ./reset_password.sh mycontainer  # another container name
#   reset_password.sh                # inside the container (docker exec -it minenode reset_password.sh)
#
# A username can be given after the container name to skip the question:
#   ./reset_password.sh minenode admin
set -eu

if [ -f /app/app/reset_password.py ]; then
  # Inside the MineNode container: run as the owner of /data so users.json keeps its owner.
  cd /app
  if [ "$(id -u)" = "0" ] && command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid="$(stat -c %u /data)" --regid="$(stat -c %g /data)" --clear-groups \
      python -m app.reset_password "$@"
  fi
  exec python -m app.reset_password "$@"
fi

CONTAINER="${1:-minenode}"
[ $# -gt 0 ] && shift
if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Run this on the machine that runs the MineNode container," >&2
  echo "or inside it: docker exec -it $CONTAINER reset_password.sh" >&2
  exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "No container named \"$CONTAINER\". Usage: $0 [container-name] [username]" >&2
  exit 1
fi
exec docker exec -it "$CONTAINER" reset_password.sh "$@"
