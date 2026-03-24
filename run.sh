#!/usr/bin/env bash
# WardriverPy launcher — works on desktop Linux and Raspberry Pi
set -e
cd "$(dirname "$0")"

VENV_DIR="$(pwd)/.venv"

# ── Detect real user (works whether called with sudo or not) ──────────────────
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

# ── Detect Raspberry Pi ───────────────────────────────────────────────────────
IS_PI=false
if grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

# ── Set up virtualenv as the real user (not root) ─────────────────────────────
if [[ ! -f "$VENV_DIR/bin/pip" ]]; then
    echo "[→] Creating virtual environment as $REAL_USER..."
    sudo -u "$REAL_USER" python3 -m venv "$VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

# Install/update deps as real user
echo "[→] Installing dependencies..."
sudo -u "$REAL_USER" "$PIP" install -q -r requirements.txt

# ── Pi-specific setup ─────────────────────────────────────────────────────────
if $IS_PI; then
    echo "[Pi] Unblocking radios..."
    sudo rfkill unblock all 2>/dev/null || true

    echo "[Pi] Bringing up Bluetooth..."
    sudo hciconfig hci0 up 2>/dev/null || true

    echo "[Pi] Disabling WiFi power save..."
    WLAN=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -1)
    [ -n "$WLAN" ] && sudo iw dev "$WLAN" set power_save off 2>/dev/null || true
fi

exec "$PYTHON" app.py
