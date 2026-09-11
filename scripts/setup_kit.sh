#!/usr/bin/env bash
# Build one participant kit, from a freshly flashed SD card to ready-to-ship.
#
#   ssh <user>@raspberrypi.local
#   git clone https://github.com/ethanmathias/obd-ev && cd obd-ev
#   ./scripts/setup_kit.sh
#
# Run this once per card. There is no master image to clone, which is
# deliberate: cloning an image copies one cloud credential onto every kit, and
# Box and Drive invalidate a token when another device refreshes it, so all but
# the first kit silently stop uploading weeks later. Building each card fresh
# gives it its own credential and removes the problem entirely.
#
# Safe to re-run. Every step is idempotent, so if something fails you can fix
# it and run the script again rather than starting the card over.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=/etc/default/obd-ev
VENV_DIR="$REPO_DIR/.venv"
STEPS=6

bold=$(tput bold 2>/dev/null || true)
dim=$(tput dim 2>/dev/null || true)
green=$(tput setaf 2 2>/dev/null || true)
yellow=$(tput setaf 3 2>/dev/null || true)
reset=$(tput sgr0 2>/dev/null || true)

step() { echo; echo "${bold}[$1/$STEPS] $2${reset}"; }
note() { echo "  ${dim}$*${reset}"; }
warn() { echo "  ${yellow}$*${reset}"; }

ask() {                       # ask <prompt> <default> -> echoes the answer
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        read -r -p "  $prompt [$default]: " reply </dev/tty
        echo "${reply:-$default}"
    else
        while :; do
            read -r -p "  $prompt: " reply </dev/tty
            [ -n "$reply" ] && { echo "$reply"; return; }
        done
    fi
}

random_password() {
    # dd exits 0, tr consumes all of its output so never sees SIGPIPE, and cut
    # exits 0 -- so this survives `set -o pipefail`. The obvious
    # `tr -dc ... | head -c 10` does not: head closes the pipe early, tr dies
    # of SIGPIPE, and pipefail turns that into a fatal error mid-setup.
    LC_ALL=C dd if=/dev/urandom bs=256 count=1 2>/dev/null \
        | LC_ALL=C tr -dc 'a-z2-9' | cut -c1-10
}

env_get() {                   # current value of a key, if the file exists
    # Must never fail: this is called from `default=$(env_get ...)`, and under
    # `set -e` a failing command substitution in an assignment kills the
    # script. The file is root-owned, so try an unprivileged read, then sudo,
    # then give up quietly.
    [ -f "$ENV_FILE" ] || return 0
    { cat "$ENV_FILE" 2>/dev/null || sudo -n cat "$ENV_FILE" 2>/dev/null || true; } \
        | sed -n "s/^$1=//p" | tail -1
}

env_set() {                   # env_set KEY VALUE: update in place, or append
    # The file is edited key by key rather than rewritten, so a re-run keeps
    # whatever else is in it: the vehicle chosen last time, a pinned BLE
    # address, OBD_EV_AUTO_UPDATE=0 set by hand to opt a kit out.
    local key="$1" value="$2"
    if sudo grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sudo sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
}

# --------------------------------------------------------------------------

echo "${bold}obd-ev kit setup${reset}"
echo "Builds one SD card from scratch. Expect 10-15 minutes, most of it"
echo "unattended package installation."
echo
echo "You will be asked for everything up front, then left alone until the"
echo "cloud login near the end, which needs a browser."

if [ "$(id -u)" -eq 0 ]; then
    echo
    echo "Run this as your normal user, not with sudo -- it needs your home" >&2
    echo "directory for the rclone credential. It will ask for sudo itself." >&2
    exit 1
fi
sudo -v

# Hold off the participant setup portal for the whole build. It puts wlan0
# into AP mode, which would disconnect this very SSH session. The unit refuses
# to start while this file exists, so even a reboot part-way through is safe.
sudo mkdir -p /var/lib/obd-ev
sudo touch /var/lib/obd-ev/setup-in-progress

# -- 1. everything we need to ask ------------------------------------------

step 1 "Kit details"

DEVICE_ID="$(ask 'Kit / participant id (e.g. P003)' "$(env_get OBD_EV_DEVICE_ID)")"
DEVICE_ID="${DEVICE_ID// /-}"

default_ap="$(env_get OBD_EV_AP_PASSWORD)"
[ -z "$default_ap" ] && default_ap="$(random_password)"
AP_PASSWORD="$(ask 'Password for this kit'\''s setup WiFi (goes on the label)' "$default_ap")"
while [ "${#AP_PASSWORD}" -lt 8 ]; do
    warn "Must be at least 8 characters."
    AP_PASSWORD="$(ask 'Setup WiFi password' "$default_ap")"
done

note "Writing $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
    sudo tee "$ENV_FILE" >/dev/null <<ENVEOF
# obd-ev kit configuration. Written by scripts/setup_kit.sh; edited in place
# on re-runs, so hand-added settings survive.
#
# Remote updates are ON: the kit pulls the `deploy` branch whenever it has
# internet and is not mid-drive (scripts/self_update.sh). Whoever can push to
# that branch gets root on this kit, so promote to it deliberately. Set
# OBD_EV_AUTO_UPDATE=0 to opt this kit out.
ENVEOF
fi
env_set OBD_EV_DEVICE_ID "$DEVICE_ID"
env_set OBD_EV_LOG_DIR "$REPO_DIR/logs"
env_set OBD_EV_REMOTE "obd-ev:obd-ev-uploads"
env_set OBD_EV_RCLONE_CONF "$HOME/.config/rclone/rclone.conf"
env_set OBD_EV_AUTO_UPDATE "1"
env_set OBD_EV_UPDATE_BRANCH "deploy"
# Password for the "OBD-EV-Setup-<id>" network the participant joins.
env_set OBD_EV_AP_PASSWORD "$AP_PASSWORD"
# Readable by the user who runs the tooling, not world-readable. 600 would
# lock out preflight.py, authorize_kit.sh and this script's own re-runs; the
# only secret in here is the setup-AP password, which is printed on the label
# anyway. systemd reads EnvironmentFile as root regardless.
sudo chown root:"$(id -gn)" "$ENV_FILE"
sudo chmod 640 "$ENV_FILE"

# -- 2. the vehicle ---------------------------------------------------------

step 2 "Vehicle"
note "Pick the car this kit is going into. Press 's' to search the full"
note "catalogue, or 0 if the car is unknown or unsupported -- the kit then"
note "logs generic Mode 01, which works on any OBD-II vehicle."
echo
python3 "$REPO_DIR/scripts/select_vehicle.py" </dev/tty || {
    warn "No vehicle selected; this kit will use generic Mode 01."
}

# -- 3. system provisioning (the long part) ---------------------------------

step 3 "System packages, interfaces and services"
note "This is the slow step. Nothing to do until it finishes."
OBD_EV_FROM_SETUP_KIT=1 "$REPO_DIR/scripts/setup_pi.sh"

# -- 4. cloud upload --------------------------------------------------------

step 4 "Cloud upload"
RCLONE_CONF="$HOME/.config/rclone/rclone.conf"
if rclone --config "$RCLONE_CONF" config show obd-ev >/dev/null 2>&1; then
    note "An 'obd-ev' remote already exists on this card."
    if [ "$(ask 'Reconfigure it? [y/N]' 'n')" = "y" ]; then
        rclone --config "$RCLONE_CONF" config delete obd-ev || true
    fi
fi

if ! rclone --config "$RCLONE_CONF" config show obd-ev >/dev/null 2>&1; then
    echo
    echo "  ${bold}rclone will now ask you to set up the upload destination.${reset}"
    echo "  Name the remote exactly:  ${bold}obd-ev${reset}"
    echo "  Storage:                  box   (or drive)"
    echo
    echo "  The Pi has no browser, so answer ${bold}n${reset} to \"Use auto config?\""
    echo "  and follow the instructions it prints on your laptop."
    echo
    read -r -p "  Press Enter to start rclone config. " </dev/tty
    rclone --config "$RCLONE_CONF" config </dev/tty
fi

if rclone --config "$RCLONE_CONF" config show obd-ev >/dev/null 2>&1; then
    note "Testing a real upload..."
    probe="obd-ev:obd-ev-uploads/_setup_${DEVICE_ID}_$$"
    if echo "obd-ev setup check" | rclone --config "$RCLONE_CONF" rcat "$probe"; then
        rclone --config "$RCLONE_CONF" delete "$probe" >/dev/null 2>&1 || true
        echo "  ${green}Upload works.${reset}"
        # Record the credential fingerprint so two cards can be compared if a
        # kit is ever cloned by accident.
        sudo mkdir -p /var/lib/obd-ev
        fp="$(rclone --config "$RCLONE_CONF" config dump 2>/dev/null | python3 -c '
import hashlib, json, sys
try: r = json.load(sys.stdin)
except Exception: print("none"); raise SystemExit
t = (r.get("obd-ev") or {}).get("token", "")
print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else "none")' || true)"
        printf '{"device_id":"%s","token_fingerprint":"%s","authorized_at":"%s"}\n' \
            "$DEVICE_ID" "$fp" "$(date -Is)" | sudo tee /var/lib/obd-ev/upload-authorized.json >/dev/null
        note "credential fingerprint $fp"
    else
        warn "Test upload failed. Fix it and re-run this script."
    fi
else
    warn "No 'obd-ev' remote configured; this kit cannot upload."
fi

# -- 5. verify --------------------------------------------------------------

step 5 "Verification"
# The participant must get the setup portal, so this card must not look
# already-provisioned from testing.
sudo rm -f /var/lib/obd-ev/provisioned.json
# Arm the portal for first boot. It still cannot start yet -- the
# setup-in-progress lock is released at the very end of this script.
sudo systemctl enable obd-ev-provision.service >/dev/null 2>&1 || true
set +e
"$VENV_DIR/bin/python" "$REPO_DIR/scripts/preflight.py"
preflight_status=$?
set -e

# -- 6. hand-off ------------------------------------------------------------

step 6 "Label this kit"
# env_get succeeds with empty output when a key is absent, so `|| echo` would
# never fire; pick the fallback explicitly.
KIT_VEHICLE="$(env_get OBD_EV_VEHICLE)"
KIT_VEHICLE_YEAR="$(env_get OBD_EV_VEHICLE_YEAR)"
cat <<SUMMARY

  ${bold}Kit id${reset}              $DEVICE_ID
  ${bold}Setup WiFi${reset}          OBD-EV-Setup-$DEVICE_ID
  ${bold}Setup password${reset}      $AP_PASSWORD
  ${bold}Vehicle${reset}             ${KIT_VEHICLE:-generic Mode 01} ${KIT_VEHICLE_YEAR:-}

  Write the setup WiFi name and password on the kit. The participant needs
  them once, to tell the device their home network.

SUMMARY

if [ "$preflight_status" -eq 0 ]; then
    # Release the lock last: from here on, a reboot raises the participant
    # setup access point, which is what should happen on the kit's first boot.
    sudo rm -f /var/lib/obd-ev/setup-in-progress
    echo "  ${green}${bold}This kit is ready to ship.${reset}"
    echo
    echo "  ${bold}Note:${reset} the setup WiFi will now come up on the next boot,"
    echo "  taking over wlan0. If you need more SSH time on this Pi first:"
    echo "    sudo touch /var/lib/obd-ev/setup-in-progress   # hold it off"
    echo "    sudo rm /var/lib/obd-ev/setup-in-progress      # re-arm before shipping"
    echo
    echo "  Bench test before it goes out: plug into a car, then"
    echo "    journalctl -u obd-ev -f"
else
    # Leave the lock in place: an unfinished kit must not seize wlan0 and lock
    # you out of the card you are still working on.
    echo "  ${yellow}${bold}Not ready.${reset} Fix the FAIL items above and re-run this script."
    echo "  The setup WiFi stays held off until this script completes cleanly."
fi
echo
exit "$preflight_status"
