#!/usr/bin/env bash
# Pull updates and apply them, when the kit has internet.
#
# Triggered by a NetworkManager dispatcher hook on connect, so a kit in the
# field picks up fixes the next time the participant parks at home. Runs as
# root; the git operations drop to the repo owner.
#
# ON by default, following the `deploy` branch. Understand what that means:
# whoever can push to that branch gets root on every kit running this,
# including ones in participants' vehicles. Treat `deploy` as a branch you
# promote to deliberately, never as a working branch. Settings in
# /etc/default/obd-ev:
#
#   OBD_EV_AUTO_UPDATE=0                 # opt this kit out
#   OBD_EV_UPDATE_BRANCH=deploy          # default: deploy
#   OBD_EV_UPDATE_MIN_INTERVAL=3600      # seconds between attempts
#   OBD_EV_UPDATE_MAX_DEFER=21600        # 6h: longest an update waits for a
#                                        # drive to end before taking the
#                                        # drive's part and updating anyway
#
#   scripts/self_update.sh --force       # ignore the interval and the trip check
#
# obd-ev-update.timer retries hourly. Without it the only trigger is the
# dispatcher hook on connect, and a kit powered from the OBD port is online
# only while the car is on -- which is exactly when an update defers. It
# would then wait for a connect that only ever happens mid-drive, and never
# update at all.
set -uo pipefail

[ -f /etc/default/obd-ev ] && . /etc/default/obd-ev

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO_DIR")"
# OBD_EV_STATE_DIR exists so the tests can run this script for real.
STATE_DIR="${OBD_EV_STATE_DIR:-/var/lib/obd-ev}"
STAMP="$STATE_DIR/last-update-check"
DEFERRED="$STATE_DIR/update-deferred-since"
LOG_DIR="${OBD_EV_LOG_DIR:-$REPO_DIR/logs}"
MIN_INTERVAL="${OBD_EV_UPDATE_MIN_INTERVAL:-3600}"
MAX_DEFER="${OBD_EV_UPDATE_MAX_DEFER:-21600}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "self_update: $*"; }
git_as() { sudo -u "$OWNER" git -C "$REPO_DIR" "$@"; }

# -- is a drive in progress? ------------------------------------------------
# obd_connected is the 4th column of every row, and .current names the part
# being written. Both outlive the logger: the marker is removed on a clean
# close, and these kits die with the ignition instead.
#
# So ask systemd first. A logger that is not running cannot be interrupted,
# and must never be able to block its own repair -- a crash-looping service
# leaves a marker behind saying the vehicle was connected, and trusting that
# alone would lock the kit out of the update that fixes it.
trip_in_progress() {
    systemctl is-active --quiet obd-ev || return 1
    [ -f "$LOG_DIR/.current" ] || return 1
    local part
    part="$(cat "$LOG_DIR/.current" 2>/dev/null)"
    [ -f "$part" ] || return 1
    [ "$(tail -1 "$part" | cut -d, -f4)" = "1" ]
}

# How long the kit has been behind and unable to catch up. The stamp is
# cleared whenever an update is taken or there is nothing left to take, so
# this is the age of the backlog, not of the kit -- and deliberately not of
# one commit: a kit that keeps being handed newer commits while it defers
# must still stop deferring eventually.
deferred_seconds() {
    local now since
    now="$(date +%s)"
    if [ ! -f "$DEFERRED" ]; then
        echo "$now" > "$DEFERRED"
        echo 0
        return
    fi
    since="$(cat "$DEFERRED" 2>/dev/null)"
    case "$since" in
        ''|*[!0-9]*) echo "$now" > "$DEFERRED"; echo 0 ;;
        *) echo $(( now - since )) ;;
    esac
}

# Ask the logger to close the part it has open, so a drive interrupted by an
# update survives as a complete file that the uploader will ship, rather than
# as whatever had been flushed when the process was killed.
close_open_part() {
    [ -d "$LOG_DIR" ] || return 0
    : > "$LOG_DIR/.flush-request" 2>/dev/null || return 0
    local waited=0
    while [ -f "$LOG_DIR/.flush-request" ] && [ "$waited" -lt 20 ]; do
        sleep 1
        waited=$(( waited + 1 ))
    done
    if [ -f "$LOG_DIR/.flush-request" ]; then
        # An older logger ignores the file; leaving it would make the next
        # one rotate for no reason.
        rm -f "$LOG_DIR/.flush-request"
        log "logger did not acknowledge the flush; updating anyway"
    fi
}

if [ "${OBD_EV_AUTO_UPDATE:-1}" != "1" ] && [ "$FORCE" != 1 ]; then
    exit 0
fi

# -- rate limit -------------------------------------------------------------
# A dispatcher hook fires on every connect, and wifi flaps. The stamp is
# written only after a fetch actually reached the remote (below): a connect
# with no internet behind it -- the setup AP coming up, a captive hotspot --
# must not use up the hour and skip the real home connection minutes later.
if [ "$FORCE" != 1 ] && [ -f "$STAMP" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$MIN_INTERVAL" ] && exit 0
fi
mkdir -p "$STATE_DIR"

# -- is there anything to take? ---------------------------------------------
# Asked before the drive check, and before anything is written to the repo:
# fetching costs a drive nothing, and there is no point deferring an update
# that does not exist.
branch="${OBD_EV_UPDATE_BRANCH:-deploy}"
current="$(git_as rev-parse --abbrev-ref HEAD)"
before="$(git_as rev-parse HEAD)"

if ! git_as fetch --quiet origin "$branch" 2>/dev/null; then
    log "fetch failed (no internet?)"
    exit 0
fi
touch "$STAMP"
remote="$(git_as rev-parse "origin/$branch" 2>/dev/null)"
[ -z "$remote" ] && { log "no such branch origin/$branch"; exit 0; }
[ "$before" = "$remote" ] && [ "$current" = "$branch" ] && { rm -f "$DEFERRED"; exit 0; }

# Refuse before touching anything: a kit with local edits is one someone was
# debugging, and silently discarding that would be worse than not updating.
if [ -n "$(git_as status --porcelain --untracked-files=no)" ]; then
    log "local modifications present -- refusing to update"
    exit 1
fi

# -- don't interrupt a drive, but don't wait forever either -----------------
# Restarting the logger mid-drive splits the trip; the rows already flushed
# are kept either way. That is worth avoiding, not worth never updating for,
# so the wait is bounded -- and the part is closed properly before the
# restart, which costs the drive a file boundary and nothing else.
if [ "$FORCE" != 1 ] && trip_in_progress; then
    waited="$(deferred_seconds)"
    if [ "$waited" -lt "$MAX_DEFER" ]; then
        log "vehicle is connected -- deferring ${remote:0:8} (waited ${waited}s of ${MAX_DEFER}s)"
        exit 0
    fi
    log "deferred ${waited}s -- closing the open part and updating mid-drive"
    close_open_part
fi
rm -f "$DEFERRED"

if [ "$current" != "$branch" ]; then
    # A kit built from main needs to move onto the release branch once, so
    # that later updates are ordinary fast-forwards of a tracking branch.
    log "switching from $current to $branch"
    if ! git_as checkout -B "$branch" --track "origin/$branch" --quiet; then
        log "could not check out $branch"
        exit 1
    fi
else
    log "updating $branch: ${before:0:8} -> ${remote:0:8}"
    if ! git_as merge --ff-only "origin/$branch" --quiet; then
        log "not a fast-forward -- refusing to update"
        exit 1
    fi
fi

# -- apply, verify, roll back if it broke -----------------------------------
"$REPO_DIR/scripts/post_update.sh" || log "post_update reported a problem"

systemctl restart obd-ev
sleep 12

if "$REPO_DIR/scripts/preflight.py" --quick >/tmp/obd-ev-update-preflight 2>&1; then
    log "updated to ${remote:0:8}, preflight clean"
    exit 0
fi

log "preflight FAILED after update -- rolling back to ${before:0:8}"
sed -n 's/^  FAIL/    FAIL/p' /tmp/obd-ev-update-preflight
git_as checkout -B "$current" --quiet "$before" 2>/dev/null \
    || git_as reset --hard "$before" --quiet
"$REPO_DIR/scripts/post_update.sh" || true
systemctl restart obd-ev
log "rolled back"
exit 1
