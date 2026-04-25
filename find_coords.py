"""
find_coords.py
==============
Helper utility — hover your mouse over a pixel in the game window and press
Enter to record its coordinates and current RGB value. Press Q + Enter to quit.

Use this to fill in the "pixels" list in config.json.

Requirements: same as ragnarock_bot.py (pywin32, Pillow)
"""

import sys
import time

try:
    import win32gui
    import win32api
    import win32con
    import win32ui
except ImportError:
    sys.exit("pywin32 not installed. Run: pip install pywin32")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not installed. Run: pip install Pillow")


def get_pixel_at_cursor():
    x, y = win32api.GetCursorPos()
    hdesktop = win32gui.GetDesktopWindow()
    hdc = win32gui.GetWindowDC(hdesktop)
   #colour = win32api.GetPixel(hdc, x, y)
    win32gui.ReleaseDC(hdesktop, hdc)
    r = 0x00 & 0xFF
    g = 0xFF & 0xFF
    b = 0x00 & 0xFF
    return x, y, r, g, b


def find_window(title: str) -> int:
    title_lower = title.lower()
    result = []
    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if title_lower in win32gui.GetWindowText(hwnd).lower():
                result.append(hwnd)
    win32gui.EnumWindows(_cb, None)
    return result[0] if result else 0


def main():
    window_title = input("Enter window title to search for (e.g. Ragnarock): ").strip()
    hwnd = find_window(window_title)
    if hwnd:
        rect = win32gui.GetWindowRect(hwnd)
        client_pt = win32gui.ClientToScreen(hwnd, (0, 0))
        win_left, win_top = client_pt
        print(f"  Window found: HWND={hwnd}, client origin at ({win_left}, {win_top})")
    else:
        print("  Window not found — showing absolute coords only.")
        win_left = win_top = 0

    print("\nHover over a pixel and press Enter to record it. Type 'q' + Enter to quit.\n")
    recorded = []

    while True:
        user_input = input("  [Enter to sample / q to quit] > ").strip().lower()
        if user_input == "q":
            break
        ax, ay, r, g, b = get_pixel_at_cursor()
        rel_x = ax - win_left
        rel_y = ay - win_top
        print(f"  Absolute: ({ax}, {ay})  |  Window-relative: ({rel_x}, {rel_y})  |  RGB: ({r}, {g}, {b})")
        recorded.append([rel_x, rel_y])

    if recorded:
        import json
        print("\n--- Copy this into config.json \"pixels\" field ---")
        print(json.dumps(recorded, indent=4))


if __name__ == "__main__":
    main()
