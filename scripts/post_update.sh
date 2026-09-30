#!/usr/bin/env bash
# Apply the system-level parts of an update. Run as root.
#
# This is what a plain `git pull` cannot do: reinstall systemd units, refresh
# Python dependencies, and fix up Bluetooth. Called by self_update.sh, and
# safe to run by hand after pulling:
#
#   sudo ./scripts/post_update.sh && sudo systemctl restart obd-ev
#
# Deliberately does not run apt -- packages change rarely, and an apt run over
# a participant's home connection is not something to do unattended. Re-run
# scripts/setup_pi.sh if package changes are needed.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO_DIR")"
INSTALL_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
VENV_DIR="$REPO_DIR/.venv"

echo "post_update: repo $REPO_DIR (owner $OWNER)"

# -- python dependencies, only if they changed ------------------------------
REQ_STAMP=/var/lib/obd-ev/requirements.sha
mkdir -p /var/lib/obd-ev
current_req="$(sha256sum "$REPO_DIR/requirements.txt" 2>/dev/null | cut -d' ' -f1)"
if [ -n "$current_req" ] && [ "$current_req" != "$(cat "$REQ_STAMP" 2>/dev/null)" ]; then
    echo "post_update: requirements.txt changed, installing"
    if sudo -u "$OWNER" "$VENV_DIR/bin/python" -m pip install --quiet \
            -r "$REPO_DIR/requirements.txt"; then
        echo "$current_req" > "$REQ_STAMP"
    else
        echo "post_update: pip install failed" >&2
    fi
fi

# -- systemd units ----------------------------------------------------------
# Units live in /etc/systemd/system and are copies, so a pull alone never
# reaches them. This is the usual reason an update appears not to apply.
tmp_units="$(mktemp -d)"
cp "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer "$tmp_units"/ 2>/dev/null
sed -i \
    -e "s|@REPO_DIR@|$REPO_DIR|g" \
    -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@INSTALL_USER@|$OWNER|g" \
    "$tmp_units"/*.service
changed=0
for f in "$tmp_units"/*.service "$tmp_units"/*.timer; do
    [ -e "$f" ] || continue
    target="/etc/systemd/system/$(basename "$f")"
    if ! cmp -s "$f" "$target"; then
        cp "$f" "$target" && changed=1
        echo "post_update: updated $(basename "$f")"
    fi
done
rm -rf "$tmp_units"
[ "$changed" = 1 ] && systemctl daemon-reload

# Enable anything an update introduced. Copying a unit file is not enough --
# without this a new timer ships to every kit and never fires.
# obd-ev-provision is deliberately excluded: it raises the setup AP, and
# setup_kit.sh decides when a kit is ready for that.
for unit in obd-ev.service obd-ev-upload.timer obd-ev-timesync.timer; do
    [ -f "/etc/systemd/system/$unit" ] || continue
    state="$(systemctl is-enabled "$unit" 2>/dev/null)"
    case "$state" in
        enabled|enabled-runtime|static|indirect) ;;
        *) systemctl enable "$unit" >/dev/null 2>&1 \
               && echo "post_update: enabled $unit" ;;
    esac
done
systemctl start obd-ev-timesync.timer >/dev/null 2>&1 || true

# -- helper scripts installed outside the repo ------------------------------
if [ -f "$REPO_DIR/scripts/nm-dispatcher-upload" ]; then
    install -m 755 "$REPO_DIR/scripts/nm-dispatcher-upload" \
        /etc/NetworkManager/dispatcher.d/90-obd-ev-upload
fi
if [ -f "$REPO_DIR/scripts/nm-dispatcher-update" ]; then
    install -m 755 "$REPO_DIR/scripts/nm-dispatcher-update" \
        /etc/NetworkManager/dispatcher.d/91-obd-ev-update
fi
if [ -f "$REPO_DIR/systemd/obd-ev-portal-dns.conf" ]; then
    mkdir -p /etc/NetworkManager/dnsmasq-shared.d
    cp "$REPO_DIR/systemd/obd-ev-portal-dns.conf" \
        /etc/NetworkManager/dnsmasq-shared.d/obd-ev-portal.conf
fi

# -- bluetooth --------------------------------------------------------------
[ -x "$REPO_DIR/scripts/bt_prepare.sh" ] && "$REPO_DIR/scripts/bt_prepare.sh"

echo "post_update: done"
exit 0
