"""
WardriverPy Configuration
Edit this file to match your hardware setup.
"""

import os
import platform
import subprocess

# ── Detect if running on Raspberry Pi ────────────────────────────────────────
def is_raspberry_pi() -> bool:
    try:
        with open("/proc/device-tree/model", "r") as f:
            return "Raspberry Pi" in f.read()
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo", "r") as f:
            return "BCM" in f.read() or "Raspberry" in f.read()
    except Exception:
        pass
    return False


def get_pi_model() -> str:
    try:
        with open("/proc/device-tree/model", "r") as f:
            return f.read().strip().rstrip("\x00")
    except Exception:
        return "Unknown Pi"


IS_PI = is_raspberry_pi()

# ── Network ───────────────────────────────────────────────────────────────────
HOST        = "0.0.0.0"
PORT        = 5000

# ── WiFi ──────────────────────────────────────────────────────────────────────
# Interface to scan on. None = auto-detect.
# Pi typically: wlan0 (built-in), wlan1 (USB dongle)
WIFI_INTERFACE     = None
WIFI_SCAN_INTERVAL = 5.0          # seconds between scans
# Disable WiFi power saving on Pi for better scanning
WIFI_DISABLE_POWER_SAVE = IS_PI

# ── Bluetooth ─────────────────────────────────────────────────────────────────
BT_ENABLED         = True
BLE_ENABLED        = True
BT_SCAN_INTERVAL   = 15.0         # seconds between BT scans
# HCI device (Pi built-in = hci0, USB dongle may be hci1)
BT_HCI_DEVICE      = "hci0"

# ── GPS ───────────────────────────────────────────────────────────────────────
# Options: auto | gpsd | serial | manual | none
GPS_MODE           = "auto"
# Pi UART GPS paths (in order of preference):
#   /dev/ttyAMA0  - Pi 3/4/5 primary UART (GPIO 14/15) — disable serial console first
#   /dev/serial0  - symlink to primary UART
#   /dev/ttyUSB0  - USB GPS dongle
#   /dev/ttyACM0  - USB GPS (CDC ACM)
GPS_SERIAL_PORT    = "/dev/serial0" if IS_PI else "/dev/ttyUSB0"
GPS_BAUD_RATE      = 9600

# ── SSL ───────────────────────────────────────────────────────────────────────
SSL_CERT = os.path.join(os.path.dirname(__file__), "cert.pem")
SSL_KEY  = os.path.join(os.path.dirname(__file__), "key.pem")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = "/var/log/wardriver.log" if IS_PI else None

# ── Export ────────────────────────────────────────────────────────────────────
EXPORTS_DIR = os.path.join(os.path.dirname(__file__), "exports")

# ── Display ───────────────────────────────────────────────────────────────────
# Set to None to auto-detect from framebuffer / display info
DISPLAY_WIDTH      = None
DISPLAY_HEIGHT     = None
DISPLAY_FULLSCREEN = True
# Use Chromium kiosk instead of pygame (good for HDMI / official Pi 7" touchscreen)
DISPLAY_KIOSK      = False        # set True in install if you prefer Chromium
DISPLAY_KIOSK_URL  = f"https://localhost:{PORT}"

# ── Persistent credentials ────────────────────────────────────────────────────
CREDS_FILE = os.path.join(os.path.dirname(__file__), ".wardriver_creds.json")

# ── Pi Hardware Helpers ───────────────────────────────────────────────────────
def disable_wifi_power_save(interface: str = "wlan0") -> bool:
    """Disable WiFi power management for continuous scanning."""
    try:
        subprocess.run(
            ["sudo", "iw", "dev", interface, "set", "power_save", "off"],
            capture_output=True, timeout=5
        )
        # Also try iwconfig method
        subprocess.run(
            ["sudo", "iwconfig", interface, "power", "off"],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        return False


def unblock_rfkill() -> bool:
    """Unblock WiFi and Bluetooth via rfkill (common on Pi)."""
    try:
        result = subprocess.run(["sudo", "rfkill", "unblock", "all"],
                                capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def reset_bluetooth(hci: str = "hci0") -> bool:
    """Reset HCI device — helps if BT scan fails."""
    try:
        subprocess.run(["sudo", "hciconfig", hci, "down"], capture_output=True, timeout=5)
        subprocess.run(["sudo", "hciconfig", hci, "up"],   capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def enable_uart_gps() -> bool:
    """
    Check if UART is available for GPS.
    On Pi, you may need to:
      - Run: sudo raspi-config → Interface Options → Serial Port
        → disable login shell over serial, enable serial port hardware
      - Or edit /boot/config.txt: enable_uart=1
    """
    import glob
    paths = ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"]
    for p in paths:
        if os.path.exists(p):
            return True
    return False


def get_local_ip() -> str:
    """
    Return the best non-loopback IPv4 address.
    Tries several route probes so it works with no internet / hotspot / VPN.
    Falls back to scanning all active interfaces.
    """
    import socket
    # Try route probes — first one that works wins
    for host in ("8.8.8.8", "1.1.1.1", "192.168.0.1", "10.0.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect((host, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Fallback: scan all interfaces via /proc/net/fib_trie or getaddrinfo
    try:
        hostname = socket.gethostname()
        candidates = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for _, _, _, _, sockaddr in candidates:
            ip = sockaddr[0]
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def get_all_local_ips() -> list:
    """Return all non-loopback IPv4 addresses (for multi-homed hosts)."""
    import socket
    ips = []
    try:
        hostname = socket.gethostname()
        for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = sockaddr[0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        ip = get_local_ip()
        if ip != "127.0.0.1":
            ips.append(ip)
    return ips
