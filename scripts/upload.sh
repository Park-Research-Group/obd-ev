#!/usr/bin/env bash
# Upload completed trip files to the configured rclone remote.
#
# Every kit points at the same cloud folder, so the device id is the top level
# of the remote layout and the trip is the level below it:
#
#   <remote>/P003/20260906_142201_a3f9c1d2/drive_P003_20260906_142201.csv
#   <remote>/P003/20260906_142201_a3f9c1d2/drive_P003_20260906_143701.csv
#   <remote>/P003/20260906_142201_a3f9c1d2/signals_P003.csv
#
# Idempotent, and safe to run on a timer: uploaded files move to
# logs/uploaded/<trip>/ so they are never sent twice, and the file the logger
# currently has open is never touched.
set -euo pipefail

LOG_DIR="${OBD_EV_LOG_DIR:-$HOME/obd-ev/logs}"
REMOTE="${OBD_EV_REMOTE:-obd-ev:obd-ev-uploads}"
RCLONE_CONF="${OBD_EV_RCLONE_CONF:-$HOME/.config/rclone/rclone.conf}"
DEVICE_ID="${OBD_EV_DEVICE_ID:-$(hostname)}"
KEEP_TRIPS="${OBD_EV_KEEP_TRIPS:-64}"

DEST="$REMOTE/$DEVICE_ID"
ARCHIVE="$LOG_DIR/uploaded"
mkdir -p "$ARCHIVE"

# Away from home WiFi this exits quietly and the timer retries later.
if command -v nm-online >/dev/null 2>&1 && ! nm-online -q -t 10; then
    exit 0
fi

# The logger records the file it is writing in logs/.current. Only honour it
# while the service is actually running -- after an unclean shutdown the
# marker is stale and that file is complete and ought to be uploaded.
#
# Re-read before EVERY file, not once up front: a run can take a while, and
# the logger rotates in the middle of it (the trip-gap rotation two minutes
# after parking is exactly when the on-connect upload is running). Shipping
# the freshly opened part and moving it out from under the logger's open
# file handle would leave the rest of that part in the archive, never
# uploaded.
logger_running=0
systemctl is-active --quiet obd-ev 2>/dev/null && logger_running=1

is_open_file() {
    [ "$logger_running" = 1 ] || return 1
    [ -f "$LOG_DIR/.current" ] || return 1
    [ "$1" = "$(cat "$LOG_DIR/.current" 2>/dev/null || true)" ]
}

shopt -s nullglob
uploaded_any=0

for trip_dir in "$LOG_DIR"/*/; do
    trip="$(basename "$trip_dir")"
    [ "$trip" = "uploaded" ] && continue

    for file in "$trip_dir"*.csv; do
        is_open_file "$file" && continue
        name="$(basename "$file")"
        if rclone --config "$RCLONE_CONF" copyto "$file" "$DEST/$trip/$name" \
                --retries 3 --low-level-retries 5; then
            # The logger may have opened this very file while rclone ran.
            # Leave it; the next run re-uploads the complete version.
            if is_open_file "$file"; then
                echo "$trip/$name was opened by the logger mid-upload; will re-upload"
                continue
            fi
            mkdir -p "$ARCHIVE/$trip"
            mv "$file" "$ARCHIVE/$trip/$name"
            echo "uploaded $trip/$name"
            uploaded_any=1
        else
            echo "upload failed for $trip/$name (will retry)" >&2
        fi
    done

    # A trip folder with nothing left in it is fully shipped.
    rmdir "$trip_dir" 2>/dev/null || true
done

[ "$uploaded_any" -eq 1 ] || exit 0

# Keep a local copy of recent trips for re-verification, bounded so the card
# cannot fill. Oldest trips go first.
ls -1dt "$ARCHIVE"/*/ 2>/dev/null | tail -n "+$((KEEP_TRIPS + 1))" | while read -r old; do
    rm -rf "$old"
    echo "pruned local archive $(basename "$old")"
done
