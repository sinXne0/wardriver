"""
Bluetooth Scanner Module
Scans for Classic BT and BLE devices in WiGLE-compatible format.

WiGLE Type field:
  BT  = Bluetooth Classic
  BLE = Bluetooth Low Energy
"""

import subprocess
import threading
import time
import logging
import asyncio
import concurrent.futures
from datetime import datetime

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ── Classic BT via bluetoothctl ───────────────────────────────────────────────
def scan_classic_bluetoothctl(timeout: int = 10) -> list[dict]:
    """
    Scan for Classic BT and BLE devices using bluetoothctl.
    bluetoothctl scan on runs both BR/EDR inquiry and LE scan simultaneously,
    so it catches phones, speakers, earbuds, and other BLE advertisers that
    hcitool misses entirely.
    """
    devices = []
    now = _utcnow()
    try:
        # Scan with output so we can read NEW_DEVICE lines and RSSI in real time
        proc = subprocess.run(
            ["bluetoothctl", "--timeout", str(timeout), "scan", "on"],
            capture_output=True, text=True, timeout=timeout + 5
        )

        # Parse any RSSI values seen during the scan from stdout
        rssi_map = {}
        for line in proc.stdout.splitlines():
            # "[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -67"
            if "RSSI:" in line and "Device" in line:
                parts = line.split()
                try:
                    mac  = [p for p in parts if ":" in p and len(p) == 17][0].upper()
                    rssi = int(parts[parts.index("RSSI:") + 1])
                    rssi_map[mac] = rssi
                except (IndexError, ValueError):
                    pass

        # Get the full device list (includes devices found across all scan types)
        list_proc = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True, text=True, timeout=5
        )
        for line in list_proc.stdout.splitlines():
            # Format: "Device AA:BB:CC:DD:EE:FF DeviceName"
            parts = line.strip().split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                mac  = parts[1].upper()
                name = parts[2].strip() if len(parts) > 2 else "<unknown>"
                rssi = rssi_map.get(mac, -100)
                devices.append({
                    "bssid":      mac,
                    "ssid":       name,
                    "auth_mode":  "[BT]",
                    "first_seen": now,
                    "channel":    0,
                    "rssi":       rssi,
                    "type":       "BT",
                    "source":     "bluetoothctl",
                })
    except Exception as e:
        logger.warning(f"bluetoothctl scan error: {e}")
    return devices


def scan_classic_hcitool(timeout: int = 10, hci: str = "hci0") -> list[dict]:
    """Scan for classic BT devices using hcitool scan."""
    devices = []
    now = _utcnow()
    try:
        result = subprocess.run(
            ["hcitool", "-i", hci, "scan", "--flush"],
            capture_output=True, text=True, timeout=timeout + 5
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("Scanning"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                mac  = parts[0].strip().upper()
                name = parts[1].strip() if len(parts) > 1 else "<unknown>"
                devices.append({
                    "bssid":      mac,
                    "ssid":       name,
                    "auth_mode":  "[BT]",
                    "first_seen": now,
                    "channel":    0,
                    "rssi":       -100,
                    "type":       "BT",
                    "source":     "hcitool",
                })
    except Exception as e:
        logger.warning(f"hcitool scan error: {e}")
    return devices


# ── BLE via bleak ─────────────────────────────────────────────────────────────
async def _ble_scan_async(timeout: float = 10.0) -> list[dict]:
    """Async BLE scan using bleak."""
    from bleak import BleakScanner
    devices = []
    now = _utcnow()
    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for addr, (device, adv) in found.items():
            rssi = adv.rssi if adv.rssi is not None else -100
            name = device.name or adv.local_name or "<unknown>"
            # Try to determine BLE type from service UUIDs
            auth = "[BLE]"
            devices.append({
                "bssid":      addr.upper(),
                "ssid":       name,
                "auth_mode":  auth,
                "first_seen": now,
                "channel":    0,
                "rssi":       rssi,
                "type":       "BLE",
                "source":     "bleak",
                "manufacturer": _parse_manufacturer(adv.manufacturer_data),
                "services":   [str(u) for u in (adv.service_uuids or [])],
            })
    except Exception as e:
        logger.warning(f"BLE scan error: {e}")
    return devices


def _parse_manufacturer(mfr_data: dict) -> str:
    """Decode manufacturer ID to brand name where known."""
    if not mfr_data:
        return ""
    known = {
        0x004C: "Apple", 0x0006: "Microsoft", 0x00E0: "Google",
        0x0075: "Samsung", 0x01D6: "Fitbit", 0x0157: "Garmin",
        0x0059: "Nordic Semi", 0x02FF: "Xiaomi",
    }
    cids = list(mfr_data.keys())
    return known.get(cids[0], f"CID:{hex(cids[0])}") if cids else ""


def _run_ble_in_real_thread(timeout: float) -> list[dict]:
    """Run the async BLE scan in a true OS thread to avoid eventlet conflicts."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_ble_scan_async(timeout))
    finally:
        loop.close()


def scan_ble(timeout: float = 10.0) -> list[dict]:
    """Run BLE scan in a real OS thread (safe under eventlet monkey-patching)."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_ble_in_real_thread, timeout)
            return future.result(timeout=timeout + 5)
    except Exception as e:
        logger.error(f"BLE scan failed: {e}")
        return []


# ── Unified BT Scanner ────────────────────────────────────────────────────────
class BTScanner:
    """
    Continuous Bluetooth scanner (Classic + BLE).
    Runs in a background thread, emits discovered devices via callbacks.
    """

    def __init__(self, interval: float = 15.0, scan_classic: bool = True, scan_ble: bool = True):
        self.interval     = interval
        self.do_classic   = scan_classic
        self.do_ble       = scan_ble
        self._running     = False
        self._thread      = None
        self._lock        = threading.Lock()
        self.devices: dict[str, dict] = {}   # keyed by BSSID/MAC
        self.scan_count   = 0
        self._callbacks   = []

    def on_update(self, cb):
        self._callbacks.append(cb)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("BT scanner started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("BT scanner stopped")

    def _loop(self):
        while self._running:
            self._scan()
            time.sleep(self.interval)

    def _scan(self):
        found = []

        # Run BLE and Classic in parallel so they don't block each other
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            if self.do_ble:
                # Give BLE the full interval minus a small margin.
                # Longer windows catch more passive advertisers (phones, earbuds).
                ble_timeout = max(10.0, self.interval - 2)
                futures["ble"] = pool.submit(scan_ble, ble_timeout)
            if self.do_classic:
                futures["classic"] = pool.submit(scan_classic_bluetoothctl, 8)

            for key, f in futures.items():
                try:
                    found += f.result(timeout=15)
                except Exception as e:
                    logger.warning(f"BT scan ({key}) failed: {e}")

        now = _utcnow()
        new_macs = []
        with self._lock:
            for dev in found:
                mac = dev["bssid"]
                if mac not in self.devices:
                    dev["last_seen"] = now
                    self.devices[mac] = dev
                    new_macs.append(mac)
                else:
                    self.devices[mac]["rssi"]      = dev["rssi"]
                    self.devices[mac]["last_seen"] = now
            self.scan_count += 1

        for cb in self._callbacks:
            try:
                cb(self.get_devices(), new_macs)
            except Exception as e:
                logger.error(f"BT callback error: {e}")

    def get_devices(self) -> list[dict]:
        with self._lock:
            return list(self.devices.values())

    def get_stats(self) -> dict:
        devs = self.get_devices()
        return {
            "total":   len(devs),
            "ble":     sum(1 for d in devs if d["type"] == "BLE"),
            "classic": sum(1 for d in devs if d["type"] == "BT"),
            "scans":   self.scan_count,
        }

    def clear(self):
        with self._lock:
            self.devices.clear()
            self.scan_count = 0
