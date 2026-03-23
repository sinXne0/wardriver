# WARDAEMON

> _Haunt the spectrum. Log the grid._

A wardriving tool for Linux and Raspberry Pi with a real-time web dashboard and full phone control via QR code.

Scans WiFi networks and Bluetooth devices, tags them with GPS coordinates, and uploads to [WiGLE](https://wigle.net).

---

## Features

- **WiFi scanning** — nmcli / iwlist, no root required for basic scans
- **Bluetooth scanning** — BLE (via bleak) + Classic BT (via bluetoothctl / hcitool)
- **GPS support** — gpsd, serial NMEA, phone browser, TCP NMEA apps
- **Phone control** — scan the QR code to open a full control UI on your phone
- **WiGLE upload** — login with your WiGLE username and password, upload with one tap
- **Live map** — OpenStreetMap with network markers colour-coded by security type
- **Export** — WiGLE CSV, KML, JSON
- **T-Pager ESP32** — optional hardware scanner over USB serial with onboard GPS
- **Raspberry Pi** — auto-detects Pi hardware, disables WiFi power save, supports Pi display

---

## Screenshots

| Desktop Dashboard | Phone UI |
|---|---|
| Real-time network list, map, stats | Scan control, GPS streaming, WiGLE upload |

---

## Requirements

- Linux (Debian/Ubuntu/Raspberry Pi OS)
- Python 3.10+
- `nmcli` (NetworkManager) — for WiFi scanning
- `bluetoothctl` / `hcitool` — for Classic BT scanning

Python packages are installed automatically by `run.sh`:

```
flask flask-socketio eventlet pyserial requests bleak qrcode[pil] cryptography Pillow
```

---

## Quick Start

```bash
git clone https://github.com/sinXne0/wardaemon.git
cd wardaemon
chmod +x run.sh
./run.sh
```

Open `https://YOUR_IP:5000` in your browser.
The dashboard prints the URL on startup.

---

## Phone Control

1. Open the dashboard on your computer
2. Expand the **Phone GPS** panel — a QR code will appear
3. Scan it with your phone — the phone control page opens instantly (no cert warning)
4. To enable GPS streaming from your phone, tap **"Tap here to open secure version"** and accept the certificate warning once
5. After that, GPS streams from your phone to the tool automatically on every scan

The phone page supports:
- Start / stop WiFi scan
- Start / stop Bluetooth scan
- GPS streaming (sends your phone's location to the tool over WiFi)
- Live network list and map
- WiGLE login and upload
- Export CSV / KML / JSON

---

## GPS Modes

| Mode | How to use |
|---|---|
| **Phone browser** | Scan QR code, open secure version, tap Start GPS Streaming |
| **gpsd** | Set GPS Mode to `gpsd` in the dashboard, start GPS |
| **Serial NMEA** | Connect GPS module to USB/UART, select port and baud rate |
| **TCP NMEA** | Use "Share GPS" (Android) or "GPS2IP" (iOS), point to your machine IP port `10110` |
| **Manual** | Enter lat/lon coordinates directly |

---

## WiGLE Upload

1. Open the **WiGLE Upload** panel (desktop) or the **WiGLE** tab (phone)
2. Enter your WiGLE **username** and **password** — same credentials as the website
3. Click **Login**
4. Start scanning, then click **Upload to WiGLE** when done
5. Optionally check **Donate to WiGLE community** before uploading

Credentials are saved locally (encrypted file, `0600` permissions). Never stored in the repo.

---

## Export Formats

| Format | Use for |
|---|---|
| **WiGLE CSV** | Upload to wigle.net manually or via the API |
| **KML** | Google Earth / Maps — networks plotted as coloured pins |
| **JSON** | Raw data for your own processing |

---

## Raspberry Pi Install

For a full headless Pi setup with auto-start on boot:

```bash
sudo bash install_pi.sh
```

This installs dependencies, copies the service file, and enables it with `systemctl`.

To run as a systemd service manually:

```bash
sudo cp wardriver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wardriver
sudo journalctl -fu wardriver   # view logs
```

> Edit `wardriver.service` to change the `User` and `WorkingDirectory` to match your setup before installing.

---

## T-Pager ESP32 (Optional Hardware)

The [LILYGO T-Pager](https://lilygo.cc) is an ESP32S3 device with a keyboard that can run the wardriving firmware independently and relay data over USB serial.

**Flash the firmware:**

1. Open `tpager_firmware/wardrive/wardrive.ino` in Arduino IDE
2. Install required libraries: `ArduinoJson`, `TinyGPSPlus`, `TFT_eSPI`, `RadioLib`
3. Select board: **ESP32S3 Dev Module**, USB CDC On Boot: **Enabled**
4. Flash at 921600 baud

**Connect to WardriverPy:**

1. Plug the T-Pager in via USB
2. Open the **T-Pager** panel in the dashboard
3. Click **Auto-detect port** or select the port manually
4. Click **Connect**

The T-Pager scans independently and streams networks + GPS data to the dashboard in real time.

See `tpager_firmware/README.md` for full wiring and serial protocol details.

---

## Config

Edit `config.py` to change defaults:

```python
PORT               = 5000       # HTTPS dashboard port (HTTP phone port = PORT+1)
WIFI_INTERFACE     = None       # None = auto-detect
WIFI_SCAN_INTERVAL = 5.0        # seconds between scans
BT_SCAN_INTERVAL   = 15.0       # seconds between BT scans
BLE_ENABLED        = True
GPS_MODE           = "auto"     # auto | gpsd | serial | manual | none
GPS_SERIAL_PORT    = "/dev/ttyUSB0"
GPS_BAUD_RATE      = 9600
LOG_LEVEL          = "INFO"
```

---

## Project Structure

```
wardriver/
├── app.py                  # Flask app, routes, WebSocket handlers
├── config.py               # Hardware config and helpers
├── gen_cert.py             # Self-signed SSL cert generator
├── run.sh                  # Launcher (installs deps, starts app)
├── wardriver.service       # systemd service file
├── modules/
│   ├── wifi_scanner.py     # nmcli / iwlist WiFi scanning
│   ├── bt_scanner.py       # BLE + Classic Bluetooth scanning
│   ├── gps_handler.py      # GPS modes: gpsd, serial, manual
│   ├── phone_gps.py        # TCP NMEA server for GPS apps
│   ├── tpager_bridge.py    # T-Pager USB serial bridge
│   ├── pi_display.py       # Raspberry Pi display support
│   └── wigle_export.py     # WiGLE CSV/KML/JSON export + API upload
├── templates/
│   ├── index.html          # Desktop dashboard
│   └── phone.html          # Mobile phone control UI
├── static/
│   └── socket.io.min.js
└── tpager_firmware/
    └── wardrive/
        └── wardrive.ino    # ESP32 firmware
```

---

## Security Notes

- The dashboard runs on HTTPS with a **self-signed certificate** (auto-generated on first run)
- The phone control page runs on plain HTTP on `PORT+1` so it opens without a cert warning
- GPS streaming from the phone requires HTTPS — tap the banner on the phone page to switch to the secure version
- WiGLE credentials are stored in `.wardriver_creds.json` (mode `0600`, excluded from git)
- The SSL cert and private key are excluded from git via `.gitignore`

---

## Made by [sinXne0](https://github.com/sinXne0)
