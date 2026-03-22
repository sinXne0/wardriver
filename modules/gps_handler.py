"""
GPS Handler Module
Supports:
  1. gpsd daemon (via socket)
  2. Serial NMEA GPS device (e.g. USB GPS dongle)
  3. Manual coordinate input (fallback)
"""

import socket
import json
import threading
import time
import logging
import re
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class GPSFix:
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    accuracy: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    satellites: int = 0
    fix_type: str = "none"   # none, 2d, 3d
    timestamp: str = ""
    source: str = "none"

    def has_fix(self) -> bool:
        return self.fix_type in ("2d", "3d") and (self.lat != 0.0 or self.lon != 0.0)

    def to_dict(self) -> dict:
        return asdict(self)


class GPSDClient:
    """Connect to gpsd via its JSON socket protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 2947):
        self.host = host
        self.port = port
        self._sock = None

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(3)
            self._sock.connect((self.host, self.port))
            # Enable JSON watch mode
            self._sock.send(b'?WATCH={"enable":true,"json":true}\n')
            return True
        except Exception as e:
            logger.warning(f"gpsd connect failed: {e}")
            return False

    def read_fix(self) -> GPSFix | None:
        if not self._sock:
            return None
        try:
            data = b""
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break

            for line in data.decode(errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("class") == "TPV":
                    fix = GPSFix()
                    fix.lat = obj.get("lat", 0.0)
                    fix.lon = obj.get("lon", 0.0)
                    fix.alt = obj.get("alt", 0.0)
                    fix.speed = obj.get("speed", 0.0)
                    fix.heading = obj.get("track", 0.0)
                    fix.accuracy = obj.get("eph", 0.0)
                    fix.timestamp = obj.get("time", "")
                    mode = obj.get("mode", 0)
                    fix.fix_type = {1: "none", 2: "2d", 3: "3d"}.get(mode, "none")
                    fix.source = "gpsd"
                    return fix

        except socket.timeout:
            pass
        except Exception as e:
            logger.error(f"gpsd read error: {e}")
        return None

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


class NMEAParser:
    """Parse NMEA sentences from serial GPS."""

    @staticmethod
    def parse_gga(sentence: str) -> dict | None:
        """Parse GPGGA / GNGGA sentence."""
        parts = sentence.split(",")
        if len(parts) < 10:
            return None
        try:
            if not parts[2] or not parts[4]:
                return None

            lat_raw = float(parts[2])
            lat_dir = parts[3]
            lon_raw = float(parts[4])
            lon_dir = parts[5]

            lat = NMEAParser._nmea_to_decimal(lat_raw, lat_dir)
            lon = NMEAParser._nmea_to_decimal(lon_raw, lon_dir)

            fix_quality = int(parts[6]) if parts[6] else 0
            satellites = int(parts[7]) if parts[7] else 0
            alt = float(parts[9]) if parts[9] else 0.0
            hdop = float(parts[8]) if parts[8] else 0.0

            return {
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "satellites": satellites,
                "hdop": hdop,
                "fix": fix_quality > 0,
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_rmc(sentence: str) -> dict | None:
        """Parse GPRMC / GNRMC sentence."""
        parts = sentence.split(",")
        if len(parts) < 8:
            return None
        try:
            status = parts[2]
            if status != "A":
                return None

            lat_raw = float(parts[3])
            lat_dir = parts[4]
            lon_raw = float(parts[5])
            lon_dir = parts[6]
            speed_knots = float(parts[7]) if parts[7] else 0.0
            heading = float(parts[8]) if parts[8] else 0.0

            lat = NMEAParser._nmea_to_decimal(lat_raw, lat_dir)
            lon = NMEAParser._nmea_to_decimal(lon_raw, lon_dir)

            return {
                "lat": lat,
                "lon": lon,
                "speed": speed_knots * 0.514444,  # knots to m/s
                "heading": heading,
                "fix": True,
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _nmea_to_decimal(value: float, direction: str) -> float:
        degrees = int(value / 100)
        minutes = value - (degrees * 100)
        decimal = degrees + (minutes / 60)
        if direction in ("S", "W"):
            decimal = -decimal
        return round(decimal, 7)


class SerialGPS:
    """Read GPS from a serial port (USB dongle, etc.)."""

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._ser = None

    def connect(self) -> bool:
        try:
            import serial
            self._ser = serial.Serial(self.port, self.baudrate, timeout=2)
            logger.info(f"Serial GPS connected on {self.port}")
            return True
        except Exception as e:
            logger.warning(f"Serial GPS connect failed: {e}")
            return False

    def read_fix(self) -> GPSFix | None:
        if not self._ser:
            return None
        try:
            fix = GPSFix()
            parser = NMEAParser()
            gga_data = None
            rmc_data = None

            # Read up to 20 lines to get a complete fix
            for _ in range(20):
                line = self._ser.readline().decode("ascii", errors="ignore").strip()
                if not line.startswith("$"):
                    continue

                sentence_type = line[1:6]

                if sentence_type in ("GPGGA", "GNGGA"):
                    gga_data = parser.parse_gga(line[7:])
                elif sentence_type in ("GPRMC", "GNRMC"):
                    rmc_data = parser.parse_rmc(line[7:])

                if gga_data and rmc_data:
                    break

            if gga_data and gga_data.get("fix"):
                fix.lat = gga_data["lat"]
                fix.lon = gga_data["lon"]
                fix.alt = gga_data["alt"]
                fix.satellites = gga_data["satellites"]
                fix.accuracy = gga_data.get("hdop", 0) * 5  # rough meters
                fix.fix_type = "3d" if fix.alt else "2d"
                fix.source = f"serial:{self.port}"

            if rmc_data:
                if not gga_data:
                    fix.lat = rmc_data["lat"]
                    fix.lon = rmc_data["lon"]
                    fix.fix_type = "2d"
                    fix.source = f"serial:{self.port}"
                fix.speed = rmc_data["speed"]
                fix.heading = rmc_data["heading"]

            return fix if fix.has_fix() else None

        except Exception as e:
            logger.error(f"Serial GPS read error: {e}")
            return None

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


class GPSHandler:
    """
    Unified GPS handler.
    Tries: gpsd → serial → manual coords
    """

    def __init__(self):
        self._fix = GPSFix()
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._mode = "none"    # gpsd | serial | manual | none
        self._gpsd = None
        self._serial = None
        self._manual_lat = 0.0
        self._manual_lon = 0.0
        self._callbacks = []

    def on_fix(self, callback):
        self._callbacks.append(callback)

    def set_manual(self, lat: float, lon: float):
        """Set manual GPS coordinates (fallback when no GPS hardware)."""
        self._manual_lat = lat
        self._manual_lon = lon
        with self._lock:
            self._fix = GPSFix(
                lat=lat, lon=lon, fix_type="2d", source="manual"
            )
        self._mode = "manual"
        logger.info(f"Manual GPS set: {lat}, {lon}")

    def start(self, mode: str = "auto", serial_port: str = "/dev/ttyUSB0", baudrate: int = 9600):
        """
        mode: auto | gpsd | serial | manual
        """
        if self._running:
            return

        self._running = True

        if mode in ("auto", "gpsd"):
            self._gpsd = GPSDClient()
            if self._gpsd.connect():
                self._mode = "gpsd"
            elif mode == "auto":
                self._gpsd = None

        if self._mode != "gpsd" and mode in ("auto", "serial"):
            self._serial = SerialGPS(serial_port, baudrate)
            if self._serial.connect():
                self._mode = "serial"
            elif mode == "auto":
                self._serial = None

        if self._mode == "none":
            self._mode = "manual"
            logger.info("No GPS hardware found — using manual coordinates")

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._gpsd:
            self._gpsd.close()
        if self._serial:
            self._serial.close()
        if self._thread:
            self._thread.join(timeout=3)

    def _poll_loop(self):
        while self._running:
            fix = None
            if self._mode == "gpsd" and self._gpsd:
                fix = self._gpsd.read_fix()
            elif self._mode == "serial" and self._serial:
                fix = self._serial.read_fix()
            elif self._mode == "manual":
                fix = GPSFix(
                    lat=self._manual_lat,
                    lon=self._manual_lon,
                    fix_type="2d" if (self._manual_lat or self._manual_lon) else "none",
                    source="manual"
                )

            if fix:
                with self._lock:
                    self._fix = fix
                for cb in self._callbacks:
                    try:
                        cb(fix)
                    except Exception as e:
                        logger.error(f"GPS callback error: {e}")

            time.sleep(1)

    def get_fix(self) -> GPSFix:
        with self._lock:
            return self._fix

    def get_mode(self) -> str:
        return self._mode

    def get_available_serial_ports(self) -> list[str]:
        import glob
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyS*")
        return sorted(ports)
