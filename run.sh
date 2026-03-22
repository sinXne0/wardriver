#!/usr/bin/env bash
# WardriverPy launcher — works on desktop Linux and Raspberry Pi
set -e
cd "$(dirname "$0")"

# ── Detect Raspberry Pi ───────────────────────────────────────────────────────
IS_PI=false
if grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

# ── Check Python deps ─────────────────────────────────────────────────────────
python3 -c "import flask, flask_socketio, eventlet, serial, bleak, cryptography" 2>/dev/null || {
    echo "[!] Missing dependencies. Installing..."
    python3 -m pip install flask flask-socketio eventlet pyserial requests bleak \
        "qrcode[pil]" cryptography Pillow --break-system-packages -q
}

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

exec python3 app.py
