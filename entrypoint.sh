#!/bin/sh
# Mount the Drive folder (when an rclone config with a `gdrive` remote is present) and run the player.
# RCLONE_CONFIG points at a file on a writable mount so rclone can persist refreshed tokens.
set -eu
if [ -n "${RCLONE_CONFIG:-}" ] && [ -f "$RCLONE_CONFIG" ]; then
  rclone mount gdrive: /data --allow-other --umask 022 \
    --vfs-cache-mode full --vfs-cache-max-size "${GDRIVE_CACHE_SIZE:-6G}" --vfs-cache-max-age 168h \
    --cache-dir /cache/rclone --vfs-write-back 10s --dir-cache-time 5m --poll-interval 1m --attr-timeout 1m \
    --log-level NOTICE &
  for i in $(seq 1 60); do [ -d /data/videos ] && break; sleep 1; done
  [ -d /data/videos ] || { echo "Drive mount did not come up"; exit 1; }
  trap 'fusermount3 -uz /data 2>/dev/null || true' EXIT
fi
exec acroscope serve --host 0.0.0.0 --port 8070 "$@"
