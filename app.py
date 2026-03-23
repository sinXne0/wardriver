#!/usr/bin/env python3
"""
WARDAEMON - Wardriving Tool with WiGLE Integration + T-Pager Support
Flask + SocketIO web UI for real-time WiFi scanning and GPS tracking.

Supports:
  - Native Linux WiFi scanning (nmcli/iwlist)
  - LILYGO T-Pager ESP32 over USB serial
  - GPS: gpsd, serial NMEA, manual coordinates, phone browser, TCP NMEA
  - WiGLE CSV/KML/JSON export and API upload
"""

import eventlet
import eventlet.wsgi
eventlet.monkey_patch()

import os
import json
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO, emit

from modules.wifi_scanner import WifiScanner, get_wifi_interfaces
from modules.gps_handler import GPSHandler, GPSFix
from modules.tpager_bridge import TPagerBridge, list_serial_ports, find_tpager_port
from modules.phone_gps import TCPNMEAServer
from modules.bt_scanner import BTScanner
from modules.pi_display import create_display
from modules.wigle_export import (
    export_to_csv, export_to_csv_string, export_to_kml, WiGLEUploader
)
import config as cfg

# ── Logging ───────────────────────────────────────────────────────────────────
log_handlers = [logging.StreamHandler()]
if cfg.LOG_FILE:
    try:
        log_handlers.append(logging.FileHandler(cfg.LOG_FILE))
    except Exception:
        pass

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger(__name__)

if cfg.IS_PI:
    logger.info(f"Running on {cfg.get_pi_model()}")
    cfg.unblock_rfkill()
    if cfg.WIFI_DISABLE_POWER_SAVE:
        ifaces = get_wifi_interfaces()
        for iface in ifaces:
            cfg.disable_wifi_power_save(iface)
            logger.info(f"WiFi power save disabled on {iface}")

# ── App Setup ─────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
EXPORTS_DIR = cfg.EXPORTS_DIR
os.makedirs(EXPORTS_DIR, exist_ok=True)

# Track current IP so we can detect changes
_current_ip = cfg.get_local_ip()

app = Flask(__name__)
app.secret_key = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Core Components ───────────────────────────────────────────────────────────
scanner    = WifiScanner(interval=cfg.WIFI_SCAN_INTERVAL, interface=cfg.WIFI_INTERFACE)
gps        = GPSHandler()
tpager     = TPagerBridge()
tcp_nmea   = TCPNMEAServer(port=10110)
bt_scanner = BTScanner(interval=cfg.BT_SCAN_INTERVAL,
                        scan_classic=True,
                        scan_ble=cfg.BLE_ENABLED)

# ── Session State ─────────────────────────────────────────────────────────────
state = {
    "scanning":           False,
    "scan_source":        "none",     # linux | tpager
    "gps_mode":           "none",
    "gps_started":        False,
    "tpager_connected":   False,
    "wigle_user":         "",         # display name / username
    "wigle_credential":   "",         # base64 encoded credential for API
    "session_start":      None,
    "interface":          None,
    "tcp_nmea_active":    False,
    "phone_gps_active":   False,
    "bt_scanning":        False,
}

# ── Persistent WiGLE credentials ─────────────────────────────────────────────
def _load_creds():
    try:
        if os.path.exists(cfg.CREDS_FILE):
            with open(cfg.CREDS_FILE) as f:
                creds = json.load(f)
            # Support old api_name/token format by re-encoding
            if creds.get("wigle_credential"):
                state["wigle_credential"] = creds["wigle_credential"]
                state["wigle_user"]       = creds.get("wigle_user", "")
            elif creds.get("wigle_api_name") and creds.get("wigle_api_token"):
                import base64 as _b64
                state["wigle_credential"] = _b64.b64encode(
                    f"{creds['wigle_api_name']}:{creds['wigle_api_token']}".encode()
                ).decode()
                state["wigle_user"] = creds["wigle_api_name"]
            if state["wigle_user"]:
                logger.info(f"WiGLE credentials loaded for {state['wigle_user']}")
    except Exception as e:
        logger.warning(f"Could not load credentials: {e}")

def _save_creds():
    try:
        with open(cfg.CREDS_FILE, "w") as f:
            json.dump({
                "wigle_user":       state["wigle_user"],
                "wigle_credential": state["wigle_credential"],
            }, f)
        os.chmod(cfg.CREDS_FILE, 0o600)
    except Exception as e:
        logger.warning(f"Could not save credentials: {e}")

_load_creds()

# ── Pi Display ────────────────────────────────────────────────────────────────
_display = create_display(cfg) if cfg.IS_PI else None

# ── Network store (unified for both Linux scanner & T-Pager) ─────────────────
_networks: dict[str, dict] = {}
_net_lock = threading.Lock()


def _store_network(net: dict, is_new: bool):
    """Thread-safe network upsert."""
    bssid = net.get("bssid", "")
    if not bssid:
        return
    with _net_lock:
        if bssid not in _networks:
            _networks[bssid] = net
            _networks[bssid]["is_new"] = True
        else:
            _networks[bssid]["rssi"]      = net.get("rssi", _networks[bssid]["rssi"])
            _networks[bssid]["last_seen"] = net.get("last_seen", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            _networks[bssid]["is_new"]    = False


def _get_all_networks() -> list[dict]:
    with _net_lock:
        return list(_networks.values())


def _get_stats() -> dict:
    nets = _get_all_networks()
    total      = len(nets)
    open_      = sum(1 for n in nets if n.get("auth_mode","") == "[ESS]")
    wpa2       = sum(1 for n in nets if "WPA2" in n.get("auth_mode",""))
    wpa3       = sum(1 for n in nets if "WPA3" in n.get("auth_mode",""))
    wpa        = sum(1 for n in nets if "WPA-PSK" in n.get("auth_mode","") and "WPA2" not in n.get("auth_mode",""))
    wep        = sum(1 for n in nets if "WEP"  in n.get("auth_mode",""))
    ble        = sum(1 for n in nets if n.get("type") == "BLE")
    bt_classic = sum(1 for n in nets if n.get("type") == "BT")
    tpager_src = sum(1 for n in nets if n.get("source") == "tpager")
    return {
        "total": total, "open": open_, "wpa2": wpa2,
        "wpa3": wpa3, "wpa": wpa, "wep": wep,
        "ble": ble, "bt_classic": bt_classic,
        "tpager": tpager_src,
    }


def _push_update(new_bssids: list = None):
    """Emit current network state to all WebSocket clients."""
    fix = gps.get_fix()
    socketio.emit("networks_update", {
        "networks": _get_all_networks(),
        "stats":    _get_stats(),
        "gps":      fix.to_dict(),
        "new_bssids": new_bssids or [],
    })


# ── Linux WiFi Scanner Callback ───────────────────────────────────────────────
def _refresh_display():
    """Push current data to the Pi display if running."""
    if _display:
        fix = gps.get_fix()
        _display.update(_get_all_networks(), _get_stats(), fix.to_dict(), state)


def on_linux_scan(networks: list, new_bssids: list):
    fix = gps.get_fix()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for net in networks:
        net.setdefault("lat", fix.lat)
        net.setdefault("lon", fix.lon)
        net.setdefault("alt", fix.alt)
        net.setdefault("accuracy", fix.accuracy)
        net.setdefault("source", "linux")
        net["last_seen"] = now
        _store_network(net, net["bssid"] in new_bssids)
    _push_update(new_bssids)
    _refresh_display()


# ── T-Pager Callbacks ──────────────────────────────────────────────────────────
def on_tpager_network(net: dict):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    net["last_seen"] = now
    _store_network(net, net.get("new", False))
    fix = gps.get_fix()
    socketio.emit("networks_update", {
        "networks": _get_all_networks(),
        "stats":    _get_stats(),
        "gps":      fix.to_dict(),
        "new_bssids": [net["bssid"]] if net.get("new") else [],
    })


def on_tpager_gps(data: dict):
    """T-Pager has onboard GPS — update our GPS handler with its fix."""
    if data.get("has"):
        gps.set_manual(data.get("lat", 0.0), data.get("lon", 0.0))
    socketio.emit("gps_update", data)


def on_tpager_status(data: dict):
    state["tpager_connected"] = data.get("status") == "connected"
    socketio.emit("tpager_status", data)


def on_tpager_stat(data: dict):
    socketio.emit("tpager_stat", data)


def on_gps_fix(fix):
    socketio.emit("gps_update", fix.to_dict())


def on_tcp_nmea_fix(fix: GPSFix):
    """Called when TCP NMEA server receives a GPS fix from phone app."""
    gps.set_manual(fix.lat, fix.lon)
    socketio.emit("gps_update", fix.to_dict())
    socketio.emit("phone_gps_status", {
        "source": fix.source,
        "lat": fix.lat,
        "lon": fix.lon,
        "fix_type": fix.fix_type,
    })


def on_bt_update(devices: list, new_macs: list):
    """Called by BT scanner — inject into shared network store."""
    fix = gps.get_fix()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for dev in devices:
        dev.setdefault("lat", fix.lat)
        dev.setdefault("lon", fix.lon)
        dev.setdefault("alt", fix.alt)
        dev.setdefault("accuracy", fix.accuracy)
        dev["last_seen"] = now
        _store_network(dev, dev["bssid"] in new_macs)
    socketio.emit("bt_update", {
        "devices":   devices,
        "new_macs":  new_macs,
        "stats":     bt_scanner.get_stats(),
        "all_stats": _get_stats(),
    })
    _refresh_display()


scanner.on_update(on_linux_scan)
bt_scanner.on_update(on_bt_update)
gps.on_fix(on_gps_fix)
tpager.on_network(on_tpager_network)
tpager.on_gps(on_tpager_gps)
tpager.on_status(on_tpager_status)
tpager.on_stat(on_tpager_stat)
tcp_nmea.on_fix(on_tcp_nmea_fix)

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    interfaces   = get_wifi_interfaces()
    serial_ports = list_serial_ports()
    auto_port    = find_tpager_port()
    return render_template("index.html",
                           interfaces=interfaces,
                           serial_ports=serial_ports,
                           auto_port=auto_port,
                           state=state)


# ── Linux Scan Control ──────────────────────────────────────────────────────
@app.route("/api/scan/start", methods=["POST"])
def start_scan():
    if state["scanning"] and state["scan_source"] == "linux":
        return jsonify({"success": False, "error": "Already scanning"})
    data      = request.json or {}
    interface = data.get("interface") or None
    interval  = float(data.get("interval", 5.0))
    scanner.interval = interval
    scanner.start(interface=interface)
    state["scanning"]     = True
    state["scan_source"]  = "linux"
    state["session_start"] = datetime.utcnow().isoformat()
    state["interface"]    = interface
    return jsonify({"success": True, "interface": interface, "interval": interval})


@app.route("/api/scan/stop", methods=["POST"])
def stop_scan():
    scanner.stop()
    state["scanning"]    = False
    state["scan_source"] = "none"
    return jsonify({"success": True, "total": _get_stats()["total"]})


@app.route("/api/scan/clear", methods=["POST"])
def clear_scan():
    with _net_lock:
        _networks.clear()
    scanner.clear()
    _push_update()
    return jsonify({"success": True})


# ── T-Pager Control ─────────────────────────────────────────────────────────
@app.route("/api/tpager/connect", methods=["POST"])
def tpager_connect():
    data = request.json or {}
    port = data.get("port") or find_tpager_port()
    baud = int(data.get("baudrate", 115200))
    tpager.baudrate = baud
    if tpager.start(port=port):
        state["scanning"]       = True
        state["scan_source"]    = "tpager"
        state["tpager_connected"] = True
        return jsonify({"success": True, "port": tpager.port})
    return jsonify({"success": False, "error": f"Could not connect to T-Pager on {port}"})


@app.route("/api/tpager/disconnect", methods=["POST"])
def tpager_disconnect():
    tpager.stop()
    state["tpager_connected"] = False
    if state["scan_source"] == "tpager":
        state["scanning"]    = False
        state["scan_source"] = "none"
    return jsonify({"success": True})


@app.route("/api/tpager/cmd", methods=["POST"])
def tpager_cmd():
    data = request.json or {}
    cmd  = data.get("cmd")
    if not cmd:
        return jsonify({"success": False, "error": "No cmd"})
    ok = tpager.send_cmd(data)
    return jsonify({"success": ok})


@app.route("/api/tpager/status")
def tpager_status():
    return jsonify(tpager.get_status())


@app.route("/api/tpager/ports")
def tpager_ports():
    return jsonify({
        "ports": list_serial_ports(),
        "auto":  find_tpager_port(),
    })


# ── GPS Control ─────────────────────────────────────────────────────────────
@app.route("/api/gps/start", methods=["POST"])
def start_gps():
    if state["gps_started"]:
        return jsonify({"success": False, "error": "GPS already started"})
    data        = request.json or {}
    mode        = data.get("mode", "auto")
    serial_port = data.get("serial_port", "/dev/ttyUSB0")
    baudrate    = int(data.get("baudrate", 9600))
    gps.start(mode=mode, serial_port=serial_port, baudrate=baudrate)
    state["gps_mode"]    = mode
    state["gps_started"] = True
    return jsonify({"success": True, "mode": gps.get_mode()})


@app.route("/api/gps/stop", methods=["POST"])
def stop_gps():
    gps.stop()
    state["gps_started"] = False
    state["gps_mode"]    = "none"
    return jsonify({"success": True})


@app.route("/api/gps/manual", methods=["POST"])
def set_manual_gps():
    data = request.json or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, ValueError):
        return jsonify({"success": False, "error": "Invalid lat/lon"})
    if not state["gps_started"]:
        gps.start(mode="manual")
        state["gps_started"] = True
        state["gps_mode"]    = "manual"
    gps.set_manual(lat, lon)
    return jsonify({"success": True, "lat": lat, "lon": lon})


@app.route("/api/gps/status")
def gps_status():
    fix = gps.get_fix()
    return jsonify({"fix": fix.to_dict(), "mode": gps.get_mode(), "has_fix": fix.has_fix()})


# ── Network Data ─────────────────────────────────────────────────────────────
@app.route("/api/networks")
def get_networks():
    fix = gps.get_fix()
    return jsonify({"networks": _get_all_networks(), "stats": _get_stats(), "gps": fix.to_dict()})


@app.route("/api/stats")
def get_stats():
    return jsonify(_get_stats())


@app.route("/api/interfaces")
def list_interfaces_route():
    return jsonify({"interfaces": get_wifi_interfaces()})


# ── Bluetooth Routes ──────────────────────────────────────────────────────────
@app.route("/api/bt/start", methods=["POST"])
def bt_start():
    if state["bt_scanning"]:
        return jsonify({"success": False, "error": "Already scanning"})
    data     = request.json or {}
    interval = float(data.get("interval", 15.0))
    do_ble   = data.get("ble", True)
    do_classic = data.get("classic", True)
    bt_scanner.interval   = interval
    bt_scanner.do_ble     = do_ble
    bt_scanner.do_classic = do_classic
    bt_scanner.start()
    state["bt_scanning"] = True
    return jsonify({"success": True})


@app.route("/api/bt/stop", methods=["POST"])
def bt_stop():
    bt_scanner.stop()
    state["bt_scanning"] = False
    return jsonify({"success": True, "total": bt_scanner.get_stats()["total"]})


@app.route("/api/bt/clear", methods=["POST"])
def bt_clear():
    bt_scanner.clear()
    return jsonify({"success": True})


@app.route("/api/bt/devices")
def bt_devices():
    return jsonify({"devices": bt_scanner.get_devices(), "stats": bt_scanner.get_stats()})


@app.route("/api/bt/status")
def bt_status():
    return jsonify({"scanning": state["bt_scanning"], "stats": bt_scanner.get_stats()})


# ── Export Routes ─────────────────────────────────────────────────────────────
@app.route("/api/export/csv")
def export_csv():
    networks = _get_all_networks()
    csv_data = export_to_csv_string(networks)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=wardriver_{ts}.csv"})


@app.route("/api/export/kml")
def export_kml_route():
    networks = _get_all_networks()
    ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(EXPORTS_DIR, f"wardriver_{ts}.kml")
    export_to_kml(networks, filepath)
    return send_file(filepath, as_attachment=True,
                     download_name=f"wardriver_{ts}.kml",
                     mimetype="application/vnd.google-earth.kml+xml")


@app.route("/api/export/json")
def export_json_route():
    networks = _get_all_networks()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return Response(json.dumps(networks, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=wardriver_{ts}.json"})


# ── WiGLE API Routes ──────────────────────────────────────────────────────────
def _wigle_uploader():
    cred = state.get("wigle_credential", "")
    return WiGLEUploader(cred) if cred else None


@app.route("/api/wigle/login", methods=["POST"])
def wigle_login_route():
    """Save WiGLE API Name + API Token credentials."""
    import base64 as _b64
    data      = request.json or {}
    api_name  = data.get("api_name", "").strip()
    api_token = data.get("api_token", "").strip()
    if not api_name or not api_token:
        return jsonify({"success": False, "error": "API Name and API Token are required"})
    # Validate by calling the profile endpoint
    encoded = _b64.b64encode(f"{api_name}:{api_token}".encode()).decode()
    uploader = WiGLEUploader(encoded)
    result = uploader.test_auth()
    if result["success"]:
        state["wigle_user"]       = api_name
        state["wigle_credential"] = encoded
        _save_creds()
        return jsonify({"success": True, "user": api_name})
    return jsonify({"success": False, "error": result.get("error", "Invalid credentials")})


@app.route("/api/wigle/status")
def wigle_status():
    cred = state.get("wigle_credential", "")
    user = state.get("wigle_user", "")
    return jsonify({"saved": bool(cred), "user": user})


@app.route("/api/wigle/logout", methods=["POST"])
def wigle_logout():
    state["wigle_user"]       = ""
    state["wigle_credential"] = ""
    _save_creds()
    return jsonify({"success": True})


@app.route("/api/wigle/upload", methods=["POST"])
def wigle_upload():
    uploader = _wigle_uploader()
    if not uploader:
        return jsonify({"success": False, "error": "Not logged in to WiGLE"})
    data     = request.json or {}
    donate   = data.get("donate", False)
    networks = _get_all_networks()
    if not networks:
        return jsonify({"success": False, "error": "No networks to upload"})
    csv_data = export_to_csv_string(networks)
    filename = f"wardriver_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    result   = uploader.upload_string(csv_data, filename=filename, donate=donate)
    result["network_count"] = len(networks)
    return jsonify(result)


@app.route("/api/wigle/search", methods=["POST"])
def wigle_search():
    uploader = _wigle_uploader()
    if not uploader:
        return jsonify({"success": False, "error": "Not logged in to WiGLE"})
    data = request.json or {}
    try:
        lat    = float(data["lat"])
        lon    = float(data["lon"])
        radius = float(data.get("radius", 0.5))
    except (KeyError, ValueError):
        fix = gps.get_fix()
        if not fix.has_fix():
            return jsonify({"success": False, "error": "No GPS fix and no coordinates provided"})
        lat, lon, radius = fix.lat, fix.lon, 0.5
    return jsonify(uploader.search_nearby(lat, lon, radius))


@app.route("/api/wigle/uploads")
def wigle_uploads():
    uploader = _wigle_uploader()
    if not uploader:
        return jsonify({"success": False, "error": "Not logged in to WiGLE"})
    return jsonify(uploader.get_uploads())


# ── Phone GPS Routes ──────────────────────────────────────────────────────────
@app.route("/phone")
def phone_page():
    """Mobile page for phone GPS streaming."""
    return render_template("phone.html")


@app.route("/api/phone/qr")
def phone_qr():
    """Generate QR code PNG for the /phone URL (plain HTTP — no cert warning)."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    local_ip = s.getsockname()[0]
    s.close()
    http_port = cfg.PORT + 1
    url = f"http://{local_ip}:{http_port}/phone"
    try:
        import qrcode
        import io
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")
    except ImportError:
        return jsonify({"url": url})


@app.route("/api/phone/url")
def phone_url():
    """Return the URL to open on the phone."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    local_ip = s.getsockname()[0]
    s.close()
    http_port = cfg.PORT + 1
    return jsonify({
        "url":      f"http://{local_ip}:{http_port}/phone",
        "tcp_nmea": f"{local_ip}:10110",
        "local_ip": local_ip,
    })


@app.route("/api/phone/tcp/start", methods=["POST"])
def start_tcp_nmea():
    if state["tcp_nmea_active"]:
        return jsonify({"success": False, "error": "Already running"})
    data = request.json or {}
    port = int(data.get("port", 10110))
    tcp_nmea.port = port
    ok = tcp_nmea.start()
    if ok:
        state["tcp_nmea_active"] = True
    return jsonify({"success": ok, "port": port,
                    "error": None if ok else "Failed to bind port"})


@app.route("/api/phone/tcp/stop", methods=["POST"])
def stop_tcp_nmea():
    tcp_nmea.stop()
    state["tcp_nmea_active"] = False
    return jsonify({"success": True})


@app.route("/api/phone/tcp/status")
def tcp_nmea_status():
    return jsonify(tcp_nmea.get_status())


# ── SocketIO ──────────────────────────────────────────────────────────────────
@socketio.on("phone_gps")
def handle_phone_gps(data):
    """Receive GPS fix from phone browser page."""
    try:
        lat      = float(data["lat"])
        lon      = float(data["lon"])
        alt      = float(data.get("alt", 0) or 0)
        accuracy = float(data.get("accuracy", 0) or 0)
        speed    = float(data.get("speed", 0) or 0)
        heading  = float(data.get("heading", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return

    fix = GPSFix(
        lat=lat, lon=lon, alt=alt,
        accuracy=accuracy, speed=speed, heading=heading,
        fix_type="3d" if alt else "2d",
        source="phone-browser",
    )
    gps.set_manual(lat, lon)
    state["phone_gps_active"] = True

    # Broadcast updated fix to all clients (including the main dashboard)
    socketio.emit("gps_update", fix.to_dict())
    # ACK back to the phone
    emit("phone_gps_ack", {"lat": lat, "lon": lon, "accuracy": accuracy})


@socketio.on("connect")
def on_connect():
    fix = gps.get_fix()
    emit("init", {
        "networks":         _get_all_networks(),
        "stats":            _get_stats(),
        "gps":              fix.to_dict(),
        "scanning":         state["scanning"],
        "scan_source":      state["scan_source"],
        "gps_mode":         gps.get_mode(),
        "tpager_connected": state["tpager_connected"],
        "bt_scanning":      state["bt_scanning"],
        "bt_stats":         bt_scanner.get_stats(),
    })


# ── IP change monitor ─────────────────────────────────────────────────────────
def _ip_monitor(cert_file: str, key_file: str):
    """
    Background greenlet — checks every 15 s if the machine's IP has changed.
    On change: regenerates the SSL cert and notifies connected clients so the
    QR code / phone URL refreshes automatically.
    """
    global _current_ip
    import time as _time
    from gen_cert import generate as _gen_cert

    while True:
        _time.sleep(15)
        try:
            new_ip = cfg.get_local_ip()
            if new_ip != _current_ip and new_ip != "127.0.0.1":
                logger.info(f"IP changed: {_current_ip} → {new_ip}")
                _current_ip = new_ip
                # Regenerate cert for new IP
                try:
                    _gen_cert()
                    logger.info(f"SSL cert regenerated for {new_ip}")
                except Exception as e:
                    logger.warning(f"Cert regen failed: {e}")
                # Notify all browser clients so QR / URL refreshes
                http_port = cfg.PORT + 1
                socketio.emit("ip_changed", {
                    "ip":       new_ip,
                    "phone_url": f"http://{new_ip}:{http_port}/phone",
                })
        except Exception as e:
            logger.debug(f"IP monitor error: {e}")


if __name__ == "__main__":
    from gen_cert import generate as _gen_cert

    CERT_FILE = cfg.SSL_CERT
    KEY_FILE  = cfg.SSL_KEY

    # Auto-generate or regenerate cert if IP changed
    _local_ip = cfg.get_local_ip()
    _regen = True
    if os.path.exists(CERT_FILE):
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            with open(CERT_FILE, "rb") as _f:
                _cert = x509.load_pem_x509_certificate(_f.read(), default_backend())
            _san = _cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName)
            _ips = [str(ip) for ip in _san.value.get_values_for_type(x509.IPAddress)]
            _regen = _local_ip not in _ips
        except Exception:
            _regen = True

    if _regen:
        logger.info(f"Generating SSL cert for {_local_ip}")
        _gen_cert()

    platform_str = f"Raspberry Pi ({cfg.get_pi_model()})" if cfg.IS_PI else "Linux"

    _http_port = cfg.PORT + 1
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║            WARDAEMON  •  WiGLE Wardriving Tool               ║
║         Platform : {platform_str:<40}║
╠══════════════════════════════════════════════════════════════╣
║  Dashboard  :  https://{_local_ip}:{cfg.PORT}
║  Phone/BT   :  http://{_local_ip}:{_http_port}/phone  (scan QR)
╚══════════════════════════════════════════════════════════════╝
""")

    # ── Start Pi display ────────────────────────────────────────────────────
    if _display:
        phone_url = f"https://{_local_ip}:{cfg.PORT}/phone"
        _display.set_phone_url(phone_url)
        _display.start()
        logger.info("Pi display started")

        # Show QR code on startup for 8 seconds, then switch to dashboard
        import eventlet as _ev
        def _switch_to_dash():
            _ev.sleep(8)
            _display._mode = 0  # MODE_DASHBOARD
        _ev.spawn(_switch_to_dash)
        _display._mode = 2  # Start on QR code mode

    # ── Kiosk mode (Chromium) ────────────────────────────────────────────────
    if cfg.IS_PI and getattr(cfg, "DISPLAY_KIOSK", False):
        import subprocess as _sp
        kiosk_url = f"https://localhost:{cfg.PORT}"
        _sp.Popen([
            "chromium-browser", "--kiosk", "--noerrdialogs",
            "--disable-infobars", "--ignore-certificate-errors",
            "--disable-session-crashed-bubble",
            "--autoplay-policy=no-user-gesture-required",
            kiosk_url
        ], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        logger.info(f"Chromium kiosk launched → {kiosk_url}")

    # ── IP change monitor ────────────────────────────────────────────────────
    eventlet.spawn(_ip_monitor, CERT_FILE, KEY_FILE)

    # ── HTTP listener on PORT+1 for phone (no cert warning) ─────────────────
    _http_port = cfg.PORT + 1
    _http_listener = eventlet.listen((cfg.HOST, _http_port),
                                     reuse_addr=True, reuse_port=True)
    eventlet.spawn(eventlet.wsgi.server, _http_listener, app,
                   log_output=False)
    logger.info(f"HTTP phone server on port {_http_port}")

    listener = eventlet.listen((cfg.HOST, cfg.PORT),
                               reuse_addr=True, reuse_port=True)
    listener = eventlet.wrap_ssl(listener,
                                  certfile=CERT_FILE,
                                  keyfile=KEY_FILE,
                                  server_side=True)
    eventlet.wsgi.server(listener, app)
