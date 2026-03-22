"""
Phone GPS Module
Receives GPS from:
  1. Browser WebSocket (phone opens /phone in browser)
  2. TCP NMEA stream (apps: Share GPS on Android, GPS2IP on iOS)
"""

import socket
import threading
import logging
from datetime import datetime
from modules.gps_handler import GPSFix, NMEAParser

logger = logging.getLogger(__name__)


class TCPNMEAServer:
    """
    Listens for incoming NMEA TCP connections from GPS apps.

    Android: "Share GPS" app → set host = this machine's IP, port = 10110
    iOS:     "GPS2IP"        → set host = this machine's IP, port = 10110
    """

    DEFAULT_PORT = 10110

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self._server = None
        self._running = False
        self._thread = None
        self._callbacks = []
        self.client_addr = None

    def on_fix(self, cb):
        self._callbacks.append(cb)

    def start(self) -> bool:
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("0.0.0.0", self.port))
            self._server.listen(1)
            self._server.settimeout(2)
            self._running = True
            self._thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._thread.start()
            logger.info(f"TCP NMEA server listening on port {self.port}")
            return True
        except Exception as e:
            logger.error(f"TCP NMEA server start failed: {e}")
            return False

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server.accept()
                self.client_addr = addr
                logger.info(f"GPS app connected from {addr}")
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Accept error: {e}")

    def _handle_client(self, conn: socket.socket, addr):
        parser = NMEAParser()
        buffer = b""
        gga_data = None

        try:
            while self._running:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buffer += chunk

                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("ascii", errors="ignore").strip()

                    if not line.startswith("$"):
                        continue

                    sentence_type = line[1:6]

                    if sentence_type in ("GPGGA", "GNGGA"):
                        gga_data = parser.parse_gga(line[7:])
                        if gga_data and gga_data.get("fix"):
                            fix = GPSFix(
                                lat=gga_data["lat"],
                                lon=gga_data["lon"],
                                alt=gga_data["alt"],
                                satellites=gga_data["satellites"],
                                accuracy=gga_data.get("hdop", 0) * 5,
                                fix_type="3d" if gga_data["alt"] else "2d",
                                source=f"tcp:{addr[0]}",
                            )
                            self._emit(fix)

                    elif sentence_type in ("GPRMC", "GNRMC"):
                        rmc = parser.parse_rmc(line[7:])
                        if rmc and gga_data:
                            fix = GPSFix(
                                lat=gga_data["lat"],
                                lon=gga_data["lon"],
                                alt=gga_data.get("alt", 0),
                                satellites=gga_data.get("satellites", 0),
                                accuracy=gga_data.get("hdop", 0) * 5,
                                speed=rmc["speed"],
                                heading=rmc["heading"],
                                fix_type="3d" if gga_data.get("alt") else "2d",
                                source=f"tcp:{addr[0]}",
                            )
                            self._emit(fix)

        except Exception as e:
            logger.error(f"GPS client {addr} error: {e}")
        finally:
            conn.close()
            logger.info(f"GPS app {addr} disconnected")
            self.client_addr = None

    def _emit(self, fix: GPSFix):
        for cb in self._callbacks:
            try:
                cb(fix)
            except Exception as e:
                logger.error(f"TCP NMEA callback error: {e}")

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "port": self.port,
            "client": str(self.client_addr) if self.client_addr else None,
        }
