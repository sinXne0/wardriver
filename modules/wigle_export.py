"""
WiGLE Export Module
Generates WiGLE-compatible CSV files and uploads via WiGLE API v2.
"""

import base64
import csv
import io
import os
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

WIGLE_CSV_HEADER_LINE = (
    "WigleWifi-1.4,"
    "appRelease=1.0,"
    "model=WardriverPy,"
    "release=1.0,"
    "device=linux,"
    "display=linux,"
    "board=linux,"
    "brand=custom"
)

WIGLE_CSV_COLUMNS = [
    "MAC",
    "SSID",
    "AuthMode",
    "FirstSeen",
    "Channel",
    "RSSI",
    "CurrentLatitude",
    "CurrentLongitude",
    "AltitudeMeters",
    "AccuracyMeters",
    "Type",
]


def network_to_wigle_row(net: dict) -> list:
    """Convert a network dict to a WiGLE CSV row."""
    return [
        net.get("bssid", ""),
        net.get("ssid", ""),
        net.get("auth_mode", "[ESS]"),
        net.get("first_seen", ""),
        str(net.get("channel", 0)),
        str(net.get("rssi", -100)),
        str(net.get("lat", 0.0)),
        str(net.get("lon", 0.0)),
        str(net.get("alt", 0.0)),
        str(net.get("accuracy", 0.0)),
        net.get("type", "WIFI"),
    ]


def export_to_csv(networks: list[dict], filepath: str) -> str:
    """Write networks to a WiGLE-format CSV file. Returns path."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        f.write(WIGLE_CSV_HEADER_LINE + "\n")
        writer = csv.writer(f)
        writer.writerow(WIGLE_CSV_COLUMNS)
        for net in networks:
            writer.writerow(network_to_wigle_row(net))

    logger.info(f"Exported {len(networks)} networks to {filepath}")
    return filepath


def export_to_csv_string(networks: list[dict]) -> str:
    """Return WiGLE CSV as a string (for download)."""
    output = io.StringIO()
    output.write(WIGLE_CSV_HEADER_LINE + "\n")
    writer = csv.writer(output)
    writer.writerow(WIGLE_CSV_COLUMNS)
    for net in networks:
        writer.writerow(network_to_wigle_row(net))
    return output.getvalue()


def export_to_kml(networks: list[dict], filepath: str) -> str:
    """Export networks to KML format for Google Maps/Earth."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    security_colors = {
        "open": "ff0000ff",      # red
        "wep": "ff00ffff",       # yellow
        "wpa": "ff00ff00",       # green
        "wpa2": "ff008000",      # dark green
        "wpa3": "ff004000",      # darkest green
    }

    def get_color(auth: str) -> str:
        auth_upper = auth.upper()
        if "WPA3" in auth_upper:
            return security_colors["wpa3"]
        elif "WPA2" in auth_upper:
            return security_colors["wpa2"]
        elif "WPA" in auth_upper:
            return security_colors["wpa"]
        elif "WEP" in auth_upper:
            return security_colors["wep"]
        return security_colors["open"]

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    lines.append("<Document>")
    lines.append("<name>Wardriver Export</name>")

    for net in networks:
        lat = net.get("lat", 0.0)
        lon = net.get("lon", 0.0)
        if lat == 0.0 and lon == 0.0:
            continue

        ssid = net.get("ssid", "<hidden>").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        bssid = net.get("bssid", "")
        auth = net.get("auth_mode", "[ESS]")
        rssi = net.get("rssi", -100)
        channel = net.get("channel", 0)
        color = get_color(auth)

        lines.append(f"<Placemark>")
        lines.append(f"<name>{ssid}</name>")
        lines.append(f"<description>BSSID: {bssid}&#10;Auth: {auth}&#10;RSSI: {rssi} dBm&#10;Channel: {channel}</description>")
        lines.append(f"<Style><IconStyle><color>{color}</color></IconStyle></Style>")
        lines.append(f"<Point><coordinates>{lon},{lat},0</coordinates></Point>")
        lines.append("</Placemark>")

    lines.append("</Document></kml>")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"Exported {len(networks)} networks to KML: {filepath}")
    return filepath


def wigle_login(username: str, password: str) -> dict:
    """
    Exchange a WiGLE username + password for an encoded API credential.
    WiGLE API: GET /api/v2/auth/login  (HTTP Basic with username:password)
    Returns {"success": True, "encoded": "...", "user": "..."}
    """
    try:
        credential = base64.b64encode(f"{username}:{password}".encode()).decode()
        r = requests.get(
            "https://api.wigle.net/api/v2/auth/login",
            headers={
                "Authorization": f"Basic {credential}",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                return {
                    "success": True,
                    "encoded": data.get("encodedCredentials", ""),
                    "user": data.get("user", username),
                }
            return {"success": False, "error": data.get("message", "Login failed")}
        if r.status_code == 401:
            return {"success": False, "error": "Invalid username or password"}
        return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


class WiGLEUploader:
    """Upload WiGLE CSV files to wigle.net API v2."""

    API_BASE = "https://api.wigle.net/api/v2"

    def __init__(self, encoded_credential: str):
        """
        encoded_credential: the base64 string returned by /auth/login
        (or manually constructed as base64(api_name:api_token) for legacy key auth).
        """
        self.encoded_credential = encoded_credential
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Basic {encoded_credential}",
            "Accept": "application/json",
        })

    @classmethod
    def from_api_key(cls, api_name: str, api_token: str) -> "WiGLEUploader":
        """Build uploader from legacy API name + token."""
        encoded = base64.b64encode(f"{api_name}:{api_token}".encode()).decode()
        return cls(encoded)

    def test_auth(self) -> dict:
        """Test credentials. Returns user info or error."""
        try:
            r = self._session.get(f"{self.API_BASE}/profile/user", timeout=10)
            if r.status_code == 200:
                return {"success": True, "data": r.json()}
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def upload_file(self, filepath: str, donate: bool = False) -> dict:
        """Upload a WiGLE CSV file. Returns API response."""
        try:
            if not os.path.exists(filepath):
                return {"success": False, "error": "File not found"}

            with open(filepath, "rb") as f:
                files = {"file": (os.path.basename(filepath), f, "text/csv")}
                data = {"donate": "true" if donate else "false"}
                r = self._session.post(
                    f"{self.API_BASE}/file/upload",
                    files=files,
                    data=data,
                    timeout=60,
                )

            if r.status_code == 200:
                result = r.json()
                if result.get("success"):
                    return {
                        "success": True,
                        "file_id": result.get("fileId", ""),
                        "message": f"Uploaded successfully. File ID: {result.get('fileId', '')}",
                    }
                return {"success": False, "error": result.get("message", "Unknown error")}

            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def upload_string(self, csv_data: str, filename: str = None, donate: bool = False) -> dict:
        """Upload WiGLE CSV from a string."""
        if not filename:
            filename = f"wardriver_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            files = {"file": (filename, csv_data.encode("utf-8"), "text/csv")}
            data = {"donate": "true" if donate else "false"}
            r = self._session.post(
                f"{self.API_BASE}/file/upload",
                files=files,
                data=data,
                timeout=60,
            )
            if r.status_code == 200:
                result = r.json()
                if result.get("success"):
                    return {
                        "success": True,
                        "file_id": result.get("fileId", ""),
                        "message": f"Uploaded. File ID: {result.get('fileId', '')}",
                    }
                return {"success": False, "error": result.get("message", "Unknown error")}
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_uploads(self) -> dict:
        """Get list of previous uploads."""
        try:
            r = self._session.get(f"{self.API_BASE}/file/transactions", timeout=10)
            if r.status_code == 200:
                return {"success": True, "data": r.json()}
            return {"success": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def search_nearby(self, lat: float, lon: float, radius_km: float = 0.5) -> dict:
        """Search WiGLE for networks near given coordinates."""
        try:
            params = {
                "latrange1": lat - (radius_km / 111),
                "latrange2": lat + (radius_km / 111),
                "longrange1": lon - (radius_km / (111 * abs(lat or 1) / 90 + 1)),
                "longrange2": lon + (radius_km / (111 * abs(lat or 1) / 90 + 1)),
                "resultsPerPage": 100,
            }
            r = self._session.get(
                f"{self.API_BASE}/network/search",
                params=params,
                timeout=15,
            )
            if r.status_code == 200:
                return {"success": True, "data": r.json()}
            return {"success": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
