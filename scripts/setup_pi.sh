#!/usr/bin/env bash
# System provisioning for a fresh Raspberry Pi OS Lite install: packages,
# interfaces, virtualenv, services. Idempotent, safe to re-run.
#
# You normally do not run this directly. `scripts/setup_kit.sh` calls it as
# one step of building a kit, and also handles the kit id, vehicle, cloud
# upload and verification. Run this alone only to repair those parts of an
# existing card.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_USER="${SUDO_USER:-$USER}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
VENV_DIR="$REPO_DIR/.venv"

echo "[1/8] apt packages"
sudo apt-get update
sudo apt-get install -y \
    python3-pip python3-venv python3-yaml \
    bluetooth bluez \
    gpsd gpsd-clients python3-gps \
    i2c-tools \
    rclone \
    network-manager \
    dnsmasq-base

echo "[2/8] enable I2C and GPS UART"
sudo raspi-config nonint do_i2c 0
# do_serial was split in newer raspi-config; fall back for older images.
if sudo raspi-config nonint do_serial_hw 0 2>/dev/null; then
    sudo raspi-config nonint do_serial_cons 1
else
    sudo raspi-config nonint do_serial 2
fi

# Which device the GPS lands on depends on the board, and getting it wrong
# looks exactly like a dead GPS.
#
#   Pi 5   the header UART is a full PL011 at /dev/ttyAMA0, but the console
#          lives on the separate debug connector so enable_uart=1 evicts
#          nothing -- it must be switched on explicitly.
#   Pi 4   and earlier: the PL011 is taken by Bluetooth, and GPIO14/15 gets
#          the mini UART at /dev/ttyS0. We keep Bluetooth (the OBD adapter is
#          BLE), so the GPS uses the mini UART.
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
PI_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"

case "$PI_MODEL" in
    *"Raspberry Pi 5"*)
        GPS_DEVICE=/dev/ttyAMA0
        if [ -f "$CONFIG_TXT" ] && ! grep -qE '^[[:space:]]*dtparam=uart0=on' "$CONFIG_TXT"; then
            echo "  Pi 5: adding dtparam=uart0=on to $CONFIG_TXT (needs a reboot)"
            printf '\n[all]\ndtparam=uart0=on\n' | sudo tee -a "$CONFIG_TXT" >/dev/null
            UART_REBOOT_NEEDED=1
        fi
        ;;
    *)
        # /dev/serial0 is the kernel's own alias for the header UART and is
        # correct on Pi 4 and earlier (it points at ttyS0, the mini UART --
        # the PL011 there is bound to Bluetooth and enumerates as ttyAMA1).
        if [ -e /dev/serial0 ]; then
            GPS_DEVICE="/dev/$(basename "$(readlink -f /dev/serial0)")"
        else
            GPS_DEVICE=/dev/ttyS0
        fi
        ;;
esac
echo "  board: $PI_MODEL"
echo "  GPS device: $GPS_DEVICE"

echo "[3/8] python deps"
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"

echo "[4/8] gpsd default device"
sudo sed -i "s|^DEVICES=.*|DEVICES=\"$GPS_DEVICE\"|" /etc/default/gpsd
sudo sed -i 's|^GPSD_OPTIONS=.*|GPSD_OPTIONS="-n"|' /etc/default/gpsd
sudo systemctl enable --now gpsd

echo "[4b/8] Bluetooth: LE-only controller mode"
# The OBD adapter is GATT/BLE and nothing else on this kit uses Bluetooth.
# Left in dual mode, BlueZ's transport-agnostic Device1.Connect() can pick
# BR/EDR for an address it has classic information about, and the connect dies
# with "br-connection-profile-unavailable" even though the LE scan found the
# adapter perfectly well.
BT_CONF=/etc/bluetooth/main.conf
if [ -f "$BT_CONF" ] && ! grep -qE '^[[:space:]]*ControllerMode[[:space:]]*=[[:space:]]*le' "$BT_CONF"; then
    if grep -qE '^[[:space:]]*#?[[:space:]]*ControllerMode' "$BT_CONF"; then
        sudo sed -i 's|^[[:space:]]*#\?[[:space:]]*ControllerMode.*|ControllerMode = le|' "$BT_CONF"
    else
        sudo sed -i '0,/^\[General\]/s//[General]\nControllerMode = le/' "$BT_CONF"
    fi
    echo "  set ControllerMode = le"
    sudo systemctl restart bluetooth || true
fi
grep -hE '^[[:space:]]*ControllerMode' "$BT_CONF" 2>/dev/null | sed 's/^/  /'

echo "[4c/8] unblock Bluetooth"
# A soft rfkill block survives reboots and makes the OBD adapter simply never
# appear -- bleak reports "No powered Bluetooth adapters found" while the
# bluetooth service itself looks perfectly healthy.
sudo rfkill unblock bluetooth 2>/dev/null || true
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true

echo "[5/8] state directory"
sudo mkdir -p /var/lib/obd-ev
sudo chmod 755 /var/lib/obd-ev

echo "[6/8] captive portal DNS + upload-on-connect hook"
# Wildcard DNS applies only while a `shared` connection is up, i.e. the setup
# access point.
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo cp "$REPO_DIR/systemd/obd-ev-portal-dns.conf" \
        /etc/NetworkManager/dnsmasq-shared.d/obd-ev-portal.conf
# Fire an upload the instant the Pi joins home WiFi rather than waiting for
# the timer; a participant may only be in range for a minute after parking.
sudo install -m 755 "$REPO_DIR/scripts/nm-dispatcher-upload" \
        /etc/NetworkManager/dispatcher.d/90-obd-ev-upload
# Self-update on connect: on by default, following the `deploy` branch.
# OBD_EV_AUTO_UPDATE=0 in /etc/default/obd-ev opts a kit out -- see
# scripts/self_update.sh for what enabling it means.
sudo install -m 755 "$REPO_DIR/scripts/nm-dispatcher-update" \
        /etc/NetworkManager/dispatcher.d/91-obd-ev-update

echo "[7/8] install systemd units"
tmp_units="$(mktemp -d)"
cp "$REPO_DIR"/systemd/*.service "$tmp_units"/
cp "$REPO_DIR"/systemd/*.timer "$tmp_units"/
sed -i \
    -e "s|@REPO_DIR@|$REPO_DIR|g" \
    -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@INSTALL_USER@|$INSTALL_USER|g" \
    "$tmp_units"/*.service
sudo cp "$tmp_units"/*.service /etc/systemd/system/
sudo cp "$tmp_units"/*.timer   /etc/systemd/system/
rm -rf "$tmp_units"
sudo systemctl daemon-reload
sudo systemctl enable obd-ev-firstboot.service
sudo systemctl enable obd-ev.service
# obd-ev-provision is deliberately NOT enabled here. It puts wlan0 into AP
# mode, which would disconnect whoever is building the card. setup_kit.sh
# arms it once the rest of the kit is verified.
sudo systemctl enable obd-ev-upload.timer

echo "[8/8] default env file"
# Default env file (overridable per-device).
if [ ! -f /etc/default/obd-ev ]; then
    # The setup access point needs a WPA2 password of its own. It is printed
    # on the kit's label; a random one per device is fine and preferable.
    # Not `tr ... | head -c 10`: head closes the pipe, tr dies of SIGPIPE, and
    # `set -o pipefail` above turns that into a fatal error.
    ap_password="$(LC_ALL=C dd if=/dev/urandom bs=256 count=1 2>/dev/null \
        | LC_ALL=C tr -dc 'a-z2-9' | cut -c1-10)"
    sudo tee /etc/default/obd-ev >/dev/null <<ENVEOF
OBD_EV_LOG_DIR=$REPO_DIR/logs
OBD_EV_REMOTE=obd-ev:obd-ev-uploads
OBD_EV_RCLONE_CONF=$INSTALL_HOME/.config/rclone/rclone.conf
# Remote updates from the deploy branch. Set to 0 to opt this kit out.
OBD_EV_AUTO_UPDATE=1
OBD_EV_UPDATE_BRANCH=deploy
# Password for the "OBD-EV-Setup-<device id>" network the participant joins.
# PRINT THIS ON THE KIT LABEL.
OBD_EV_AP_PASSWORD=$ap_password
ENVEOF
    sudo chown root:"$(id -gn "$INSTALL_USER")" /etc/default/obd-ev
    sudo chmod 640 /etc/default/obd-ev
    echo
    echo "  Setup-AP password for this device: $ap_password"
    echo "  (also in /etc/default/obd-ev; put it on the kit label)"
fi

echo
if [ -n "${UART_REBOOT_NEEDED:-}" ]; then
    echo "NOTE: dtparam=uart0=on was just added. $GPS_DEVICE (the GPS) will"
    echo "      not exist until you reboot."
elif [ ! -e "$GPS_DEVICE" ]; then
    echo "NOTE: $GPS_DEVICE does not exist yet -- reboot, then check with"
    echo "      ls -l /dev/serial*  and  cgps -s"
fi
echo "System provisioning done."
if [ -z "${OBD_EV_FROM_SETUP_KIT:-}" ]; then
    echo "To build a complete kit (vehicle, cloud upload, checks), use:"
    echo "  ./scripts/setup_kit.sh"
fi
