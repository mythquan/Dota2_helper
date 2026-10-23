"""Interactive-desktop Ctrl+F11 launcher for the local Dota coach page.

Run this in the signed-in user's desktop session. The HTTP/GSI server may run
in a separate desktop, where RegisterHotKey cannot see Dota's keyboard input.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
from pathlib import Path


TITLE = "Dota 2 新手节奏教练"
HOTKEY_ID = 0xD072
VK_F11 = 0x7A
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
LOG = Path(__file__).with_name("hotkey.log")


def log(message: str) -> None:
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def find_coach_window(user32: ctypes.WinDLL) -> int | None:
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    target: list[int | None] = [None]

    @callback_type
    def inspect(hwnd: int, _: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if TITLE in title.value:
                target[0] = hwnd
                return False
        return True

    user32.EnumWindows(inspect, 0)
    return target[0]


def main() -> None:
    user32 = ctypes.windll.user32
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    log("starting; coach_window_found=" + str(find_coach_window(user32) is not None))
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, VK_F11):
        log("Ctrl+F11 registration failed")
        return
    log("Ctrl+F11 registered")
    message = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message != 0x0312 or message.wParam != HOTKEY_ID:
                continue
            hwnd = find_coach_window(user32)
            if hwnd is None:
                log("Ctrl+F11 received; coach window not found")
                continue
            was_maximized = bool(user32.IsZoomed(hwnd))
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE only when minimized
            activated = bool(user32.SetForegroundWindow(hwnd))
            log(f"Ctrl+F11 received; activated={activated}; maximized_before={was_maximized}; "
                f"maximized_after={bool(user32.IsZoomed(hwnd))}")
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


if __name__ == "__main__":
    main()
