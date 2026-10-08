"""Single instance guard."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

_MUTEX_HANDLE = None
MUTEX_NAME = "Local\\UsagePeek_Mutex_vlx0"
WINDOW_TITLE = "UsagePeek - darkshade"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_RESTORE = 9


def claim_or_exit() -> None:
    global _MUTEX_HANDLE
    kernel32.SetLastError(0)
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() != 183:
        return
    hwnd = _find_window()
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        sys.exit(0)


def _find_window() -> int:
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        return int(hwnd)
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _enum(h, _l):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, buf, 512)
        if buf.value == WINDOW_TITLE:
            found.value = h
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value or 0)
