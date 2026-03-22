#!/usr/bin/env bash
# ============================================================
# WardriverPy — Raspberry Pi Installer
# Tested on: Raspberry Pi OS (Bullseye / Bookworm), 32/64-bit
# Run as: bash install_pi.sh
# ============================================================
set -e
WARDRIVER_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_USER="${SUDO_USER:-pi}"
SERVICE_NAME="wardriver"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[→]${NC} $1"; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║       WardriverPy — Raspberry Pi Installer           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Root check ───────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Run with sudo: sudo bash install_pi.sh"
    exit 1
fi

# ── Detect Pi model ───────────────────────────────────────────────────────────
PI_MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0' || echo "Unknown")
log "Detected: $PI_MODEL"

# ── System packages ───────────────────────────────────────────────────────────
info "Updating package lists..."
apt-get update -qq

info "Installing system packages..."
apt-get install -y -qq \
    python3 python3-pip python3-dev \
    bluetooth bluez bluez-tools \
    libglib2.0-dev \
    libdbus-1-dev \
    python3-dbus \
    libbluetooth-dev \
    rfkill \
    iw wireless-tools \
    network-manager \
    gpsd gpsd-clients \
    libssl-dev \
    git curl wget \
    libatlas-base-dev \
    2>/dev/null || warn "Some packages may have failed — continuing"

log "System packages installed"

# ── Python packages ───────────────────────────────────────────────────────────
info "Installing Python packages..."
python3 -m pip install --break-system-packages -q \
    flask \
    flask-socketio \
    eventlet \
    pyserial \
    requests \
    bleak \
    "qrcode[pil]" \
    cryptography \
    Pillow \
    pygame

log "Python packages installed"

# ── Bluetooth setup ───────────────────────────────────────────────────────────
info "Configuring Bluetooth..."

# Enable and start Bluetooth service
systemctl enable bluetooth
systemctl start bluetooth

# Add service user to bluetooth group
usermod -aG bluetooth "$SERVICE_USER" 2>/dev/null || true

# Unblock Bluetooth via rfkill
rfkill unblock bluetooth 2>/dev/null || true
rfkill unblock wifi      2>/dev/null || true

# Bring up hci0
hciconfig hci0 up 2>/dev/null || warn "hci0 not available (no BT hardware?)"

log "Bluetooth configured"

# ── WiFi power save off ───────────────────────────────────────────────────────
info "Disabling WiFi power management..."
WLAN=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -1)
if [[ -n "$WLAN" ]]; then
    iw dev "$WLAN" set power_save off 2>/dev/null || true
    iwconfig "$WLAN" power off 2>/dev/null || true
    log "WiFi power save disabled on $WLAN"

    # Persist across reboots
    WIFI_PM_CONF="/etc/NetworkManager/conf.d/wifi-pm-off.conf"
    cat > "$WIFI_PM_CONF" <<'EOF'
[connection]
wifi.powersave = 2
EOF
    log "WiFi power save persistently disabled"
else
    warn "No WiFi interface found"
fi

# ── UART GPS setup ────────────────────────────────────────────────────────────
info "Checking UART for GPS..."
if [[ -e /dev/serial0 ]] || [[ -e /dev/ttyAMA0 ]]; then
    log "UART available for GPS"
else
    warn "UART not detected. To enable:"
    warn "  Run: sudo raspi-config → Interface Options → Serial Port"
    warn "  Disable login shell, enable hardware serial port"
    warn "  OR add 'enable_uart=1' to /boot/config.txt"
fi

# ── SSL certificate ───────────────────────────────────────────────────────────
info "Generating SSL certificate..."
cd "$WARDRIVER_DIR"
python3 gen_cert.py
log "SSL certificate generated"

# ── systemd service ───────────────────────────────────────────────────────────
info "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Get Python path
PYTHON_BIN=$(which python3)

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=WardriverPy Wardriving Tool
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=bluetooth
WorkingDirectory=${WARDRIVER_DIR}
ExecStartPre=/bin/bash -c 'rfkill unblock all; hciconfig hci0 up; iw dev wlan0 set power_save off 2>/dev/null || true'
ExecStart=${PYTHON_BIN} app.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wardriver
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
log "systemd service installed and enabled"

# ── Permissions ───────────────────────────────────────────────────────────────
info "Setting permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$WARDRIVER_DIR"

# Allow non-root WiFi scanning
setcap cap_net_raw,cap_net_admin=eip "$(which python3)" 2>/dev/null || \
    warn "Could not set Python capabilities — WiFi scan may need sudo"

# Allow non-root access to hcitool
setcap cap_net_raw+eip "$(which hcitool)" 2>/dev/null || true

log "Permissions set"

# ── Chromium kiosk (optional) ─────────────────────────────────────────────────
echo ""
read -r -p "$(echo -e "${CYAN}[?]${NC} Enable Chromium kiosk mode on the Pi screen? (y/N) ")" KIOSK_ANSWER
if [[ "$KIOSK_ANSWER" =~ ^[Yy]$ ]]; then
    apt-get install -y -qq chromium-browser 2>/dev/null || \
    apt-get install -y -qq chromium 2>/dev/null || \
        warn "Chromium not found — skipping kiosk"

    # Update config.py to enable kiosk
    sed -i 's/^DISPLAY_KIOSK.*=.*/DISPLAY_KIOSK      = True/' "$WARDRIVER_DIR/config.py"

    # Create autostart entry for display manager
    AUTOSTART_DIR="/home/$SERVICE_USER/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/wardriver-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WardriverPy Kiosk
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars --ignore-certificate-errors https://localhost:5000
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$AUTOSTART_DIR/wardriver-kiosk.desktop"
    log "Chromium kiosk configured"
else
    log "Using pygame display (framebuffer/SDL)"
fi

# ── SDL/framebuffer display permissions ───────────────────────────────────────
usermod -aG video,render "$SERVICE_USER" 2>/dev/null || true
# Allow wardriver service to access framebuffer
if [[ -e /dev/fb0 ]]; then
    chmod 660 /dev/fb0 2>/dev/null || true
    chown root:video /dev/fb0 2>/dev/null || true
fi

# ── Summary ───────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${BOLD}${GREEN}Installation complete!${NC}"
echo ""
echo -e "${CYAN}Start now:${NC}    sudo systemctl start wardriver"
echo -e "${CYAN}Auto-start:${NC}   Already enabled (starts on every boot)"
echo -e "${CYAN}View logs:${NC}    journalctl -u wardriver -f"
echo -e "${CYAN}Stop:${NC}         sudo systemctl stop wardriver"
echo ""
echo -e "${CYAN}Dashboard:${NC}    https://${LOCAL_IP}:5000"
echo -e "${CYAN}Phone GPS:${NC}    https://${LOCAL_IP}:5000/phone"
echo ""
echo -e "${YELLOW}NOTE:${NC} Log out and back in for Bluetooth group membership to take effect"
echo -e "${YELLOW}NOTE:${NC} If using UART GPS, run: sudo raspi-config → Interface Options → Serial Port"
