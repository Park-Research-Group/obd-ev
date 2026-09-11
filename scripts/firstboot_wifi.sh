#!/usr/bin/env bash
# First-boot WiFi provisioning for participants.
#
# At image time, the participant edits /boot/firmware/obd-ev-wifi.conf on the
# SD card with their home SSID and password. On first boot this script reads
# that file and creates a NetworkManager connection profile. After that, the
# Pi auto-connects whenever it's in range.
#
# Multiple networks may be supplied (one block per network).
#
# File format:
#   ssid=MyHomeWiFi
#   psk=mypassword
#   ---
#   ssid=Backup
#   psk=otherpassword
set -euo pipefail

CONF="/boot/firmware/obd-ev-wifi.conf"

if [ ! -f "$CONF" ]; then
    echo "no wifi config at $CONF, skipping"
    exit 0
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

# Split file into blocks separated by lines of dashes.
awk -v RS='---\n' '{print > "'"$tmp_dir"'/wifi_block_" NR ".tmp"}' "$CONF"

added=0
for block in "$tmp_dir"/wifi_block_*.tmp; do
    [ -s "$block" ] || continue
    ssid=$(grep -E '^[[:space:]]*ssid=' "$block" | head -1 | cut -d= -f2- | tr -d '\r' || true)
    psk=$(grep -E '^[[:space:]]*psk='  "$block" | head -1 | cut -d= -f2- | tr -d '\r' || true)
    [ -z "$ssid" ] && continue

    echo "adding network: $ssid"
    # Replace rather than duplicate: a re-run (the file was re-added, or a
    # previous run died before renaming it) must not stack profiles.
    nmcli connection delete "$ssid" >/dev/null 2>&1 || true
    if [ -z "$psk" ]; then
        ok=0
        nmcli -t connection add type wifi ifname wlan0 \
            con-name "$ssid" ssid "$ssid" \
            connection.autoconnect yes && ok=1
    else
        ok=0
        nmcli -t connection add type wifi ifname wlan0 \
            con-name "$ssid" ssid "$ssid" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" \
            connection.autoconnect yes && ok=1
    fi
    if [ "$ok" = 1 ]; then
        added=$((added + 1))
    else
        echo "could not add network: $ssid" >&2
    fi
done

if [ "$added" -eq 0 ]; then
    echo "no wifi networks found in $CONF"
    exit 1
fi

# Mark this run done so we don't re-process on every boot.
mv "$CONF" "${CONF}.applied"
echo "wifi provisioning done"
