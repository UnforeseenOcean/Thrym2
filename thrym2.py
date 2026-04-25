"""
ragnarock_bot.py
================
Watches 4-6 configured pixels inside the Ragnarock game window and handles
automatic lobby ready-up via template matching.

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

Lobby state machine (runs on a background thread):
    IDLE            -> Not Ready button found -> press Space -> WAITING_FOR_READY
    WAITING_FOR_READY -> Ready button found (or Not Ready gone) -> PLAYING
    PLAYING         -> 15 s silence after last note -> SONG_ENDED
    SONG_ENDED      -> Vote button found -> WAITING_FOR_LOBBY
                    -> Vote button not found within 5 s -> back to PLAYING
    WAITING_FOR_LOBBY -> Not Ready button found -> press Space -> WAITING_FOR_READY

Requirements:
    pip install pywin32 Pillow pynput opencv-python

Run:
    python ragnarock_bot.py [--config path/to/config.json] [--debug] [--verbose] [--overlay]
"""

import argparse
import ctypes
import json
import sys
import time
import colorsys
import threading
from enum import Enum, auto
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

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit("[ERROR] opencv-python not installed.\nRun:  pip install opencv-python")


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
    (False, True,  True,  False): ["w", "o"],
    (False, False, True,  True):  ["e", "p"],
    (True,  False, True,  False): ["q", "o"],
    (False, True,  False, True):  ["w", "p"],
    (True,  False, False, True):  ["q", "p"],
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
# Screen capture — pixel sampling (one BitBlt per poll cycle)
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

    def capture_region_cv2(
        self,
        win_left: int, win_top: int,
        win_right: int, win_bottom: int,
        region_fraction_top: float = 0.5,
    ) -> "np.ndarray":
        """
        Capture the lower portion of the game window and return a BGR numpy
        array suitable for cv2 template matching.

        region_fraction_top: how far down the window to start (0.5 = lower half).
        """
        win_w = win_right  - win_left
        win_h = win_bottom - win_top
        strip_top = int(win_h * region_fraction_top)

        sx = win_left
        sy = win_top + strip_top
        sw = win_w
        sh = win_h - strip_top

        if sw <= 0 or sh <= 0:
            return np.zeros((1, 1, 3), dtype=np.uint8)

        hdesktop = win32gui.GetDesktopWindow()
        hdc_src  = win32gui.GetWindowDC(hdesktop)
        hdc_mem  = win32ui.CreateDCFromHandle(hdc_src)
        hdc_cpy  = hdc_mem.CreateCompatibleDC()
        bmp      = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc_mem, sw, sh)
        hdc_cpy.SelectObject(bmp)
        hdc_cpy.BitBlt((0, 0), (sw, sh), hdc_mem, (sx, sy), win32con.SRCCOPY)

        raw    = bmp.GetBitmapBits(True)
        stride = bmp.GetInfo()["bmWidth"] * 4

        win32gui.DeleteObject(bmp.GetHandle())
        hdc_cpy.DeleteDC()
        hdc_mem.DeleteDC()
        win32gui.ReleaseDC(hdesktop, hdc_src)

        # raw is BGRA, reshape to (h, w, 4) then drop alpha -> BGR
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(sh, sw, 4)
        return arr[:, :, :3].copy()


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
# Template matcher
# ---------------------------------------------------------------------------
class TemplateMatcher:
    """Loads button reference images and searches for them via cv2 matchTemplate."""

    def __init__(self, assets_dir: Path):
        self._templates: dict[str, np.ndarray] = {}
        names = {
            "not_ready": assets_dir / "btn_not_ready.png",
            "ready":     assets_dir / "btn_ready.png",
            "vote":      assets_dir / "btn_vote.png",
        }
        missing = []
        for name, path in names.items():
            if not path.exists():
                missing.append(str(path))
                continue
            img = cv2.imread(str(path))
            if img is None:
                missing.append(str(path))
                continue
            self._templates[name] = img

        if missing:
            print(
                f"[WARN] Lobby auto-ready disabled — missing template image(s):\n"
                + "\n".join(f"       {p}" for p in missing)
                + "\n       Place them in the assets/ folder next to ragnarock_bot.py."
            )

    @property
    def ready(self) -> bool:
        """True if all three templates loaded successfully."""
        return len(self._templates) == 3

    def find(self, name: str, screen: "np.ndarray", threshold: float) -> bool:
        """
        Return True if the named button template is found in `screen` at or
        above `threshold` confidence (0.0–1.0).
        """
        tmpl = self._templates.get(name)
        if tmpl is None or screen is None:
            return False
        th, tw = tmpl.shape[:2]
        sh, sw = screen.shape[:2]
        if sh < th or sw < tw:
            return False
        result = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        return max_val >= threshold


# ---------------------------------------------------------------------------
# Lobby state machine
# ---------------------------------------------------------------------------
class LobbyState(Enum):
    IDLE              = auto()   # In lobby, Not Ready button expected
    WAITING_FOR_READY = auto()   # Space pressed, waiting to confirm ready
    PLAYING           = auto()   # Song in progress
    SONG_ENDED        = auto()   # Silence detected, checking for vote screen
    WAITING_FOR_LOBBY = auto()   # Vote screen seen, waiting for lobby to return


class LobbyManager:
    """
    Runs on a background thread. Polls for UI buttons at a slow cadence
    and drives the lobby ready-up state machine.

    The main loop notifies it of note events via note_fired().
    The main loop provides fresh screen captures via set_last_screen().
    """

    # How long after the last note before we consider the song over
    SONG_END_SILENCE_S = 15.0
    # How long to wait for the vote screen after silence before giving up
    VOTE_CHECK_WINDOW_S = 5.0
    # How long to wait for the Ready button to confirm after pressing Space
    READY_CONFIRM_TIMEOUT_S = 5.0
    # Poll interval for the lobby checker (ms) — much slower than note loop
    LOBBY_POLL_MS = 500

    def __init__(self, matcher: TemplateMatcher, threshold: float, debug: bool = False):
        self._matcher   = matcher
        self._threshold = threshold
        self._debug     = debug

        self._state            = LobbyState.IDLE
        self._last_note_time   = 0.0          # time of last note hit
        self._song_started     = False        # True once first note was ever hit
        self._state_entered_at = time.perf_counter()

        self._screen_lock = threading.Lock()
        self._last_screen: "np.ndarray | None" = None

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="lobby"
        )
        self._thread.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def note_fired(self):
        """Called by the main loop every time a drum note is triggered."""
        self._last_note_time = time.perf_counter()
        if not self._song_started:
            self._song_started = True
        # If we thought the song ended but notes came back, return to PLAYING
        if self._state in (LobbyState.SONG_ENDED, LobbyState.WAITING_FOR_LOBBY):
            self._transition(LobbyState.PLAYING)

    def set_last_screen(self, screen: "np.ndarray"):
        """Called by the main loop with a fresh lower-half screen capture."""
        with self._screen_lock:
            self._last_screen = screen

    def stop(self):
        self._stop_event.set()

    @property
    def state(self) -> LobbyState:
        return self._state

    # ── Internal ─────────────────────────────────────────────────────────────

    def _transition(self, new_state: LobbyState):
        if self._state != new_state:
            if self._debug:
                print(f"\n[LOBBY] {self._state.name} -> {new_state.name}")
            self._state = new_state
            self._state_entered_at = time.perf_counter()

    def _screen(self) -> "np.ndarray | None":
        with self._screen_lock:
            return self._last_screen

    def _find(self, name: str) -> bool:
        s = self._screen()
        if s is None:
            return False
        return self._matcher.find(name, s, self._threshold)

    def _loop(self):
        poll_s = self.LOBBY_POLL_MS / 1000
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                if self._debug:
                    print(f"[LOBBY] tick error: {e}")
            time.sleep(poll_s)

    def _tick(self):
        now   = time.perf_counter()
        state = self._state
        age   = now - self._state_entered_at  # time spent in current state

        if state == LobbyState.IDLE:
            # Looking for Not Ready button — means we're in the lobby unready
            if self._find("not_ready"):
                if self._debug:
                    print("\n[LOBBY] Not Ready button detected — pressing Space")
                press_key_async(" ", 40)
                self._transition(LobbyState.WAITING_FOR_READY)

        elif state == LobbyState.WAITING_FOR_READY:
            # Confirm ready: look for Ready button or absence of Not Ready
            if self._find("ready") or not self._find("not_ready"):
                if self._debug:
                    print("\n[LOBBY] Ready confirmed")
                self._song_started = False
                self._last_note_time = now
                self._transition(LobbyState.PLAYING)
            elif age > self.READY_CONFIRM_TIMEOUT_S:
                # Timed out — go back to IDLE to try again
                if self._debug:
                    print("\n[LOBBY] Ready confirm timed out — retrying")
                self._transition(LobbyState.IDLE)

        elif state == LobbyState.PLAYING:
            # Wait for 15 s of silence after the song ends
            # Only start counting once at least one note has been hit
            if self._song_started:
                silence = now - self._last_note_time
                if silence >= self.SONG_END_SILENCE_S:
                    if self._debug:
                        print(f"\n[LOBBY] {self.SONG_END_SILENCE_S:.0f}s silence — checking for vote screen")
                    self._transition(LobbyState.SONG_ENDED)

        elif state == LobbyState.SONG_ENDED:
            if self._find("vote"):
                if self._debug:
                    print("\n[LOBBY] Vote screen detected — waiting for lobby")
                self._transition(LobbyState.WAITING_FOR_LOBBY)
            elif age > self.VOTE_CHECK_WINDOW_S:
                # No vote screen found — probably a false alarm, back to PLAYING
                if self._debug:
                    print("\n[LOBBY] No vote screen found — resuming PLAYING")
                self._transition(LobbyState.PLAYING)

        elif state == LobbyState.WAITING_FOR_LOBBY:
            # Song chosen, waiting for lobby screen to come back
            if self._find("not_ready"):
                if self._debug:
                    print("\n[LOBBY] Lobby returned — pressing Space to ready up")
                press_key_async(" ", 40)
                self._transition(LobbyState.WAITING_FOR_READY)


# ---------------------------------------------------------------------------
# Key presser
# ---------------------------------------------------------------------------
keyboard   = KbController()
_key_locks: dict[str, threading.Lock] = {}


def _get_lock(key: str) -> threading.Lock:
    if key not in _key_locks:
        _key_locks[key] = threading.Lock()
    return _key_locks[key]


def press_chord_async(keys: list[str], hold_ms: float):
    """Press all keys in a chord simultaneously in one background thread."""
    if not keys:
        return

    def _press():
        for k in keys:
            keyboard.press(k)
        time.sleep(hold_ms / 1000)
        for k in reversed(keys):
            keyboard.release(k)

    threading.Thread(target=_press, daemon=True).start()


def press_key_async(key: str, hold_ms: float):
    """Press a single key in a background thread."""
    def _press():
        with _get_lock(key):
            keyboard.press(key)
            time.sleep(hold_ms / 1000)
            keyboard.release(key)
    threading.Thread(target=_press, daemon=True).start()


def press_sequence_async(sequence: list[list[str]], hold_ms: float, gap_ms: float):
    """Fire a list of chords in rapid left-to-right succession (meme map handling)."""
    if not sequence:
        return

    def _fire():
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
    win_relative = config["window_relative"]

    # Lobby settings
    assets_dir      = Path(__file__).with_name(config.get("assets_dir", "assets"))
    tmpl_threshold  = config.get("lobby", {}).get("match_threshold", 0.85)
    lobby_enabled   = config.get("lobby", {}).get("enabled", True)

    if len(pixels) < 4:
        sys.exit("[ERROR] At least 4 pixel coordinates are required.")
    if len(pixels) > 6:
        sys.exit("[ERROR] At most 6 pixel coordinates are supported.")

    drum_pixels   = pixels[:4]
    status_pixels = pixels[4:]

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

    win_left_init, win_top_init, win_right_init, win_bottom_init = get_window_rect(hwnd)

    if overlay:
        print(f"[INFO] Found window HWND={hwnd}. << Overlay mode >>")
        print(f"[INFO] Crosshairs shown at each pixel position. Press Ctrl+C to stop.\n")
    else:
        print(f"[INFO] Found window HWND={hwnd}. Bot running. Press Ctrl+C to stop.\n")

    cap = WindowCapture(hwnd)

    # Overlay setup
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

    # Lobby manager setup
    lobby: LobbyManager | None = None
    if lobby_enabled and True:
        matcher = TemplateMatcher(assets_dir)
        if matcher.ready:
            lobby = LobbyManager(matcher, tmpl_threshold, debug=debug)
            print(f"[INFO] Lobby auto-ready enabled (threshold={tmpl_threshold}).")
        else:
            print("[INFO] Lobby auto-ready disabled (missing templates).")

    # Lobby screen capture cadence — run every N main loop iterations
    # 500 ms / 16 ms ≈ every 31 iterations
    _lobby_every_n = max(1, round(LobbyManager.LOBBY_POLL_MS / poll_ms))
    _lobby_tick    = 0

    win_right  = win_right_init
    win_bottom = win_bottom_init

    try:
        marker_expiry = [0.0] * 6  # Timestamps for when each marker should stop flashing
        VISUAL_HOLD_S = 0.15       # Hold the color for 150ms so you can actually see it
        while True:
            t0 = time.perf_counter()

            # Refresh window position
            try:
                win_left, win_top, win_right, win_bottom = get_window_rect(hwnd)
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

            # Feed screen capture to lobby manager at reduced cadence
            if lobby:
                _lobby_tick += 1
                if _lobby_tick >= _lobby_every_n:
                    _lobby_tick = 0
                    try:
                        screen = cap.capture_region_cv2(
                            win_left, win_top, win_right, win_bottom,
                            region_fraction_top=config.get("lobby", {}).get(
                                "search_region_top", 0.5)
                        )
                        lobby.set_last_screen(screen)
                    except Exception as e:
                        if debug:
                            print(f"[DEBUG] Lobby screen capture error: {e}")

            # Capture note pixels
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
            now = time.perf_counter()

            # --- Verbose raw pixel readout ---
            if verbose:
                drum_labels = ["LeftMost", "LeftMid ", "RightMid", "RightMost"]
                vparts = []
                for lbl, rgb in zip(drum_labels, drum_rgbs):
                    r, g, b = rgb
                    flag = " \033[32m✓GREEN\033[0m" if is_green(rgb, tol) else ""
                    vparts.append(f"  {lbl}: R={r:3d} G={g:3d} B={b:3d}{flag}")
                for idx, rgb in enumerate(status_rgbs):
                    r, g, b = rgb
                    st = classify_status(rgb, tol)
                    colour = (
                        "\033[33myellow\033[0m" if st == "yellow" else
                        "\033[36mcyan\033[0m"   if st == "cyan"   else
                        "none"
                    )
                    vparts.append(f"  Status{idx+1} : R={r:3d} G={g:3d} B={b:3d}  [{colour}]")
                if lobby:
                    vparts.append(f"  Lobby state : {lobby.state.name}")
                print("\033[2J\033[H" + "\n".join(vparts), flush=True)

            # --- Drum notes ---
            active = tuple(is_green(rgb, tol) for rgb in drum_rgbs)

            if True:
                if active != prev_active and any(active):
                    if now - last_chord_time >= cooldown_s:
                        n = note_count(active)
                        if n <= 2:
                            keys = resolve_chord(active)
                            if keys:
                                press_chord_async(keys, hold_ms)
                                last_chord_time = now
                                if lobby:
                                    lobby.note_fired()
                                if debug:
                                    print(f"[HIT]  {active} -> {keys}")
                        else:
                            sequence = resolve_sequence(active)
                            press_sequence_async(
                                sequence, hold_ms,
                                config["rapid_sequence_gap_ms"]
                            )
                            last_chord_time = now
                            if lobby:
                                lobby.note_fired()
                            if debug:
                                flat = [k for chord in sequence for k in chord]
                                print(f"[MEME] {active} ({n} notes) -> rapid sequence {flat}")

                prev_active = active

            # --- Shield ---
            if status_rgbs:
                statuses = [classify_status(rgb, tol) for rgb in status_rgbs]
                shield_now = any(s in ("yellow", "cyan") for s in statuses)

                if True:
                    if shield_now and not prev_shield:
                        if now - last_shield_time >= cooldown_s:
                            press_key_async(" ", hold_ms)
                            last_shield_time = now
                            if debug:
                                print(f"[SHIELD] spacebar -> {statuses}")

                prev_shield = shield_now

                if not verbose:
                    c = {
                        "yellow": "\033[33myellow\033[0m",
                        "cyan":   "\033[36mcyan\033[0m",
                        "none":   "none",
                    }
                    parts = [f"pixel{5+i}={c[s]}" for i, s in enumerate(statuses)]
                    lobby_str = f" | lobby={lobby.state.name}" if lobby else ""
                    print(f"\r[STATUS] {' | '.join(parts)}{lobby_str}   ",
                          end="", flush=True)

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
        if lobby:
            lobby.stop()
        print("\n[INFO] Bot stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Ragnarock rhythm-game bot")
    parser.add_argument("--config",  type=Path, default=DEFAULT_CONFIG,
                        help="Path to config.json (default: ./config.json)")
    parser.add_argument("--debug",   action="store_true",
                        help="Print every key press, state transition, and status event")
    parser.add_argument("--overlay", action="store_true",
                        help="Show pixel crosshair overlay; no keypresses are sent")
    parser.add_argument("--verbose", action="store_true",
                        help="Print raw RGB values every poll cycle for threshold tuning")
    args = parser.parse_args()
    run(load_config(args.config),
        debug=args.debug, verbose=args.verbose, overlay=args.overlay)

if __name__ == "__main__":
    main()