"""
T-Pager Bridge Module
Communicates with the LILYGO T-Pager (ESP32-based) over USB serial.
The T-Pager runs wardrive.ino and sends JSON-line data to the host.

Protocol (JSON lines):
  {"type":"net","bssid":"AA:BB:CC:DD:EE:FF","ssid":"MyWifi","rssi":-70,
   "channel":6,"auth":"WPA2","lat":0.0,"lon":0.0,"alt":0.0}
  {"type":"gps","lat":51.5,"lon":-0.1,"alt":50.0,"fix":"3d","sats":8,"speed":0}
  {"type":"stat","total":42,"scans":10,"uptime":3600}
  {"type":"ping","version":"1.0"}
"""

import serial
import serial.tools.list_ports
import json
import threading
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def find_tpager_port() -> str | None:
    """Auto-detect T-Pager / ESP32 USB serial port."""
    # Common ESP32 USB-to-serial chips: CP2102, CH340, CH9102
    TARGET_VIDS = {0x10C4, 0x1A86, 0x303A}  # Silicon Labs, QinHeng, Espressif
    for port in serial.tools.list_ports.comports():
        if port.vid in TARGET_VIDS:
            return port.device
        # Also try common device names
        if any(x in (port.device or "") for x in ("ttyUSB", "ttyACM", "ttyS")):
            return port.device
    return None


def list_serial_ports() -> list[dict]:
    """List all serial ports with device info."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description or "",
            "hwid": p.hwid or "",
            "vid": hex(p.vid) if p.vid else "",
        })
    return ports


class TPagerBridge:
    """
    Bridge between the T-Pager ESP32 device and the wardriver app.
    Reads JSON lines from USB serial and dispatches to callbacks.
    """

    def __init__(self, port: str = None, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._ser = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self.connected = False
        self.device_version = None
        self.last_ping = None

        # Callbacks
        self._net_callbacks = []
        self._gps_callbacks = []
        self._stat_callbacks = []
        self._status_callbacks = []

    # ── Callback Registration ─────────────────────────────────────────────
    def on_network(self, cb): self._net_callbacks.append(cb)
    def on_gps(self, cb):     self._gps_callbacks.append(cb)
    def on_stat(self, cb):    self._stat_callbacks.append(cb)
    def on_status(self, cb):  self._status_callbacks.append(cb)

    # ── Connection ────────────────────────────────────────────────────────
    def connect(self, port: str = None) -> bool:
        if port:
            self.port = port
        if not self.port:
            self.port = find_tpager_port()
        if not self.port:
            logger.error("No T-Pager port found")
            return False

        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=2)
            self.connected = True
            logger.info(f"T-Pager connected on {self.port} @ {self.baudrate} baud")

            # Send ping to check device
            self._ser.write(b'{"cmd":"ping"}\n')
            return True
        except Exception as e:
            logger.error(f"T-Pager connect failed: {e}")
            return False

    def disconnect(self):
        self._running = False
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self.connected = False

    # ── Start/Stop ────────────────────────────────────────────────────────
    def start(self, port: str = None) -> bool:
        if self._running:
            return True
        if not self.connect(port):
            return False
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self.disconnect()
        if self._thread:
            self._thread.join(timeout=3)

    # ── Command Sending ───────────────────────────────────────────────────
    def send_cmd(self, cmd: dict):
        """Send a JSON command to the T-Pager."""
        if not self._ser or not self.connected:
            return False
        try:
            self._ser.write((json.dumps(cmd) + "\n").encode())
            return True
        except Exception as e:
            logger.error(f"Send command error: {e}")
            return False

    def start_scan(self, interval: int = 5):
        return self.send_cmd({"cmd": "scan_start", "interval": interval})

    def stop_scan(self):
        return self.send_cmd({"cmd": "scan_stop"})

    def set_display_mode(self, mode: str):
        """Set T-Pager display: stats | map | list"""
        return self.send_cmd({"cmd": "display", "mode": mode})

    def ping(self):
        return self.send_cmd({"cmd": "ping"})

    # ── Read Loop ─────────────────────────────────────────────────────────
    def _read_loop(self):
        buffer = b""
        while self._running and self._ser:
            try:
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        self._dispatch(line.decode("utf-8", errors="ignore"))
            except serial.SerialException as e:
                logger.error(f"Serial read error: {e}")
                self.connected = False
                self._notify_status("disconnected", str(e))
                break
            except Exception as e:
                logger.error(f"Read loop error: {e}")

        self.connected = False

    def _dispatch(self, line: str):
        """Parse and dispatch a JSON line from the T-Pager."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON from T-Pager: {line[:80]}")
            return

        msg_type = data.get("type", "")

        if msg_type == "net":
            # Normalize to match our internal format
            net = {
                "bssid": data.get("bssid", "").upper(),
                "ssid": data.get("ssid", "<hidden>") or "<hidden>",
                "auth_mode": self._parse_auth(data.get("auth", "")),
                "first_seen": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "channel": data.get("channel", 0),
                "rssi": data.get("rssi", -100),
                "lat": data.get("lat", 0.0),
                "lon": data.get("lon", 0.0),
                "alt": data.get("alt", 0.0),
                "accuracy": data.get("acc", 0.0),
                "type": "WIFI",
                "source": "tpager",
            }
            for cb in self._net_callbacks:
                try:
                    cb(net)
                except Exception as e:
                    logger.error(f"Net callback error: {e}")

        elif msg_type == "gps":
            for cb in self._gps_callbacks:
                try:
                    cb(data)
                except Exception as e:
                    logger.error(f"GPS callback error: {e}")

        elif msg_type == "stat":
            for cb in self._stat_callbacks:
                try:
                    cb(data)
                except Exception as e:
                    logger.error(f"Stat callback error: {e}")

        elif msg_type == "ping":
            self.device_version = data.get("version", "unknown")
            self.last_ping = datetime.utcnow().isoformat()
            self._notify_status("connected", f"T-Pager v{self.device_version}")

    def _parse_auth(self, auth: str) -> str:
        auth = auth.upper()
        if "WPA3" in auth:
            return "[WPA3-SAE-CCMP][ESS]"
        elif "WPA2" in auth:
            return "[WPA2-PSK-CCMP][ESS]"
        elif "WPA" in auth:
            return "[WPA-PSK-TKIP][ESS]"
        elif "WEP" in auth:
            return "[WEP][ESS]"
        return "[ESS]"

    def _notify_status(self, status: str, message: str = ""):
        for cb in self._status_callbacks:
            try:
                cb({"status": status, "message": message, "port": self.port})
            except Exception as e:
                logger.error(f"Status callback error: {e}")

    def get_status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "baudrate": self.baudrate,
            "version": self.device_version,
            "last_ping": self.last_ping,
        }
