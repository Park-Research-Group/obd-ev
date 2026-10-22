#!/usr/bin/env bash
# Put the touchscreen dashboard on this kit. Test kits only (P002).
#
#   ./scripts/install_dashboard.sh            # install and start
#   ./scripts/install_dashboard.sh --remove   # take it off again
#
# Nothing in a participant build runs this, and nothing it installs is touched
# by setup_pi.sh or an update: the unit template lives in systemd/optional/,
# which neither of them globs. Removing it leaves the kit exactly as it was.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
INSTALL_USER="${SUDO_USER:-$USER}"
UNIT=obd-ev-dashboard.service
TEMPLATE="$REPO_DIR/systemd/optional/$UNIT"

if [ "${1:-}" = "--remove" ]; then
    sudo systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
    sudo rm -f "/etc/systemd/system/$UNIT"
    sudo systemctl daemon-reload
    echo "dashboard removed. The logger was never touched."
    exit 0
fi

echo "[1/5] GL userspace"
# Pi OS Lite ships the kernel side of KMS -- /dev/dri/card1 and its HDMI
# connectors are there on a bare image -- and none of the Mesa userspace
# behind it. SDL then reaches the display and dies at `EGL not initialized`,
# which reads like a broken panel rather than four missing packages.
#
# kmscube is the test that settles it: DRM -> GBM -> EGL -> GLES with no SDL
# in the picture. `sudo kmscube` on a working kit spins a cube on the screen.
GL_PACKAGES="libgl1-mesa-dri libegl1 libgles2 libgbm1"
missing=""
for pkg in $GL_PACKAGES; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
done
if [ -n "$missing" ]; then
    echo "     installing:$missing"
    # shellcheck disable=SC2086 -- deliberate word splitting
    sudo apt-get install -y $missing
else
    echo "      already present"
fi

echo "[2/5] pygame"
# Not in requirements.txt: that file is installed on every participant kit,
# and this is the one package only a test kit needs. The venv is built with
# --system-site-packages, so the apt build is visible from it -- and the apt
# build is the one linked against the Pi's own SDL/KMS stack.
if ! "$VENV_DIR/bin/python" -c "import pygame" >/dev/null 2>&1; then
    sudo apt-get install -y python3-pygame || true
fi
if ! "$VENV_DIR/bin/python" -c "import pygame" >/dev/null 2>&1; then
    echo "      apt build not usable from the venv, falling back to pip"
    "$VENV_DIR/bin/python" -m pip install --quiet pygame
fi
"$VENV_DIR/bin/python" -c "import pygame; print('      pygame', pygame.version.ver)"

echo "[3/5] display permissions"
# SDL on KMS/DRM needs the framebuffer and the touch panel; none of these are
# in the service user's groups on a Lite image.
for group in video render input; do
    getent group "$group" >/dev/null || continue
    sudo usermod -aG "$group" "$INSTALL_USER"
done

echo "[4/5] install $UNIT"
tmp="$(mktemp)"
sed -e "s|@REPO_DIR@|$REPO_DIR|g" \
    -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@INSTALL_USER@|$INSTALL_USER|g" \
    "$TEMPLATE" > "$tmp"
sudo cp "$tmp" "/etc/systemd/system/$UNIT"
rm -f "$tmp"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"

echo "[5/5] check"
sleep 3
if systemctl is-active --quiet "$UNIT"; then
    echo "      running. The screen should be showing the open trip."
else
    echo "      NOT running. sudo journalctl -u $UNIT -n 30"
    echo "      Is it the display or is it us? This proves the GL stack"
    echo "      without SDL -- a spinning cube means the fault is ours:"
    echo "        sudo apt-get install -y kmscube && sudo kmscube"
    echo "      Over SSH the same view works as text:"
    echo "        $REPO_DIR/scripts/dashboard.py --backend text"
fi
echo
echo "Remove with: $0 --remove"
