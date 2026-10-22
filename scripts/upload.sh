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

# Clean the shipped trips and upload the result alongside the raw data, so the
# cloud folder holds analysis-ready CSVs without anyone running anything.
# `OBD_EV_CLEAN_ARGS=--fill` carries each signal forward over the rows that did
# not sample it; left off, the upload stays strictly what the car reported.
CLEAN="${OBD_EV_CLEAN:-1}"
CLEAN_ARGS="${OBD_EV_CLEAN_ARGS:-}"
PYTHON="${OBD_EV_PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEST="$REMOTE/$DEVICE_ID"
ARCHIVE="$LOG_DIR/uploaded"
# Inside the log directory, not the archive, which is pruned per trip folder.
CLEANED="$LOG_DIR/${DEVICE_ID}_cleaned"
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
# systemd is not the only way the logger runs: on a bench kit it gets started
# by hand, and everything that protects the open part -- the flush request and
# the skip below -- hangs off this one answer. A file still being appended to
# is evidence systemd does not have. After a power cut the marker outlives the
# logger, but its file stops changing, so this stays false and the completed
# part uploads as usual.
if [ "$logger_running" = 0 ] && [ -f "$LOG_DIR/.current" ]; then
    current_part="$(cat "$LOG_DIR/.current" 2>/dev/null || true)"
    if [ -n "$current_part" ] && [ -f "$current_part" ] \
            && [ -n "$(find "$current_part" -newermt '-60 seconds' 2>/dev/null)" ]; then
        logger_running=1
    fi
fi

# Ask the logger to close the part it has open, so this run can ship it instead
# of leaving it for a run that may never come.
#
# Without this, the end of every drive rides on the kit outliving the ignition.
# The file stays open until trip_gap_seconds elapses, the loop below skips open
# files, and on a car that de-energises the OBD port with the key there is no
# "later" -- which is how the tail of a drive goes missing. Being online at all
# means, on a kit powered by the car, that the driver is home with the engine
# still running: the last moment that file can be saved.
#
# A file and not a signal: a logger built before this feature ignores the file,
# whereas an unhandled SIGUSR1 would kill it -- and right after a hand-run
# `git pull` this script is new while the running logger is still old.
#
# The logger decides whether the request is worth acting on (it declines while
# parked) and deletes the file either way. That deletion is the ack, so a
# decline costs no waiting.
request_flush() {
    local req="$LOG_DIR/.flush-request" waited=0
    [ "$logger_running" = 1 ] || return 0
    : > "$req" 2>/dev/null || return 0

    # A loop iteration is bounded by read_budget_seconds, and at idle_hz by the
    # row period, so this is answered in well under a second either way.
    while [ "$waited" -lt 150 ]; do
        [ -e "$req" ] || return 0
        sleep 0.1
        waited=$((waited + 1))
    done
    # Take it back rather than leaving it to fire at some unrelated moment.
    rm -f "$req" 2>/dev/null || true
    echo "logger did not answer the flush request in 15s; shipping what is closed" >&2
}
request_flush

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
    # Our own output, uploaded as a whole below rather than file by file.
    [ "$trip" = "${DEVICE_ID}_cleaned" ] && continue

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

# -- clean the archive and ship that too --------------------------------------
#
# After the loop above, not before it: a trip's parts are uploaded one at a
# time and moved to the archive as they go, so until that is done a trip can be
# split across the pending folder and the archive. Cleaning the archive alone
# means every trip it sees is whole, bar a part whose upload failed -- and that
# part arrives next run, when the trip is cleaned again and the output
# overwritten, since the file is named for the trip's start and boot id.
#
# Only when something new arrived, because the timer runs every minute and
# re-cleaning the whole archive each time would burn the card and the CPU for
# no change. The missing-directory case covers the first run after an update.
clean_and_upload() {
    [ "$CLEAN" = "1" ] || return 0
    [ -x "$SCRIPT_DIR/clean_trips.py" ] || return 0
    if [ "$uploaded_any" -ne 1 ] && [ -d "$CLEANED" ]; then
        return 0
    fi

    # shellcheck disable=SC2086  # CLEAN_ARGS is deliberately word-split
    if ! "$PYTHON" "$SCRIPT_DIR/clean_trips.py" "$ARCHIVE" -o "$CLEANED" \
            --quiet $CLEAN_ARGS; then
        echo "cleaning failed; the raw upload above still stands" >&2
        return 0
    fi

    # copy, not sync: a cleaned trip whose raw data has since been pruned from
    # the local archive must stay in the cloud. --checksum so the unchanged
    # trips, rewritten with a new mtime on every run, are not re-sent.
    if rclone --config "$RCLONE_CONF" copy "$CLEANED" "$DEST/${DEVICE_ID}_cleaned" \
            --checksum --retries 3 --low-level-retries 5; then
        echo "uploaded cleaned data for $DEVICE_ID"
    else
        echo "upload of cleaned data failed (will retry)" >&2
    fi
}

# Never let a cleaning problem fail the run: the raw upload is the job that
# matters, and it has already happened by this point.
clean_and_upload || true

[ "$uploaded_any" -eq 1 ] || exit 0

# Keep a local copy of recent trips for re-verification, bounded so the card
# cannot fill. Oldest trips go first.
ls -1dt "$ARCHIVE"/*/ 2>/dev/null | tail -n "+$((KEEP_TRIPS + 1))" | while read -r old; do
    rm -rf "$old"
    echo "pruned local archive $(basename "$old")"
done
