"""
Pi Display Module — WardriverPy
Renders a live wardriving dashboard using pygame.

Works with:
  - Official Raspberry Pi 7" touchscreen (800x480)
  - Small TFT HATs via framebuffer (320x240, 480x320, 480x800)
  - Any HDMI/DSI display
  - Headless (gracefully skips if no display)

Touch / click cycles through display modes.
"""

import os
import io
import threading
import time
import logging
import textwrap
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Display Modes ─────────────────────────────────────────────────────────────
MODE_DASHBOARD  = 0   # stats + GPS + recent networks
MODE_NETWORKS   = 1   # full scrollable network list
MODE_QR         = 2   # big QR code + phone URL
MODE_BT         = 3   # Bluetooth device list
MODE_COUNT      = 4

# ── Colour palette ────────────────────────────────────────────────────────────
C = {
    "bg":      (10,  14,  26),
    "bg2":     (17,  24,  39),
    "bg3":     (28,  35,  51),
    "accent":  (0,   212, 255),
    "green":   (0,   255, 136),
    "red":     (255, 68,  85),
    "yellow":  (255, 215, 0),
    "orange":  (255, 140, 0),
    "purple":  (155, 89,  255),
    "bt":      (0,   130, 252),
    "ble":     (123, 97,  255),
    "muted":   (107, 122, 153),
    "text":    (224, 232, 240),
    "white":   (255, 255, 255),
    "border":  (30,  45,  74),
}


def _sec_color(auth: str, net_type: str) -> tuple:
    if net_type == "BLE":       return C["ble"]
    if net_type == "BT":        return C["bt"]
    if not auth or auth == "[ESS]": return C["red"]
    if "WPA3" in auth:          return C["purple"]
    if "WPA2" in auth:          return C["green"]
    if "WPA"  in auth:          return C["accent"]
    if "WEP"  in auth:          return C["orange"]
    return C["red"]


def _rssi_bars(rssi: int, x: int, y: int, surface, bar_w=4, bar_gap=2, max_h=14):
    """Draw 4 signal-strength bars."""
    pct = max(0, min(100, (rssi + 100) * 2))
    col = C["green"] if pct > 60 else C["yellow"] if pct > 30 else C["red"]
    for i in range(4):
        h = int(max_h * (i + 1) / 4)
        rect_y = y + (max_h - h)
        rect = (x + i * (bar_w + bar_gap), rect_y, bar_w, h)
        color = col if pct > (i / 4) * 100 else C["border"]
        import pygame
        pygame.draw.rect(surface, color, rect, border_radius=1)


class PiDisplay:
    """
    Pygame-based wardriving display.
    Call start() to launch in a background thread.
    Call update(networks, stats, gps_fix, state) to push new data.
    """

    def __init__(self, width: int = None, height: int = None,
                 fullscreen: bool = True, phone_url: str = ""):
        self.width       = width
        self.height      = height
        self.fullscreen  = fullscreen
        self.phone_url   = phone_url

        self._running    = False
        self._thread     = None
        self._lock       = threading.Lock()
        self._mode       = MODE_DASHBOARD
        self._scroll     = 0

        # Shared data (updated via update())
        self._networks   = []
        self._stats      = {}
        self._gps        = {}
        self._state      = {}
        self._qr_surface = None
        self._dirty      = True

    # ── Public API ────────────────────────────────────────────────────────────
    def update(self, networks: list, stats: dict, gps: dict, state: dict):
        with self._lock:
            self._networks = networks
            self._stats    = stats
            self._gps      = gps
            self._state    = state
            self._dirty    = True

    def set_phone_url(self, url: str):
        with self._lock:
            self.phone_url = url
            self._qr_surface = None  # regenerate QR

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="pi-display")
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Main Loop ─────────────────────────────────────────────────────────────
    def _run(self):
        try:
            self._main_loop()
        except Exception as e:
            logger.error(f"Display error: {e}", exc_info=True)

    def _main_loop(self):
        import pygame

        # Use framebuffer if no display env (headless / small TFT)
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "fbcon")
            os.environ.setdefault("SDL_FBDEV", "/dev/fb0")
            os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        try:
            pygame.init()
            pygame.mouse.set_visible(False)
        except Exception as e:
            logger.error(f"pygame init failed: {e}")
            return

        # Determine display size
        try:
            if self.fullscreen:
                info = pygame.display.Info()
                w = self.width  or info.current_w or 800
                h = self.height or info.current_h or 480
                screen = pygame.display.set_mode((w, h), pygame.FULLSCREEN | pygame.NOFRAME)
            else:
                w = self.width  or 800
                h = self.height or 480
                screen = pygame.display.set_mode((w, h))
        except Exception as e:
            logger.error(f"Display mode failed: {e}")
            pygame.quit()
            return

        pygame.display.set_caption("WardriverPy")

        # Load fonts (scaled to screen size)
        scale = max(0.5, min(1.0, w / 800))

        def font(size):
            try:
                return pygame.font.SysFont("monospace", int(size * scale))
            except Exception:
                return pygame.font.Font(None, int(size * scale))

        f_large  = font(32)
        f_medium = font(20)
        f_small  = font(15)
        f_tiny   = font(12)

        clock = pygame.time.Clock()

        while self._running:
            # ── Events ───────────────────────────────────────────────────────
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_q:
                        self._running = False
                    elif event.key == pygame.K_RIGHT or event.key == pygame.K_SPACE:
                        self._mode = (self._mode + 1) % MODE_COUNT
                        self._scroll = 0
                    elif event.key == pygame.K_UP:
                        self._scroll = max(0, self._scroll - 1)
                    elif event.key == pygame.K_DOWN:
                        self._scroll += 1
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                    # Tap right half = next mode, tap left half = prev mode
                    try:
                        tx = event.pos[0] if hasattr(event, 'pos') else event.x * w
                    except Exception:
                        tx = w / 2
                    if tx > w / 2:
                        self._mode = (self._mode + 1) % MODE_COUNT
                    else:
                        self._mode = (self._mode - 1) % MODE_COUNT
                    self._scroll = 0

            # ── Draw ─────────────────────────────────────────────────────────
            with self._lock:
                nets   = list(self._networks)
                stats  = dict(self._stats)
                gps    = dict(self._gps)
                state  = dict(self._state)
                url    = self.phone_url

            screen.fill(C["bg"])

            if   self._mode == MODE_DASHBOARD:  self._draw_dashboard(screen, w, h, nets, stats, gps, state, f_large, f_medium, f_small, f_tiny, scale)
            elif self._mode == MODE_NETWORKS:    self._draw_networks(screen, w, h, nets, f_medium, f_small, f_tiny, scale)
            elif self._mode == MODE_QR:          self._draw_qr(screen, w, h, url, f_large, f_medium, f_small)
            elif self._mode == MODE_BT:          self._draw_bt(screen, w, h, nets, f_medium, f_small, f_tiny, scale)

            # Mode indicator dots at bottom
            self._draw_mode_dots(screen, w, h)

            pygame.display.flip()
            clock.tick(10)  # 10fps — enough for wardriving

        pygame.quit()

    # ── Mode: Dashboard ───────────────────────────────────────────────────────
    def _draw_dashboard(self, screen, w, h, nets, stats, gps, state, f_large, f_medium, f_small, f_tiny, scale):
        import pygame
        pad = int(8 * scale)
        y   = pad

        # ── Header ───────────────────────────────────────────────────────────
        header_h = int(36 * scale)
        pygame.draw.rect(screen, C["bg2"], (0, 0, w, header_h))
        pygame.draw.line(screen, C["accent"], (0, header_h), (w, header_h), 1)

        title = f_medium.render("WARDRIVERPY", True, C["accent"])
        screen.blit(title, (pad, (header_h - title.get_height()) // 2))

        # Status dots: WiFi | BT | GPS
        dot_labels = [
            ("WIFI", state.get("scanning") and state.get("scan_source") == "linux"),
            ("BT",   state.get("bt_scanning")),
            ("GPS",  gps.get("fix_type") not in (None, "none", "")),
        ]
        dx = w - pad
        for label, active in reversed(dot_labels):
            lbl_surf = f_tiny.render(label, True, C["green"] if active else C["muted"])
            dx -= lbl_surf.get_width() + pad
            col = C["green"] if active else C["red"]
            pygame.draw.circle(screen, col, (dx - 8, header_h // 2), 5)
            screen.blit(lbl_surf, (dx, (header_h - lbl_surf.get_height()) // 2))
            dx -= 14

        y = header_h + pad

        # ── Stats row ────────────────────────────────────────────────────────
        box_h = int(60 * scale)
        box_labels = [
            ("TOTAL",   str(stats.get("total",  0)), C["accent"]),
            ("OPEN",    str(stats.get("open",   0)), C["red"]),
            ("WPA2",    str(stats.get("wpa2",   0)), C["green"]),
            ("BLE",     str(stats.get("ble",    0)), C["ble"]),
            ("BT",      str(stats.get("bt_classic", 0)), C["bt"]),
        ]
        n_boxes = len(box_labels)
        box_w   = (w - pad * (n_boxes + 1)) // n_boxes
        bx = pad
        for lbl, val, color in box_labels:
            pygame.draw.rect(screen, C["bg3"], (bx, y, box_w, box_h), border_radius=6)
            v_surf = f_large.render(val, True, color)
            l_surf = f_tiny.render(lbl,  True, C["muted"])
            screen.blit(v_surf, (bx + (box_w - v_surf.get_width())  // 2, y + 4))
            screen.blit(l_surf, (bx + (box_w - l_surf.get_width()) // 2,  y + box_h - l_surf.get_height() - 4))
            bx += box_w + pad
        y += box_h + pad

        # ── GPS panel ────────────────────────────────────────────────────────
        gps_h   = int(52 * scale)
        gps_w   = w // 2 - pad - pad // 2
        pygame.draw.rect(screen, C["bg2"], (pad, y, gps_w, gps_h), border_radius=6)
        pygame.draw.rect(screen, C["border"], (pad, y, gps_w, gps_h), 1, border_radius=6)

        has_fix  = gps.get("fix_type") not in (None, "none", "")
        fix_col  = C["green"] if has_fix else C["red"]
        fix_txt  = (gps.get("fix_type", "none") or "none").upper() + " FIX"
        lat      = gps.get("lat",  0.0)
        lon      = gps.get("lon",  0.0)
        acc      = gps.get("accuracy", 0)
        spd      = gps.get("speed",    0)
        sats     = gps.get("satellites", 0)

        screen.blit(f_small.render(fix_txt,                        True, fix_col),    (pad + 6, y + 4))
        screen.blit(f_tiny.render(f"{lat:.5f}, {lon:.5f}",         True, C["text"]),  (pad + 6, y + 22))
        screen.blit(f_tiny.render(f"±{acc:.0f}m  {sats}sat  {(spd or 0)*3.6:.0f}km/h", True, C["muted"]), (pad + 6, y + 36))

        # ── Time / scan count ────────────────────────────────────────────────
        now_surf = f_small.render(datetime.now().strftime("%H:%M:%S"), True, C["muted"])
        screen.blit(now_surf, (pad + gps_w + pad, y + 4))
        scans_surf = f_tiny.render(f"{len(nets)} networks", True, C["accent"])
        screen.blit(scans_surf, (pad + gps_w + pad, y + 24))

        y += gps_h + pad

        # ── Recent networks ───────────────────────────────────────────────────
        net_h  = int(22 * scale)
        avail  = h - y - int(20 * scale)
        n_show = avail // net_h

        wifi_nets = [n for n in nets if n.get("type") == "WIFI"]
        wifi_nets.sort(key=lambda n: n.get("rssi", -100), reverse=True)
        recent = wifi_nets[:n_show]

        for i, net in enumerate(recent):
            ny    = y + i * net_h
            color = _sec_color(net.get("auth_mode",""), net.get("type","WIFI"))
            ssid  = (net.get("ssid") or "<hidden>")[:24]
            rssi  = net.get("rssi", -100)

            # Row bg on hover (alternate shade)
            if i % 2 == 0:
                pygame.draw.rect(screen, C["bg3"], (pad, ny, w - pad*2, net_h - 1), border_radius=3)

            screen.blit(f_small.render(ssid, True, C["text"]), (pad + 4, ny + 3))
            _rssi_bars(rssi, w - pad - 28, ny + 4, screen, bar_w=4, bar_gap=2, max_h=14)

            sec_lbl = "OPEN" if color == C["red"] else ("WPA3" if "WPA3" in (net.get("auth_mode") or "") else "WPA2" if "WPA2" in (net.get("auth_mode") or "") else "WPA" if "WPA" in (net.get("auth_mode") or "") else "WEP" if "WEP" in (net.get("auth_mode") or "") else "?")
            sl = f_tiny.render(sec_lbl, True, color)
            screen.blit(sl, (w - pad - 80, ny + 5))

    # ── Mode: Network List ────────────────────────────────────────────────────
    def _draw_networks(self, screen, w, h, nets, f_medium, f_small, f_tiny, scale):
        import pygame
        pad = int(8 * scale)

        pygame.draw.rect(screen, C["bg2"], (0, 0, w, int(30 * scale)))
        title = f_medium.render(f"NETWORKS ({len(nets)})", True, C["accent"])
        screen.blit(title, (pad, 5))

        wifi_nets = sorted([n for n in nets if n.get("type") == "WIFI"],
                           key=lambda n: n.get("rssi", -100), reverse=True)
        row_h  = int(26 * scale)
        y_start = int(34 * scale)
        avail   = h - y_start - int(18 * scale)
        n_show  = avail // row_h
        start   = min(self._scroll, max(0, len(wifi_nets) - n_show))
        visible = wifi_nets[start:start + n_show]

        for i, net in enumerate(visible):
            ny    = y_start + i * row_h
            color = _sec_color(net.get("auth_mode",""), "WIFI")
            ssid  = (net.get("ssid") or "<hidden>")[:28]
            rssi  = net.get("rssi", -100)
            chan  = net.get("channel", 0)
            auth  = (net.get("auth_mode") or "")

            if i % 2 == 0:
                pygame.draw.rect(screen, C["bg3"], (0, ny, w, row_h - 1))

            screen.blit(f_small.render(ssid, True, C["text"]),             (pad, ny + 5))
            screen.blit(f_tiny.render(f"Ch{chan}", True, C["muted"]),       (w - 120, ny + 7))
            screen.blit(f_tiny.render(f"{rssi}dBm", True, C["muted"]),     (w - 80, ny + 7))
            _rssi_bars(rssi, w - pad - 28, ny + 6, screen)

    # ── Mode: QR Code ─────────────────────────────────────────────────────────
    def _draw_qr(self, screen, w, h, url, f_large, f_medium, f_small):
        import pygame

        if not url:
            screen.blit(f_medium.render("No URL configured", True, C["muted"]),
                        (w//2 - 80, h//2))
            return

        # Generate QR surface if needed
        if not self._qr_surface and url:
            try:
                import qrcode
                qr_img = qrcode.make(url)
                qr_size = min(w, h) - 80
                qr_img  = qr_img.resize((qr_size, qr_size))
                # Convert PIL → pygame surface
                raw = qr_img.tobytes()
                mode = qr_img.mode
                if mode == "1":
                    # Binary image — convert to RGBA
                    qr_img = qr_img.convert("RGB")
                    raw = qr_img.tobytes()
                    mode = "RGB"
                self._qr_surface = pygame.image.fromstring(raw, qr_img.size, mode)
            except Exception as e:
                logger.warning(f"QR generate failed: {e}")

        # Title
        title = f_large.render("SCAN TO CONNECT PHONE", True, C["accent"])
        screen.blit(title, ((w - title.get_width()) // 2, 15))

        if self._qr_surface:
            qx = (w - self._qr_surface.get_width()) // 2
            qy = 60
            screen.blit(self._qr_surface, (qx, qy))
            qy += self._qr_surface.get_height() + 10
        else:
            qy = h // 2

        # URL text (wraps if needed)
        url_surf = f_small.render(url, True, C["green"])
        screen.blit(url_surf, ((w - url_surf.get_width()) // 2, qy))

        hint = f_small.render("Tap to next screen", True, C["muted"])
        screen.blit(hint, ((w - hint.get_width()) // 2, h - 35))

    # ── Mode: Bluetooth ───────────────────────────────────────────────────────
    def _draw_bt(self, screen, w, h, nets, f_medium, f_small, f_tiny, scale):
        import pygame
        pad = int(8 * scale)

        bt_nets = [n for n in nets if n.get("type") in ("BT", "BLE")]
        bt_nets.sort(key=lambda n: n.get("rssi", -100), reverse=True)

        pygame.draw.rect(screen, C["bg2"], (0, 0, w, int(30 * scale)))
        title = f_medium.render(f"BLUETOOTH ({len(bt_nets)})", True, C["bt"])
        screen.blit(title, (pad, 5))

        row_h   = int(28 * scale)
        y_start = int(34 * scale)
        avail   = h - y_start - int(18 * scale)
        n_show  = avail // row_h
        start   = min(self._scroll, max(0, len(bt_nets) - n_show))
        visible = bt_nets[start:start + n_show]

        for i, dev in enumerate(visible):
            ny    = y_start + i * row_h
            is_ble = dev.get("type") == "BLE"
            color  = C["ble"] if is_ble else C["bt"]
            name   = (dev.get("ssid") or "<unknown>")[:26]
            rssi   = dev.get("rssi", -100)
            mfr    = dev.get("manufacturer", "")

            if i % 2 == 0:
                pygame.draw.rect(screen, C["bg3"], (0, ny, w, row_h - 1))

            typ_surf = f_tiny.render("BLE" if is_ble else "BT", True, color)
            screen.blit(typ_surf, (pad, ny + 8))
            screen.blit(f_small.render(name, True, C["text"]), (pad + 32, ny + 5))
            if mfr:
                screen.blit(f_tiny.render(mfr, True, C["muted"]), (pad + 32, ny + 18))
            screen.blit(f_tiny.render(f"{rssi}dBm", True, C["muted"]), (w - 60, ny + 8))
            _rssi_bars(rssi, w - pad - 28, ny + 8, screen)

    # ── Mode dots ─────────────────────────────────────────────────────────────
    def _draw_mode_dots(self, screen, w, h):
        import pygame
        dot_r = 4
        gap   = 12
        total = MODE_COUNT * (dot_r * 2 + gap) - gap
        x     = (w - total) // 2
        y     = h - dot_r - 6
        for i in range(MODE_COUNT):
            col = C["accent"] if i == self._mode else C["border"]
            pygame.draw.circle(screen, col, (x + i * (dot_r * 2 + gap) + dot_r, y), dot_r)


def create_display(config=None) -> PiDisplay | None:
    """
    Factory: create a PiDisplay if a display is available.
    Returns None if no display hardware found.
    """
    try:
        import pygame

        # Check if any display is available
        has_display = (
            bool(os.environ.get("DISPLAY")) or
            bool(os.environ.get("WAYLAND_DISPLAY")) or
            os.path.exists("/dev/fb0") or
            os.path.exists("/dev/fb1")
        )

        if not has_display:
            logger.info("No display detected — skipping pygame display")
            return None

        w = getattr(config, "DISPLAY_WIDTH",  None) if config else None
        h = getattr(config, "DISPLAY_HEIGHT", None) if config else None
        fs = getattr(config, "DISPLAY_FULLSCREEN", True) if config else True
        return PiDisplay(width=w, height=h, fullscreen=fs)

    except ImportError:
        logger.warning("pygame not installed — display disabled")
        return None
    except Exception as e:
        logger.warning(f"Display init skipped: {e}")
        return None
