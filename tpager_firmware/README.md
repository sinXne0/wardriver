# T-Pager ESP32 Firmware

## Flash Instructions

1. Open `wardrive/wardrive.ino` in Arduino IDE
2. Install required libraries via Library Manager:
   - **ArduinoJson** (Benoit Blanchon) >= 6.x
   - **TinyGPSPlus** (Mikal Hart) — if using GPS
   - **TFT_eSPI** (Bodmer) — if using display, configure `User_Setup.h`
   - **RadioLib** — if using LoRa relay
3. Board settings (Tools menu):
   - Board: **LILYGO T-Pager** or **ESP32S3 Dev Module**
   - USB CDC On Boot: **Enabled**
   - Flash Size: 16MB (or match your board)
4. Flash at 921600 baud
5. Open Serial Monitor at **115200 baud** to verify

## Enable Optional Features

Edit the top of `wardrive.ino` and uncomment:
```cpp
#define ENABLE_GPS     // requires GPS module on UART1
#define ENABLE_DISPLAY // requires TFT_eSPI configured
#define ENABLE_LORA    // requires RadioLib + SX1262 module
```

## GPS Wiring (T-Pager)
Connect a NEO-6M/NEO-M8 GPS module:
- GPS TX → GPIO 18 (GPS_RX_PIN)
- GPS RX → GPIO 17 (GPS_TX_PIN)
- VCC → 3.3V
- GND → GND

Change `GPS_RX_PIN` / `GPS_TX_PIN` in the sketch to match your wiring.

## Serial Protocol

The firmware communicates with WardriverPy over USB Serial (115200 baud):

**Host → Device:**
```json
{"cmd":"ping"}
{"cmd":"scan_start","interval":5}
{"cmd":"scan_stop"}
{"cmd":"stats"}
{"cmd":"clear"}
```

**Device → Host:**
```json
{"type":"ping","version":"1.0","device":"T-Pager"}
{"type":"net","bssid":"AA:BB:CC:DD:EE:FF","ssid":"MyWifi","rssi":-70,"channel":6,"auth":"WPA2","lat":51.5,"lon":-0.1,"alt":10.0,"acc":5.0,"new":true}
{"type":"gps","lat":51.5,"lon":-0.1,"alt":10.0,"sats":8,"speed":1.2,"fix":"3d","has":true}
{"type":"stat","total":42,"scans":100,"uptime":3600,"mem":245000}
```
