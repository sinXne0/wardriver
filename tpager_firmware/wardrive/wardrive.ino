/**
 * WardriverPy - T-Pager ESP32 Firmware
 * ======================================
 * Scans WiFi networks and streams JSON data over USB Serial.
 * Optionally reads GPS via UART and displays stats on TFT screen.
 *
 * Compatible with:
 *   - LILYGO T-Pager (ESP32-S3)
 *   - LILYGO T-Deck
 *   - Any ESP32 dev board
 *
 * Libraries required (install via Arduino Library Manager):
 *   - TinyGPSPlus (by Mikal Hart)
 *   - ArduinoJson (by Benoit Blanchon)
 *   - TFT_eSPI (if using display — configure User_Setup.h for your board)
 *
 * Serial Protocol: JSON lines at 115200 baud
 *   Host → Device: {"cmd":"ping"} | {"cmd":"scan_start","interval":5} | {"cmd":"scan_stop"}
 *   Device → Host: {"type":"net",...} | {"type":"gps",...} | {"type":"stat",...} | {"type":"ping",...}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>

// ── GPS (optional) ─────────────────────────────────────────────────────────
// Uncomment and adjust pins for your GPS module
// #define ENABLE_GPS
#ifdef ENABLE_GPS
  #include <TinyGPSPlus.h>
  #define GPS_RX_PIN   18   // Adjust for your T-Pager/T-Deck wiring
  #define GPS_TX_PIN   17
  #define GPS_BAUD     9600
  HardwareSerial GPSSerial(1);
  TinyGPSPlus gpsParser;
#endif

// ── Display (optional) ──────────────────────────────────────────────────────
// Uncomment if your T-Pager has a TFT display and TFT_eSPI is configured
// #define ENABLE_DISPLAY
#ifdef ENABLE_DISPLAY
  #include <TFT_eSPI.h>
  TFT_eSPI tft = TFT_eSPI();
#endif

// ── LoRa (optional) ────────────────────────────────────────────────────────
// Uncomment to enable LoRa relay (sends found networks to a LoRa receiver)
// #define ENABLE_LORA
#ifdef ENABLE_LORA
  #include <RadioLib.h>
  // Adjust pins for your T-Pager LoRa module
  #define LORA_NSS   8
  #define LORA_DIO1  14
  #define LORA_RST   12
  #define LORA_BUSY  13
  SX1262 radio = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);
#endif

// ── Config ─────────────────────────────────────────────────────────────────
#define FW_VERSION     "1.0"
#define SERIAL_BAUD    115200
#define SCAN_INTERVAL  5000     // ms between scans (overridable via command)
#define JSON_BUF_SIZE  512

// ── State ──────────────────────────────────────────────────────────────────
bool scanning = false;
unsigned long lastScanTime = 0;
unsigned long scanInterval = SCAN_INTERVAL;
unsigned long totalNetworks = 0;
unsigned long scanCount = 0;
unsigned long startTime = 0;

double gpsLat = 0.0, gpsLon = 0.0, gpsAlt = 0.0;
int gpsSats = 0;
float gpsSpeed = 0.0;
bool gpsFix = false;
String gpsFix3D = "none";

// Track seen BSSIDs to report first-seen vs updated
// Using a simple hash set approach (limited to 512 entries)
struct BSSIDEntry { uint8_t mac[6]; bool seen; };
#define MAX_TRACKED 512
BSSIDEntry tracked[MAX_TRACKED];
int trackedCount = 0;

// ── Helpers ────────────────────────────────────────────────────────────────
void parseMac(const char* str, uint8_t* mac) {
  sscanf(str, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
         &mac[0], &mac[1], &mac[2], &mac[3], &mac[4], &mac[5]);
}

bool isNewNetwork(const char* bssid) {
  uint8_t mac[6];
  parseMac(bssid, mac);
  for (int i = 0; i < trackedCount; i++) {
    if (memcmp(tracked[i].mac, mac, 6) == 0) return false;
  }
  if (trackedCount < MAX_TRACKED) {
    memcpy(tracked[trackedCount++].mac, mac, 6);
  }
  totalNetworks++;
  return true;
}

const char* authMode(wifi_auth_mode_t enc) {
  switch (enc) {
    case WIFI_AUTH_OPEN:         return "OPEN";
    case WIFI_AUTH_WEP:          return "WEP";
    case WIFI_AUTH_WPA_PSK:      return "WPA";
    case WIFI_AUTH_WPA2_PSK:     return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
    case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2-EAP";
    case WIFI_AUTH_WPA3_PSK:     return "WPA3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default:                     return "UNKNOWN";
  }
}

// ── JSON Emit ──────────────────────────────────────────────────────────────
void emitNetwork(const char* bssid, const char* ssid, int32_t rssi,
                 int32_t channel, const char* auth) {
  StaticJsonDocument<JSON_BUF_SIZE> doc;
  doc["type"]    = "net";
  doc["bssid"]   = bssid;
  doc["ssid"]    = ssid;
  doc["rssi"]    = rssi;
  doc["channel"] = channel;
  doc["auth"]    = auth;
  doc["lat"]     = gpsLat;
  doc["lon"]     = gpsLon;
  doc["alt"]     = gpsAlt;
  doc["acc"]     = gpsFix ? 5.0 : 0.0;
  doc["new"]     = isNewNetwork(bssid);
  serializeJson(doc, Serial);
  Serial.println();
}

void emitGPS() {
  StaticJsonDocument<256> doc;
  doc["type"]  = "gps";
  doc["lat"]   = gpsLat;
  doc["lon"]   = gpsLon;
  doc["alt"]   = gpsAlt;
  doc["sats"]  = gpsSats;
  doc["speed"] = gpsSpeed;
  doc["fix"]   = gpsFix3D;
  doc["has"]   = gpsFix;
  serializeJson(doc, Serial);
  Serial.println();
}

void emitStats() {
  StaticJsonDocument<256> doc;
  doc["type"]   = "stat";
  doc["total"]  = totalNetworks;
  doc["scans"]  = scanCount;
  doc["uptime"] = (millis() - startTime) / 1000;
  doc["gps"]    = gpsFix;
  doc["mem"]    = ESP.getFreeHeap();
  serializeJson(doc, Serial);
  Serial.println();
}

void emitPing() {
  StaticJsonDocument<128> doc;
  doc["type"]    = "ping";
  doc["version"] = FW_VERSION;
  doc["device"]  = "T-Pager";
  doc["chip"]    = ESP.getChipModel();
  doc["freq"]    = ESP.getCpuFreqMHz();
  serializeJson(doc, Serial);
  Serial.println();
}

// ── WiFi Scan ──────────────────────────────────────────────────────────────
void doScan() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  int n = WiFi.scanNetworks(false, true); // async=false, show_hidden=true
  scanCount++;

  if (n == WIFI_SCAN_FAILED || n < 0) {
    return;
  }

  for (int i = 0; i < n; i++) {
    String bssid = WiFi.BSSIDstr(i);
    String ssid  = WiFi.SSID(i);
    int32_t rssi = WiFi.RSSI(i);
    int32_t chan = WiFi.channel(i);
    const char* auth = authMode(WiFi.encryptionType(i));

    emitNetwork(
      bssid.c_str(),
      ssid.length() > 0 ? ssid.c_str() : "",
      rssi, chan, auth
    );
    delay(2); // small yield
  }

  WiFi.scanDelete();

  // Emit stats every 10 scans
  if (scanCount % 10 == 0) {
    emitStats();
  }
}

// ── GPS Update ─────────────────────────────────────────────────────────────
#ifdef ENABLE_GPS
void updateGPS() {
  unsigned long start = millis();
  while (GPSSerial.available() && millis() - start < 100) {
    char c = GPSSerial.read();
    gpsParser.encode(c);
  }

  if (gpsParser.location.isUpdated()) {
    gpsLat   = gpsParser.location.lat();
    gpsLon   = gpsParser.location.lng();
    gpsAlt   = gpsParser.altitude.isValid() ? gpsParser.altitude.meters() : 0.0;
    gpsSats  = gpsParser.satellites.isValid() ? gpsParser.satellites.value() : 0;
    gpsSpeed = gpsParser.speed.isValid() ? gpsParser.speed.mps() : 0.0;
    gpsFix   = gpsParser.location.isValid();
    gpsFix3D = (gpsAlt > 0) ? "3d" : "2d";
    emitGPS();
  }
}
#endif

// ── Display Update ─────────────────────────────────────────────────────────
#ifdef ENABLE_DISPLAY
void updateDisplay() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_CYAN, TFT_BLACK);
  tft.setTextSize(1);
  tft.setCursor(0, 0);
  tft.println("=== WardriverPy ===");

  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.print("Networks: ");
  tft.println(totalNetworks);
  tft.print("Scans:    ");
  tft.println(scanCount);

  tft.setTextColor(gpsFix ? TFT_GREEN : TFT_RED, TFT_BLACK);
  tft.print("GPS: ");
  tft.println(gpsFix ? gpsFix3D : "NO FIX");

  if (gpsFix) {
    tft.setTextColor(TFT_YELLOW, TFT_BLACK);
    tft.print("Lat: "); tft.println(gpsLat, 6);
    tft.print("Lon: "); tft.println(gpsLon, 6);
    tft.print("Sats: "); tft.println(gpsSats);
  }

  tft.setTextColor(scanning ? TFT_GREEN : TFT_RED, TFT_BLACK);
  tft.println(scanning ? "[SCANNING]" : "[STOPPED]");

  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.print("Heap: ");
  tft.print(ESP.getFreeHeap() / 1024);
  tft.println("KB");
}
#endif

// ── Command Parser ─────────────────────────────────────────────────────────
void handleCommand(const String& line) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) return;

  const char* cmd = doc["cmd"];
  if (!cmd) return;

  if (strcmp(cmd, "ping") == 0) {
    emitPing();
  } else if (strcmp(cmd, "scan_start") == 0) {
    scanning = true;
    if (doc.containsKey("interval")) {
      scanInterval = (unsigned long)(doc["interval"].as<int>()) * 1000UL;
    }
    emitPing();
  } else if (strcmp(cmd, "scan_stop") == 0) {
    scanning = false;
    emitStats();
  } else if (strcmp(cmd, "stats") == 0) {
    emitStats();
  } else if (strcmp(cmd, "clear") == 0) {
    trackedCount = 0;
    totalNetworks = 0;
    scanCount = 0;
  }
}

// ── Setup ──────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  startTime = millis();

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

#ifdef ENABLE_GPS
  GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
#endif

#ifdef ENABLE_DISPLAY
  tft.init();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_CYAN);
  tft.setTextSize(2);
  tft.setCursor(10, 20);
  tft.println("WardriverPy");
  tft.setTextSize(1);
  tft.setCursor(10, 50);
  tft.println("Connecting to host...");
#endif

#ifdef ENABLE_LORA
  int loraState = radio.begin();
  if (loraState != RADIOLIB_ERR_NONE) {
    // LoRa init failed — continue without it
  }
#endif

  // Auto-start scanning
  scanning = true;
  emitPing();
}

// ── Loop ───────────────────────────────────────────────────────────────────
void loop() {
  // Handle incoming serial commands
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      handleCommand(line);
    }
  }

#ifdef ENABLE_GPS
  updateGPS();
#endif

  // WiFi scan
  if (scanning && (millis() - lastScanTime >= scanInterval)) {
    lastScanTime = millis();
    doScan();

#ifdef ENABLE_DISPLAY
    updateDisplay();
#endif
  }
}
