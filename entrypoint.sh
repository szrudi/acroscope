#!/bin/sh
# Mount the Drive folder (when GDRIVE_TOKEN is set) and run the player. rclone takes its config from the
# RCLONE_CONFIG_GDRIVE_* environment, so no config file holds the token; refreshed access tokens stay in memory.
set -eu
if [ -n "${GDRIVE_TOKEN:-}" ]; then
  export RCLONE_CONFIG_GDRIVE_TYPE=drive RCLONE_CONFIG_GDRIVE_SCOPE=drive \
         RCLONE_CONFIG_GDRIVE_TOKEN="$GDRIVE_TOKEN" RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID="${GDRIVE_ROOT_FOLDER_ID:?}"
  rclone mount gdrive: /data --allow-other --umask 022 \
    --vfs-cache-mode full --vfs-cache-max-size "${GDRIVE_CACHE_SIZE:-6G}" --vfs-cache-max-age 168h \
    --cache-dir /cache/rclone --vfs-write-back 10s --dir-cache-time 5m --poll-interval 1m --attr-timeout 1m \
    --log-level NOTICE &
  for i in $(seq 1 60); do [ -d /data/videos ] && break; sleep 1; done
  [ -d /data/videos ] || { echo "Drive mount did not come up"; exit 1; }
  trap 'fusermount3 -uz /data 2>/dev/null || true' EXIT
fi
exec acroscope serve --host 0.0.0.0 --port 8070 "$@"
