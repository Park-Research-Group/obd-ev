#!/usr/bin/env bash
# Pull updates and apply them, when the kit has internet.
#
# Triggered by a NetworkManager dispatcher hook on connect, so a kit in the
# field picks up fixes the next time the participant parks at home. Runs as
# root; the git operations drop to the repo owner.
#
# OFF unless OBD_EV_AUTO_UPDATE=1 is set in /etc/default/obd-ev. Understand
# what you are enabling: whoever can push to the tracked branch gets root on
# every kit running this, including ones in participants' vehicles. Point it at
# a branch you promote to deliberately, not at your working branch.
#
#   OBD_EV_AUTO_UPDATE=1
#   OBD_EV_UPDATE_BRANCH=deploy          # default: the current branch
#   OBD_EV_UPDATE_MIN_INTERVAL=3600      # seconds between attempts
#
#   scripts/self_update.sh --force       # ignore the interval and the trip check
set -uo pipefail

[ -f /etc/default/obd-ev ] && . /etc/default/obd-ev

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO_DIR")"
STAMP=/var/lib/obd-ev/last-update-check
MIN_INTERVAL="${OBD_EV_UPDATE_MIN_INTERVAL:-3600}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "self_update: $*"; }
git_as() { sudo -u "$OWNER" git -C "$REPO_DIR" "$@"; }

if [ "${OBD_EV_AUTO_UPDATE:-0}" != "1" ] && [ "$FORCE" != 1 ]; then
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
mkdir -p /var/lib/obd-ev

# -- never interrupt a drive ------------------------------------------------
# obd_connected is the 4th column of every row. Updating mid-trip would restart
# the logger and lose the drive.
if [ "$FORCE" != 1 ] && [ -f "${OBD_EV_LOG_DIR:-$REPO_DIR/logs}/.current" ]; then
    current="$(cat "${OBD_EV_LOG_DIR:-$REPO_DIR/logs}/.current" 2>/dev/null)"
    if [ -f "$current" ] && [ "$(tail -1 "$current" | cut -d, -f4)" = "1" ]; then
        log "vehicle is connected -- deferring until the trip ends"
        exit 0
    fi
fi

# -- is there anything to take? ---------------------------------------------
branch="${OBD_EV_UPDATE_BRANCH:-$(git_as rev-parse --abbrev-ref HEAD)}"
current="$(git_as rev-parse --abbrev-ref HEAD)"
before="$(git_as rev-parse HEAD)"

if ! git_as fetch --quiet origin "$branch" 2>/dev/null; then
    log "fetch failed (no internet?)"
    exit 0
fi
touch "$STAMP"
remote="$(git_as rev-parse "origin/$branch" 2>/dev/null)"
[ -z "$remote" ] && { log "no such branch origin/$branch"; exit 0; }
[ "$before" = "$remote" ] && [ "$current" = "$branch" ] && exit 0

# Refuse before touching anything: a kit with local edits is one someone was
# debugging, and silently discarding that would be worse than not updating.
if [ -n "$(git_as status --porcelain --untracked-files=no)" ]; then
    log "local modifications present -- refusing to update"
    exit 1
fi

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
