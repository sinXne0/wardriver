"""
WiFi Scanner Module
Scans for nearby WiFi networks using nmcli or iwlist.
Returns data in WiGLE-compatible format.
"""

import subprocess
import re
import time
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def parse_auth_mode(security: str) -> str:
    """Convert nmcli security string to WiGLE AuthMode format."""
    if not security or security.strip() == "--":
        return "[ESS]"

    sec = security.upper()
    parts = []

    if "WPA3" in sec:
        parts.append("WPA3-SAE-CCMP")
    elif "WPA2" in sec:
        parts.append("WPA2-PSK-CCMP+TKIP")
    elif "WPA" in sec:
        parts.append("WPA-PSK-TKIP")
    elif "WEP" in sec:
        parts.append("WEP")

    if "802.1X" in sec or "EAP" in sec:
        parts.append("WPA2-EAP-CCMP")

    if not parts:
        return "[ESS]"

    return "[" + "][".join(parts) + "][ESS]"


def scan_nmcli(interface: str = None) -> list[dict]:
    """Scan using nmcli - works without root for basic scans."""
    try:
        cmd = [
            "nmcli", "-t", "-f",
            "BSSID,SSID,CHAN,SIGNAL,SECURITY,FREQ,MODE",
            "device", "wifi", "list"
        ]
        if interface:
            cmd += ["ifname", interface]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

        if result.returncode != 0:
            logger.warning(f"nmcli error: {result.stderr.strip()}")
            return []

        networks = []
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        for line in result.stdout.strip().splitlines():
            # nmcli -t uses : as separator, but BSSIDs have colons — handle carefully
            # Format: BSSID:SSID:CHAN:SIGNAL:SECURITY:FREQ:MODE
            # BSSID is XX:XX:XX:XX:XX:XX so split from right
            parts = line.split(":")
            if len(parts) < 11:
                continue

            # BSSID is first 6 hex pairs separated by colons = parts[0:6]
            # nmcli -t escapes colons with \, so strip backslashes from each octet
            bssid = ":".join(p.replace("\\", "") for p in parts[:6]).upper()
            remaining = parts[6:]

            if len(remaining) < 5:
                continue

            ssid = remaining[0].replace("\\:", ":")
            channel = remaining[1]
            signal = remaining[2]
            security = remaining[3]
            freq = remaining[4] if len(remaining) > 4 else ""

            try:
                rssi_raw = int(signal)
                # nmcli returns 0-100 signal quality, convert to approx dBm
                rssi_dbm = (rssi_raw / 2) - 100
            except ValueError:
                rssi_dbm = -100

            try:
                chan = int(channel)
            except ValueError:
                chan = 0

            networks.append({
                "bssid": bssid,
                "ssid": ssid if ssid else "<hidden>",
                "auth_mode": parse_auth_mode(security),
                "first_seen": now,
                "channel": chan,
                "rssi": int(rssi_dbm),
                "frequency": freq,
                "type": "WIFI",
                "raw_security": security,
            })

        return networks

    except subprocess.TimeoutExpired:
        logger.error("nmcli scan timed out")
        return []
    except Exception as e:
        logger.error(f"nmcli scan failed: {e}")
        return []


def scan_iwlist(interface: str = "wlan0") -> list[dict]:
    """Scan using iwlist (requires root/CAP_NET_ADMIN)."""
    try:
        result = subprocess.run(
            ["iwlist", interface, "scan"],
            capture_output=True, text=True, timeout=20
        )

        if result.returncode != 0:
            logger.warning(f"iwlist error: {result.stderr.strip()}")
            return []

        networks = []
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        current = {}

        for line in result.stdout.splitlines():
            line = line.strip()

            if "Cell" in line and "Address:" in line:
                if current:
                    networks.append(current)
                bssid = re.search(r"Address: ([\dA-Fa-f:]{17})", line)
                current = {
                    "bssid": bssid.group(1).upper() if bssid else "00:00:00:00:00:00",
                    "ssid": "<hidden>",
                    "auth_mode": "[ESS]",
                    "first_seen": now,
                    "channel": 0,
                    "rssi": -100,
                    "frequency": "",
                    "type": "WIFI",
                    "raw_security": "",
                }
            elif line.startswith("ESSID:"):
                ssid = re.search(r'ESSID:"(.*?)"', line)
                if ssid and current:
                    current["ssid"] = ssid.group(1) or "<hidden>"
            elif line.startswith("Channel:"):
                chan = re.search(r"Channel:(\d+)", line)
                if chan and current:
                    current["channel"] = int(chan.group(1))
            elif "Frequency:" in line:
                freq = re.search(r"Frequency:([\d.]+)", line)
                if freq and current:
                    current["frequency"] = freq.group(1) + " GHz"
            elif "Signal level=" in line:
                sig = re.search(r"Signal level=(-?\d+)", line)
                if sig and current:
                    current["rssi"] = int(sig.group(1))
            elif "Encryption key:" in line:
                if current:
                    if "off" in line:
                        current["auth_mode"] = "[ESS]"
            elif "IE: IEEE 802.11i/WPA2" in line:
                if current:
                    current["auth_mode"] = "[WPA2-PSK-CCMP][ESS]"
                    current["raw_security"] = "WPA2"
            elif "IE: WPA Version 1" in line:
                if current:
                    current["auth_mode"] = "[WPA-PSK-TKIP][ESS]"
                    current["raw_security"] = "WPA"

        if current:
            networks.append(current)

        return networks

    except subprocess.TimeoutExpired:
        logger.error("iwlist scan timed out")
        return []
    except Exception as e:
        logger.error(f"iwlist scan failed: {e}")
        return []


def get_wifi_interfaces() -> list[str]:
    """Get available WiFi interfaces."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device"],
            capture_output=True, text=True, timeout=5
        )
        interfaces = []
        for line in result.stdout.splitlines():
            if ":wifi" in line:
                interfaces.append(line.split(":")[0])
        return interfaces
    except Exception:
        # Fallback: check /sys/class/net
        try:
            result = subprocess.run(
                ["ls", "/sys/class/net"],
                capture_output=True, text=True
            )
            ifaces = result.stdout.split()
            return [i for i in ifaces if i.startswith(("wlan", "wlp", "wlx", "ath"))]
        except Exception:
            return ["wlan0"]


class WifiScanner:
    """Continuous WiFi scanner with background thread."""

    def __init__(self, interval: float = 5.0, interface: str = None):
        self.interval = interval
        self.interface = interface
        self._running = False
        self._thread = None
        self.networks: dict[str, dict] = {}  # keyed by BSSID
        self.scan_count = 0
        self.last_scan = None
        self._lock = threading.Lock()
        self._callbacks = []

    def on_update(self, callback):
        """Register callback called with new/updated network list."""
        self._callbacks.append(callback)

    def start(self, interface: str = None):
        if interface:
            self.interface = interface
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()
        logger.info(f"WiFi scanner started on {self.interface or 'default'}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WiFi scanner stopped")

    def _scan_loop(self):
        while self._running:
            try:
                self._do_scan()
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
            time.sleep(self.interval)

    def _do_scan(self):
        # Try nmcli first, fall back to iwlist
        networks = scan_nmcli(self.interface)
        if not networks:
            iface = self.interface or "wlan0"
            networks = scan_iwlist(iface)

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        new_found = []

        with self._lock:
            for net in networks:
                bssid = net["bssid"]
                if bssid not in self.networks:
                    net["last_seen"] = now
                    self.networks[bssid] = net
                    new_found.append(bssid)
                else:
                    # Update signal and last seen
                    self.networks[bssid]["rssi"] = net["rssi"]
                    self.networks[bssid]["last_seen"] = now

            self.scan_count += 1
            self.last_scan = now

        for cb in self._callbacks:
            try:
                cb(self.get_networks(), new_found)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def get_networks(self) -> list[dict]:
        with self._lock:
            return list(self.networks.values())

    def get_stats(self) -> dict:
        with self._lock:
            nets = list(self.networks.values())
        total = len(nets)
        open_nets = sum(1 for n in nets if n["auth_mode"] == "[ESS]")
        wpa2 = sum(1 for n in nets if "WPA2" in n["auth_mode"])
        wpa3 = sum(1 for n in nets if "WPA3" in n["auth_mode"])
        wpa = sum(1 for n in nets if "WPA-PSK-TKIP" in n["auth_mode"] and "WPA2" not in n["auth_mode"])
        wep = sum(1 for n in nets if "WEP" in n["auth_mode"])
        return {
            "total": total,
            "open": open_nets,
            "wpa2": wpa2,
            "wpa3": wpa3,
            "wpa": wpa,
            "wep": wep,
            "scans": self.scan_count,
            "last_scan": self.last_scan,
        }

    def clear(self):
        with self._lock:
            self.networks.clear()
            self.scan_count = 0
