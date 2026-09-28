#!/usr/bin/env bash
# Hand the status LED to the logger. Run as root from obd-ev.service's
# ExecStartPre, alongside bt_prepare.sh.
#
# Two things stop an unprivileged process driving a sysfs LED: the kernel
# trigger owns it (ACT is wired to mmc0, SD-card activity, on a Pi 4), and
# brightness is root-owned. Detach the trigger and hand over the file.
#
# Always exits 0: an LED must never be the reason the logger fails to start.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO_DIR" 2>/dev/null || echo root)"

# Honour led.path from config.yaml if it is set, else the Pi 4/5 default.
LED_PATH=/sys/class/leds/ACT
if [ -f "$REPO_DIR/config.yaml" ]; then
    from_cfg="$(sed -n 's/^[[:space:]]*path:[[:space:]]*//p' "$REPO_DIR/config.yaml" \
        | grep '^/sys/class/leds/' | head -1)"
    [ -n "$from_cfg" ] && LED_PATH="$from_cfg"
fi

[ -d "$LED_PATH" ] || { echo "led_prepare: no LED at $LED_PATH"; exit 0; }

# Detaching the trigger gives up SD-activity indication; that is the trade for
# an explicit OBD status light.
if [ -w "$LED_PATH/trigger" ] || [ "$(id -u)" = 0 ]; then
    echo none > "$LED_PATH/trigger" 2>/dev/null \
        && echo "led_prepare: detached trigger on $(basename "$LED_PATH")"
fi

if [ -e "$LED_PATH/brightness" ]; then
    chown "$OWNER" "$LED_PATH/brightness" 2>/dev/null \
        && echo "led_prepare: $LED_PATH/brightness now writable by $OWNER"
fi
exit 0
