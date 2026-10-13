#!/bin/sh
set -eu

uid="${PUID:-1000}"
gid="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  if chown -R "${uid}:${gid}" /data 2>/dev/null; then
    exec su-exec "${uid}:${gid}" python3 /app/app.py
  fi
  echo "WARNING: could not switch to user ${uid}:${gid}; continuing as root" >&2
fi

exec python3 /app/app.py
