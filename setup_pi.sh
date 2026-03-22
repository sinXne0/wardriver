#!/usr/bin/env bash
# ============================================================
# WardriverPy — Pi Quick Setup (no sudo password prompts)
# Run as: bash setup_pi.sh
# ============================================================
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
info() { echo -e "${CYAN}[→]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║       WardriverPy — Pi Quick Setup                   ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Python packages (no sudo needed) ──────────────────────────────────────
info "Installing Python packages..."
pip3 install --break-system-packages -q \
    flask \
    flask-socketio \
    eventlet \
    pyserial \
    requests \
    bleak \
    "qrcode[pil]" \
    cryptography \
    Pillow \
    pygame 2>&1 | grep -v "^$" | grep -v "already satisfied" || true
ok "Python packages done"

# ── 2. System packages (needs sudo) ──────────────────────────────────────────
if command -v sudo &>/dev/null; then
    info "Installing system packages (sudo)..."
    sudo apt-get update -qq 2>/dev/null
    sudo apt-get install -y -qq \
        bluetooth bluez bluez-tools \
        rfkill iw wireless-tools \
        gpsd gpsd-clients \
        libglib2.0-dev 2>/dev/null || warn "Some system packages failed — continuing"
    ok "System packages done"

    # ── 3. Bluetooth setup ───────────────────────────────────────────────────
    info "Setting up Bluetooth..."
    sudo systemctl enable bluetooth 2>/dev/null || true
    sudo systemctl start  bluetooth 2>/dev/null || true
    sudo rfkill unblock all         2>/dev/null || true
    sudo hciconfig hci0 up          2>/dev/null || warn "hci0 not found — no BT adapter?"
    sudo usermod -aG bluetooth "$USER" 2>/dev/null || true
    ok "Bluetooth ready"

    # ── 4. WiFi power save off ───────────────────────────────────────────────
    info "Disabling WiFi power save..."
    WLAN=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -1)
    if [[ -n "$WLAN" ]]; then
        sudo iw dev "$WLAN" set power_save off 2>/dev/null || true
        ok "WiFi power save off ($WLAN)"
    else
        warn "No WiFi interface found"
    fi

    # ── 5. Framebuffer permissions ───────────────────────────────────────────
    sudo usermod -aG video,render "$USER" 2>/dev/null || true
else
    warn "No sudo — skipping system packages and BT/WiFi setup"
    warn "Run manually: sudo apt-get install bluetooth bluez rfkill iw"
fi

# ── 6. SSL cert ───────────────────────────────────────────────────────────────
info "Generating SSL certificate..."
python3 gen_cert.py 2>/dev/null && ok "SSL cert generated" || warn "SSL cert generation failed"

# ── 7. Create missing dirs ────────────────────────────────────────────────────
mkdir -p exports logs
ok "Directories ready"

# ── 8. Quick import test ──────────────────────────────────────────────────────
info "Testing imports..."
python3 - <<'PYEOF'
import sys, importlib
ok = []
fail = []
mods = [
    ('eventlet',      'eventlet'),
    ('flask',         'flask'),
    ('flask_socketio','flask_socketio'),
    ('pyserial',      'serial'),
    ('bleak',         'bleak'),
    ('cryptography',  'cryptography'),
    ('Pillow',        'PIL'),
    ('qrcode',        'qrcode'),
    ('pygame',        'pygame'),
    ('requests',      'requests'),
]
for name, imp in mods:
    try:
        importlib.import_module(imp)
        ok.append(name)
    except ImportError as e:
        fail.append(f'{name}: {e}')

print(f"\033[32m[✓]\033[0m OK: {', '.join(ok)}")
if fail:
    print(f"\033[31m[✗]\033[0m MISSING:")
    for f in fail: print(f"    - {f}")
    sys.exit(1)
else:
    print("\033[32m[✓]\033[0m All imports OK")
PYEOF

# ── 9. Test app loads ─────────────────────────────────────────────────────────
info "Testing app startup..."
timeout 6 python3 -c "
import eventlet; eventlet.monkey_patch()
import sys
sys.path.insert(0, '.')
try:
    import config as cfg
    from modules.wifi_scanner import WifiScanner, get_wifi_interfaces
    from modules.gps_handler import GPSHandler, GPSFix
    from modules.bt_scanner import BTScanner
    from modules.wigle_export import export_to_csv_string
    from modules.tpager_bridge import TPagerBridge
    from modules.pi_display import create_display
    print('\033[32m[✓]\033[0m App modules load OK')
    print(f'    IS_PI={cfg.IS_PI}')
    print(f'    WiFi interfaces: {get_wifi_interfaces()}')
except Exception as e:
    print(f'\033[31m[✗]\033[0m Import error: {e}')
    import traceback; traceback.print_exc()
    sys.exit(1)
" 2>&1 || { err "App failed to load — check errors above"; exit 1; }

# ── 10. systemd service (optional) ───────────────────────────────────────────
if command -v sudo &>/dev/null && command -v systemctl &>/dev/null; then
    SERVICE="/etc/systemd/system/wardriver.service"
    cat > /tmp/wardriver.service <<EOF
[Unit]
Description=WardriverPy Wardriving Tool
After=network-online.target bluetooth.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=bluetooth
WorkingDirectory=$DIR
ExecStartPre=/bin/bash -c 'rfkill unblock all 2>/dev/null; hciconfig hci0 up 2>/dev/null; iw dev \$(iw dev | awk "/Interface/{print \$2}" | head -1) set power_save off 2>/dev/null || true'
ExecStart=/usr/bin/python3 $DIR/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    sudo cp /tmp/wardriver.service "$SERVICE"
    sudo systemctl daemon-reload
    sudo systemctl enable wardriver
    ok "systemd service installed (wardriver.service)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo -e "  Start now:   ${CYAN}python3 app.py${NC}"
echo -e "  Or via svc:  ${CYAN}sudo systemctl start wardriver${NC}"
echo -e "  Dashboard:   ${CYAN}https://${LOCAL_IP}:5000${NC}"
echo -e "  Phone:       ${CYAN}https://${LOCAL_IP}:5000/phone${NC}"
echo ""
echo -e "${YELLOW}NOTE:${NC} Log out and back in for Bluetooth group to take effect"
