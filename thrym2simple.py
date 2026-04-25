"""
ragnarock_bot.py
================
Watches 4-6 configured pixels inside the Ragnarock game window.

Drum layout (left to right):
    [LeftMost] [LeftMid] [RightMid] [RightMost]
       Q          W/I       E/O          P

Left hammer:  Q, W, E   (cannot press two simultaneously)
Right hammer: I, O, P   (cannot press two simultaneously)

Drum-to-hammer assignment:
    LeftMost  -> Q only      (left hammer)
    LeftMid   -> W (left)  or I (right)
    RightMid  -> O (right) or E (left)
    RightMost -> P only      (right hammer)

Status pixels (5-6): yellow or cyan -> press spacebar (shield).

Requirements:
    pip install pywin32 Pillow pynput

Run:
    python ragnarock_bot.py [--config path/to/config.json] [--debug]
"""

import argparse
import ctypes
import json
import sys
import time
import colorsys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency guard
# ---------------------------------------------------------------------------
try:
    import win32gui, win32ui, win32con, win32api
except ImportError:
    sys.exit(
        "[ERROR] pywin32 not installed.\n"
        "Run:  pip install pywin32\n"
        "Then: python -m pywin32_postinstall -install  (run as admin if needed)"
    )

try:
    from PIL import Image
except ImportError:
    sys.exit("[ERROR] Pillow not installed.\nRun:  pip install Pillow")

try:
    from pynput.keyboard import Controller as KbController
except ImportError:
    sys.exit("[ERROR] pynput not installed.\nRun:  pip install pynput")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = Path(__file__).with_name("config.json")

def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[ERROR] Config file not found: {path}")
    with open(path, "r") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Chord resolver
# ---------------------------------------------------------------------------
# Drums indexed 0-3: LeftMost, LeftMid, RightMid, RightMost
#
# Left hammer pool  (in priority order): Q(d0), W(d1), E(d2)
# Right hammer pool (in priority order): P(d3), I(d1), O(d2)
#
# Resolution rules for valid chords (≤2 notes):
#   d0 (LeftMost)  -> must use left  hammer: Q
#   d3 (RightMost) -> must use right hammer: P
#   d1 (LeftMid)   -> prefer W (left), fall back to I (right)
#   d2 (RightMid)  -> prefer O (right), fall back to E (left)
#
# Invalid chords (3-4 notes) are "meme map" patterns — physically impossible
# in normal VR play. These are returned as an ORDERED sequence of single-note
# chords (left to right) to be fired as fast as possible in rapid succession.

def resolve_single(drum_index: int) -> str:
    """Return the preferred key for a single drum hit in isolation."""
    return ["q", "w", "o", "p"][drum_index]


def resolve_chord(active: tuple) -> list[str]:
    """
    active: (LeftMost, LeftMid, RightMid, RightMost) booleans.
    Returns a list of keys to press SIMULTANEOUSLY.
    Only called for valid (≤2 note) patterns.
    """
    d0, d1, d2, d3 = active
    left_busy  = False
    right_busy = False
    keys: list[str] = []

    if d0:
        keys.append("q")
        left_busy = True
    if d3:
        keys.append("p")
        right_busy = True

    if d1:
        if not left_busy:
            keys.append("w")
            left_busy = True
        elif not right_busy:
            keys.append("i")
            right_busy = True

    if d2:
        if not right_busy:
            keys.append("o")
            right_busy = True
        elif not left_busy:
            keys.append("e")
            left_busy = True

    return keys


def resolve_sequence(active: tuple) -> list[list[str]]:
    """
    For invalid (3-4 note) patterns, decompose into a left-to-right list of
    single-key presses to be fired in rapid succession.
    Returns a list of single-element key lists, e.g. [["q"], ["w"], ["o"], ["p"]].
    """
    return [[resolve_single(i)] for i, hit in enumerate(active) if hit]


def note_count(active: tuple) -> int:
    return sum(active)


# Validate resolver against every valid pattern listed in the spec
_SPEC_PATTERNS: dict[tuple, list[str]] = {
    (False, False, False, False): [],
    (True,  False, False, False): ["q"],
    (False, True,  False, False): ["w"],
    (False, False, True,  False): ["o"],
    (False, False, False, True):  ["p"],
    (True,  True,  False, False): ["q", "i"],
    (True,  False, True,  False): ["q", "o"],
    (True,  False, False, True):  ["q", "p"],
    (False, True,  False, True):  ["w", "p"],
    (False, True,  True,  False): ["w", "o"],
    (False, False, True,  True):  ["e", "p"],
}

def _validate_resolver():
    all_ok = True
    for pattern, expected in _SPEC_PATTERNS.items():
        got = resolve_chord(pattern)
        if sorted(got) != sorted(expected):
            print(
                f"[WARN] Chord mismatch for {pattern}: "
                f"expected {sorted(expected)}, got {sorted(got)}"
            )
            all_ok = False
    if all_ok:
        print("[INFO] Chord resolver validated against all spec patterns. ✓")

_validate_resolver()


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------
def find_window(title: str) -> int:
    title_lower = title.lower()
    found: list[int] = []
    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if title_lower in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)
    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def get_window_rect(hwnd: int) -> tuple:
    """Returns (left, top, right, bottom) of the client area in screen coords."""
    rect = win32gui.GetClientRect(hwnd)
    pt   = win32gui.ClientToScreen(hwnd, (0, 0))
    return pt[0], pt[1], pt[0] + rect[2], pt[1] + rect[3]


# ---------------------------------------------------------------------------
# Screen capture (one BitBlt per poll cycle for all pixels)
# ---------------------------------------------------------------------------
class WindowCapture:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd

    def get_pixels(
        self,
        coords: list[tuple],
        window_relative: bool,
        win_left: int,
        win_top: int,
    ) -> list[tuple]:
        if not coords:
            return []

        abs_coords = (
            [(win_left + x, win_top + y) for x, y in coords]
            if window_relative else list(coords)
        )

        xs = [c[0] for c in abs_coords]
        ys = [c[1] for c in abs_coords]
        bx, by = min(xs), min(ys)
        bw = max(xs) - bx + 1
        bh = max(ys) - by + 1

        hdesktop = win32gui.GetDesktopWindow()
        hdc_src  = win32gui.GetWindowDC(hdesktop)
        hdc_mem  = win32ui.CreateDCFromHandle(hdc_src)
        hdc_cpy  = hdc_mem.CreateCompatibleDC()
        bmp      = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc_mem, bw, bh)
        hdc_cpy.SelectObject(bmp)
        hdc_cpy.BitBlt((0, 0), (bw, bh), hdc_mem, (bx, by), win32con.SRCCOPY)

        raw    = bmp.GetBitmapBits(True)
        stride = bmp.GetInfo()["bmWidth"] * 4

        results = []
        for ax, ay in abs_coords:
            off = (ay - by) * stride + (ax - bx) * 4
            # BitBlt returns BGRA
            results.append((raw[off + 2], raw[off + 1], raw[off]))

        win32gui.DeleteObject(bmp.GetHandle())
        hdc_cpy.DeleteDC()
        hdc_mem.DeleteDC()
        win32gui.ReleaseDC(hdesktop, hdc_src)
        return results


# ---------------------------------------------------------------------------
# Colour classification
# ---------------------------------------------------------------------------
def is_green(rgb: tuple, tol: dict) -> bool:
    r, g, b = rgb
    return (
        g >= tol["green_g_min"]
        and r <= tol["green_rb_max"]
        and b <= tol["green_rb_max"]
    )


def classify_status(rgb: tuple, tol: dict) -> str:
    """Returns 'yellow', 'cyan', or 'none'."""
    r, g, b = rgb
    if r == 0 and g == 0 and b == 0:
        return "none"
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    h_deg = h * 360
    if s < tol["hsv_saturation_min"] or v < tol["hsv_value_min"]:
        return "none"
    if tol["yellow_hue_min"] <= h_deg <= tol["yellow_hue_max"]:
        return "yellow"
    if tol["cyan_hue_min"] <= h_deg <= tol["cyan_hue_max"]:
        return "cyan"
    return "none"


# ---------------------------------------------------------------------------
# Key presser
# ---------------------------------------------------------------------------
keyboard   = KbController()
_key_locks: dict[str, threading.Lock] = {}


def _get_lock(key: str) -> threading.Lock:
    if key not in _key_locks:
        _key_locks[key] = threading.Lock()
    return _key_locks[key]


def press_chord_async(keys: list[str], delay_ms: float, hold_ms: float):
    """Press all keys in a chord simultaneously in one background thread."""
    if not keys:
        return

    def _press():
        # time.sleep(delay_ms / 1000)
        for k in keys:
            keyboard.press(k)
        time.sleep(hold_ms / 1000)
        for k in reversed(keys):
            keyboard.release(k)

    threading.Thread(target=_press, daemon=True).start()


def press_key_async(key: str, hold_ms: float):
    """Press a single key in a background thread (used for spacebar)."""
    def _press():
        with _get_lock(key):
            keyboard.press(key)
            time.sleep(hold_ms / 1000)
            keyboard.release(key)
    threading.Thread(target=_press, daemon=True).start()


def press_sequence_async(sequence: list[list[str]], delay_ms: float, hold_ms: float, gap_ms: float):
    """Fire a list of chords in rapid left-to-right succession (meme map handling).

    Each chord in the sequence is pressed-then-released before the next starts,
    with a short gap_ms pause between them.
    """
    if not sequence:
        return

    def _fire():
        # time.sleep(delay_ms / 1000)
        for keys in sequence:
            for k in keys:
                keyboard.press(k)
            time.sleep(hold_ms / 1000)
            for k in reversed(keys):
                keyboard.release(k)
            time.sleep(gap_ms / 1000)

    threading.Thread(target=_fire, daemon=True).start()


# ---------------------------------------------------------------------------
# Pixel overlay window
# ---------------------------------------------------------------------------
# Creates a transparent, click-through, always-on-top window that draws a
# small crosshair marker (+) over each monitored pixel position.
#
# Marker colours:
#   Drum pixels (1-4) : white  by default, flashes green  when triggered
#   Status pixels (5-6): white by default, flashes yellow/cyan when active
#
# The overlay window is managed on its own thread and communicates with the
# main loop via thread-safe calls to update_markers().

# Transparent colour key — any pixel painted this colour becomes invisible.
# Chosen to be an unlikely on-screen colour.
_TRANSPARENT_COLOR = win32api.RGB(255, 0, 255)   # near-black, not pure black

# GDI colour helpers
_WHITE  = win32api.RGB(255, 255, 255)
_GREEN  = win32api.RGB(0,   255, 0  )
_YELLOW = win32api.RGB(255, 220, 0  )
_CYAN   = win32api.RGB(0,   220, 220)
_GRAY   = win32api.RGB(160, 160, 160)

# Marker dimensions (pixels)
_CROSS_HALF  = 8    # half-length of each arm of the + crosshair
_CROSS_GAP   = 5    # gap around the centre point (keeps centre clear)
_LABEL_OFFSET = 12  # vertical offset of the text label below the centre

_OVERLAY_CLASS = "RagnarockBotOverlay"


class PixelOverlay:
    """Transparent always-on-top GDI overlay showing pixel sample positions."""

    def __init__(self, pixel_coords: list[tuple[int, int]],
                 pixel_labels: list[str],
                 win_left: int, win_top: int,
                 window_relative: bool):
        """
        pixel_coords   : list of (x, y) as stored in config
        pixel_labels   : short label per pixel e.g. "D1", "D2", "S1"
        win_left/top   : current game window client origin (screen coords)
        window_relative: whether coords are relative to the game window
        """
        self._coords        = pixel_coords
        self._labels        = pixel_labels
        self._win_left      = win_left
        self._win_top       = win_top
        self._win_relative  = window_relative
        self._hwnd          = None
        self._lock          = threading.Lock()
        # Per-marker state: None = idle, "green"/"yellow"/"cyan" = flashing
        self._states: list[str | None] = [None] * len(pixel_coords)
        self._thread        = threading.Thread(target=self._message_loop,
                                               daemon=True, name="overlay")
        self._thread.start()

    # ── Public API (called from main loop thread) ────────────────────────────

    def update_position(self, win_left: int, win_top: int):
        """Call when the game window moves so the overlay follows it."""
        with self._lock:
            self._win_left = win_left
            self._win_top  = win_top
        if self._hwnd:
            self._reposition()

    def update_markers(self, states: list[str | None]):
        """
        Pass the current state for every pixel.
        states[i] is None (idle) or a colour name ("green"/"yellow"/"cyan").
        Triggers a repaint.
        """
        with self._lock:
            self._states = list(states)
        if self._hwnd:
            win32gui.InvalidateRect(self._hwnd, None, True)

    def destroy(self):
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _abs_coords(self) -> list[tuple[int, int]]:
        """Convert stored coords to absolute screen coords."""
        with self._lock:
            wl, wt = self._win_left, self._win_top
            rel     = self._win_relative
            coords  = list(self._coords)
        if rel:
            return [(wl + x, wt + y) for x, y in coords]
        return list(coords)

    def _reposition(self):
        """Move/resize the overlay to cover the entire screen."""
        if not self._hwnd:
            return
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        win32gui.SetWindowPos(
            self._hwnd, win32con.HWND_TOPMOST,
            0, 0, sw, sh,
            win32con.SWP_NOACTIVATE
        )
    
    def _on_erase_bkgnd(self, hwnd, msg, wparam, lparam):
        return 1
    
    def _message_loop(self):
        # Register window class
        hinstance = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance     = hinstance
        wc.lpszClassName = _OVERLAY_CLASS
        wc.style         = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor       = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        # wc.hbrBackground = win32gui.CreateSolidBrush(_TRANSPARENT_COLOR)
        wc.hbrBackground = win32gui.GetStockObject(win32con.NULL_BRUSH)
        wc.lpfnWndProc   = {
            win32con.WM_PAINT:   self._on_paint,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_ERASEBKGND:  lambda h, m, w, l: 1
        }
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # already registered from a previous run in the same process

        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED |
            win32con.WS_EX_TRANSPARENT |
            win32con.WS_EX_TOPMOST |
            win32con.WS_EX_NOACTIVATE,
            _OVERLAY_CLASS,
            "RagnarockBotOverlay",
            win32con.WS_POPUP,
            0, 0, sw, sh,
            0, 0, hinstance, None
        )

        # Make _TRANSPARENT_COLOR pixels fully transparent, everything else opaque
        win32gui.SetLayeredWindowAttributes(
            self._hwnd,
            _TRANSPARENT_COLOR,
            0,
            win32con.LWA_COLORKEY
        )

        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.UpdateWindow(self._hwnd)

        # Standard Win32 message pump
        win32gui.PumpMessages()

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        win32gui.PostQuitMessage(0)
        return 0

    def _on_paint(self, hwnd, msg, wparam, lparam):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            self._draw(hdc)
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0

    def _draw(self, hdc):
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        brush = win32gui.CreateSolidBrush(_TRANSPARENT_COLOR)
        # Clear the canvas with the transparency key
        brush = win32gui.CreateSolidBrush(_TRANSPARENT_COLOR)
        win32gui.FillRect(hdc, (0, 0, sw, sh), brush)
        win32gui.DeleteObject(brush)
        
        abs_coords = self._abs_coords()
        with self._lock:
            states = list(self._states)
            labels = list(self._labels)

        for i, ((ax, ay), state, label) in enumerate(
                zip(abs_coords, states, labels)):

            # Choose colour based on state
            if state == "green":
                colour = _GREEN
            elif state == "yellow":
                colour = _YELLOW
            elif state == "cyan":
                colour = _CYAN
            else:
                colour = _WHITE

            pen   = win32gui.CreatePen(win32con.PS_SOLID, 1, colour)
            old_pen   = win32gui.SelectObject(hdc, pen)

            # Draw + crosshair: two lines with a gap around centre
            # Horizontal arm
            win32gui.MoveToEx(hdc, ax - _CROSS_HALF, ay)
            win32gui.LineTo  (hdc, ax - _CROSS_GAP,  ay)
            win32gui.MoveToEx(hdc, ax + _CROSS_GAP,  ay)
            win32gui.LineTo  (hdc, ax + _CROSS_HALF, ay)
            # Vertical arm
            win32gui.MoveToEx(hdc, ax, ay - _CROSS_HALF)
            win32gui.LineTo  (hdc, ax, ay - _CROSS_GAP )
            win32gui.MoveToEx(hdc, ax, ay + _CROSS_GAP )
            win32gui.LineTo  (hdc, ax, ay + _CROSS_HALF)

            # Text label below the crosshair
            win32gui.SetTextColor(hdc, colour)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            lx = ax - 10
            ly = ay + _LABEL_OFFSET + 5
            win32gui.ExtTextOut(hdc, lx, ly, 2, None, label)
            win32gui.SelectObject(hdc, old_pen)
            win32gui.DeleteObject(pen)

# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------
def run(config: dict, debug: bool = False, verbose: bool = False,
        overlay: bool = False):
    title        = config["window_title"]
    pixels       = [tuple(p) for p in config["pixels"]]
    tol          = config["color_tolerances"]
    poll_ms      = config["poll_interval_ms"]
    hold_ms      = config["key_hold_ms"]
    cooldown_ms  = config["cooldown_ms"]
    delay_ms     = config["key_delay"]
    win_relative = config["window_relative"]
    # rapid_sequence_gap_ms is only used for meme-map (3-4 note) patterns
    # It's passed through `config` directly to press_sequence_async via the firing block

    if len(pixels) < 4:
        sys.exit("[ERROR] At least 4 pixel coordinates are required.")
    if len(pixels) > 6:
        sys.exit("[ERROR] At most 6 pixel coordinates are supported.")

    drum_pixels   = pixels[:4]   # LeftMost, LeftMid, RightMid, RightMost
    status_pixels = pixels[4:]   # optional shield pixels

    poll_s     = poll_ms / 1000
    cooldown_s = cooldown_ms / 1000

    last_chord_time  = 0.0
    last_shield_time = 0.0
    prev_active      = (False, False, False, False)
    prev_shield      = False

    print(f"[INFO] Looking for window: '{title}'")
    hwnd = 0
    while not hwnd:
        hwnd = find_window(title)
        if not hwnd:
            print("[INFO] Window not found, retrying in 2 s …")
            time.sleep(2)

    win_left_init, win_top_init, _, _ = get_window_rect(hwnd)

    if overlay:
        #print(f"[INFO] Found window HWND={hwnd}. Overlay mode — no keys will be pressed.")
        print(f"[INFO] Found window HWND={hwnd}. << Overlay mode >>")
        print(f"[INFO] Crosshairs shown at each pixel position. Press Ctrl+C to stop.\n")
    else:
        print(f"[INFO] Found window HWND={hwnd}. Bot running. Press Ctrl+C to stop.\n")

    cap = WindowCapture(hwnd)

    # Build short labels: D1-D4 for drum pixels, S1-S2 for status pixels
    pixel_labels = (
        [f"D{i+1}" for i in range(len(drum_pixels))] +
        [f"S{i+1}" for i in range(len(status_pixels))]
    )
    all_pixels = drum_pixels + status_pixels

    ov: PixelOverlay | None = None
    if overlay:
        ov = PixelOverlay(
            pixel_coords    = all_pixels,
            pixel_labels    = pixel_labels,
            win_left        = win_left_init,
            win_top         = win_top_init,
            window_relative = win_relative,
        )

    try:
        marker_expiry = [0.0] * 6  # Timestamps for when each marker should stop flashing
        VISUAL_HOLD_S = 0.15       # Hold the color for 150ms so you can actually see it
        while True:
            t0 = time.perf_counter()

            # Refresh window position each cycle (handles window moves)
            try:
                win_left, win_top, _, _ = get_window_rect(hwnd)
                if ov:
                    ov.update_position(win_left, win_top)
            except Exception:
                print("[WARN] Lost window, searching again …")
                hwnd = 0
                while not hwnd:
                    hwnd = find_window(title)
                    time.sleep(1)
                cap = WindowCapture(hwnd)
                continue

            # Capture all pixels in one shot
            try:
                rgbs = cap.get_pixels(
                    drum_pixels + status_pixels, win_relative, win_left, win_top
                )
            except Exception as e:
                if debug:
                    print(f"[DEBUG] Capture error: {e}")
                time.sleep(poll_s)
                continue
            
            drum_rgbs   = rgbs[:4]
            status_rgbs = rgbs[4:]
            
            # --- Verbose raw pixel readout (for threshold tuning) ---
            if verbose:
                drum_labels = ["LeftMost", "LeftMid ", "RightMid", "RightMost"]
                vparts = []
                for lbl, rgb in zip(drum_labels, drum_rgbs):
                    r, g, b = rgb
                    flag = " [32m✓GREEN[0m" if is_green(rgb, tol) else ""
                    vparts.append(f"  {lbl}: R={r:3d} G={g:3d} B={b:3d}{flag}")
                for idx, rgb in enumerate(status_rgbs):
                    r, g, b = rgb
                    st = classify_status(rgb, tol)
                    colour = (
                        "[33myellow[0m" if st == "yellow"
                        else "[36mcyan[0m" if st == "cyan"
                        else "none"
                    )
                    vparts.append(f"  Status{idx+1} : R={r:3d} G={g:3d} B={b:3d}  [{colour}]")
                print("[2J[H" + "\n".join(vparts), flush=True)
            
            now = time.perf_counter()

            # --- Drum notes ---
            active = tuple(is_green(rgb, tol) for rgb in drum_rgbs)

            # Fire on rising edge (new pattern) with cooldown
            if active != prev_active and any(active):
                if now - last_chord_time >= cooldown_s:
                    n = note_count(active)
                    if n <= 2:
                        # Normal valid chord — press all keys simultaneously
                        keys = resolve_chord(active)
                        if keys:
                            press_chord_async(keys, delay_ms, hold_ms)
                            last_chord_time = now
                            if debug:
                                print(f"[HIT]  {active} -> {keys}")
                    else:
                        # Meme map: 3-4 notes — fire left-to-right in rapid succession
                        sequence = resolve_sequence(active)
                        press_sequence_async(sequence, delay_ms, hold_ms, config["rapid_sequence_gap_ms"])
                        last_chord_time = now
                        if debug:
                            flat = [k for chord in sequence for k in chord]
                            print(f"[MEME] {active} ({n} notes) -> rapid sequence {flat}")

            prev_active = active

            # --- Shield ---
            if status_rgbs:
                statuses = [classify_status(rgb, tol) for rgb in status_rgbs]
                shield_now = any(s in ("yellow", "cyan") for s in statuses)

                # Press spacebar on rising edge
                if shield_now and not prev_shield:
                    if now - last_shield_time >= cooldown_s:
                        press_key_async(" ", hold_ms)
                        last_shield_time = now
                        if debug:
                            print(f"[SHIELD] spacebar -> {statuses}")

                prev_shield = shield_now

                # Live status line
                c = {"yellow": "\033[33myellow\033[0m", "cyan": "\033[36mcyan\033[0m", "none": "none"}
                parts = [f"pixel{5+i}={c[s]}" for i, s in enumerate(statuses)]
                print(f"\r[STATUS] {' | '.join(parts)}   ", end="", flush=True)

            # --- Update overlay marker states ---
            # --- Update overlay marker states with persistence ---
            if ov:
                now = time.perf_counter()
                
                # Update expiry times for currently active pixels
                for i, is_active in enumerate(active):
                    if is_active:
                        marker_expiry[i] = now + VISUAL_HOLD_S
                
                # Check status pixels (5-6)
                if status_rgbs:
                    for i, rgb in enumerate(status_rgbs):
                        st = classify_status(rgb, tol)
                        if st != "none":
                            marker_expiry[4 + i] = now + VISUAL_HOLD_S

                # Build the state list based on what is CURRENTLY "hot"
                current_states = []
                for i in range(len(all_pixels)):
                    if now < marker_expiry[i]:
                        # Determine which color to show
                        if i < 4:
                            current_states.append("green")
                        else:
                            # Re-run classification or just store the color type
                            st = classify_status(rgbs[i], tol)
                            current_states.append(st if st != "none" else "green")
                    else:
                        current_states.append(None)
                
                ov.update_markers(current_states)

            # Pace the loop
            time.sleep(max(0.0, poll_s - (time.perf_counter() - t0)))

    except KeyboardInterrupt:
        if ov:
            ov.destroy()
        print("\n[INFO] Bot stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Ragnarock rhythm-game bot")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to config.json (default: ./config.json)")
    parser.add_argument("--debug", action="store_true",
                        help="Print every key press and status event")
    parser.add_argument("--overlay", action="store_true",
                        help="Show pixel crosshair overlay (no keypresses in overlay-only mode)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print raw RGB values every poll cycle for threshold tuning")
    args = parser.parse_args()
    run(load_config(args.config), debug=args.debug, verbose=args.verbose, overlay=args.overlay)

if __name__ == "__main__":
    main()