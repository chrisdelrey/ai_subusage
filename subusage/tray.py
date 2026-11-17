"""System tray icon for Windows: standard library only (ctypes + the Win32 Shell_NotifyIcon API).

The icon is a small ring that fills with your tightest limit's percentage (green → amber → red), and
its tooltip lists each subscription. Left-click shows/hides the widget; right-click opens a menu.

The tray runs its own Win32 message loop on a background thread. It never touches Tk: it only puts
event names on a queue that the widget polls on the UI thread, and the widget hands it new state via
set_state(), which is applied on the tray thread.
"""
from __future__ import annotations

import ctypes
import math
import queue
import struct
import threading
from typing import Optional

from .core import IS_WIN, log

TRACK_RGB = (150, 156, 168)
MENU = ((1, "toggle"), (2, "refresh"), (3, "settings"), (4, "add"), (9, "quit"))


def available() -> bool:
    return IS_WIN


# --------------------------------------------------------------------------------------------------
# Icon drawing (pure Python, testable anywhere)
# --------------------------------------------------------------------------------------------------

def ring_pixels(n: int, pct: Optional[float], rgb: tuple[int, int, int]) -> bytes:
    """n×n BGRA pixels, bottom-up rows: a ring filled clockwise from 12 o'clock to pct%."""
    c = (n - 1) / 2
    r_out = n / 2 - 0.5
    r_in = r_out - max(2.2, n * 0.24)
    out = bytearray()
    for row in range(n - 1, -1, -1):
        for x in range(n):
            dx, dy = x - c, c - row
            d = math.hypot(dx, dy)
            a = min(max(0.0, min(1.0, r_out + 0.5 - d)), max(0.0, min(1.0, d - (r_in - 0.5))))
            if a <= 0:
                out += b"\0\0\0\0"
                continue
            ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360  # 0° = top, clockwise
            filled = pct is not None and pct > 0 and ang <= max(0.0, min(100.0, pct)) * 3.6
            r, g, b = rgb if filled else TRACK_RGB
            alpha = a if filled else a * 0.55
            out += bytes((b, g, r, int(alpha * 255)))
    return bytes(out)


def icon_image(n: int, pct: Optional[float], rgb: tuple[int, int, int]) -> bytes:
    """One icon image in resource format (BITMAPINFOHEADER + XOR pixels + AND mask)."""
    px = ring_pixels(n, pct, rgb)
    mask = b"\0" * (((n + 31) // 32) * 4 * n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(px) + len(mask), 0, 0, 0, 0)
    return header + px + mask


def ico_file(pct: Optional[float], rgb: tuple[int, int, int], sizes=(16, 24, 32, 48)) -> bytes:
    """A complete .ico (used for docs/tests; the tray builds icons in memory)."""
    images = [icon_image(n, pct, rgb) for n in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for n, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(img), offset + len(data))
        data += img
    return head + entries + data


def summary_tooltip(lines: list[str], limit: int = 127) -> str:
    """'SubUsage' plus one line per subscription; Windows caps tray tooltips at 127 characters."""
    text = "\n".join(["SubUsage"] + lines)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------------------------------

if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON)]

    def _sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)
        return fn

    _sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    _sig(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    _sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE,
         wintypes.LPVOID)
    _sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    _sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    _sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.PostQuitMessage, None, ctypes.c_int)
    _sig(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
    _sig(user32.CreatePopupMenu, wintypes.HMENU)
    _sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR)
    _sig(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.UINT)
    _sig(user32.TrackPopupMenu, ctypes.c_int, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
    _sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
    _sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    _sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _sig(user32.CreateIconFromResourceEx, wintypes.HICON, ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL,
         wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _sig(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    _sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

WM_CLOSE, WM_DESTROY, WM_NULL = 0x0010, 0x0002, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
WM_TRAY, WM_STATE = 0x8000 + 1, 0x8000 + 2
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
SM_CXSMICON = 49


class WinTray(threading.Thread):
    """The tray icon. Events ("toggle", "refresh", "settings", "add", "quit") go to `events`."""

    CLASS_NAME = "SubUsageTrayWindow"

    def __init__(self, events: "queue.Queue[str]"):
        super().__init__(daemon=True, name="SubUsageTray")
        self.events = events
        self.ready = threading.Event()
        self.ok = False                       # icon added to the tray
        self.hwnd = None
        self.widget_visible = True            # for the "Show/Hide widget" menu label
        self._state: tuple[str, Optional[float], tuple[int, int, int]] = ("SubUsage", None, TRACK_RGB)
        self._applied: Optional[tuple] = None
        self._hicon = None
        self._proc = None
        self._taskbar_created = 0

    # ----- called from the UI thread
    def set_state(self, tooltip: str, pct: Optional[float], rgb: tuple[int, int, int]) -> None:
        bucket = None if pct is None else int(round(pct / 2.0)) * 2  # redraw every 2%, not every refresh
        state = ((tooltip or "SubUsage")[:127], bucket, rgb)
        if state != self._state:
            self._state = state
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_STATE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ----- tray thread
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # never take the widget down with us
            log(f"tray: {e!r}")
        finally:
            self.ready.set()

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:  # already registered
            raise ctypes.WinError(ctypes.get_last_error())
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        # A real (never shown) top-level window, so we also receive TaskbarCreated if Explorer restarts.
        self.hwnd = user32.CreateWindowExW(0, self.CLASS_NAME, "SubUsage", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.ok = self._notify(NIM_ADD)
        if not self.ok:
            log("tray: Shell_NotifyIcon(NIM_ADD) failed")
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnregisterClassW(self.CLASS_NAME, hinst)

    def _make_icon(self):
        _tip, pct, rgb = self._state
        n = user32.GetSystemMetrics(SM_CXSMICON) or 16
        img = icon_image(n, pct, rgb)
        return user32.CreateIconFromResourceEx(img, len(img), True, 0x00030000, n, n, 0)

    def _notify(self, action: int) -> bool:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        if action != NIM_DELETE:
            new_icon = self._make_icon()
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = new_icon
            nid.szTip = self._state[0][:127]
        ok = bool(shell32.Shell_NotifyIconW(action, ctypes.byref(nid)))
        if action != NIM_DELETE:
            if self._hicon:
                user32.DestroyIcon(self._hicon)
            self._hicon = new_icon
            self._applied = self._state
        return ok

    def _menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        labels = {1: "Hide widget" if self.widget_visible else "Show widget", 2: "Refresh now",
                  3: "Settings…", 4: "Add subscription…", 9: "Quit SubUsage"}
        for cmd, _ in MENU:
            if cmd == 9:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, cmd, labels[cmd])
        user32.SetMenuDefaultItem(hmenu, 1, False)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # so the menu closes when you click elsewhere
        cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0,
                                    self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        event = dict(MENU).get(cmd)
        if event:
            self.events.put(event)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self.events.put("toggle")
                elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_STATE:
                if self.ok and self._state != self._applied:
                    self._notify(NIM_MODIFY)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:  # Explorer restarted
                self.ok = self._notify(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                if self.ok:
                    self._notify(NIM_DELETE)
                if self._hicon:
                    user32.DestroyIcon(self._hicon)
                    self._hicon = None
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.hwnd = None
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            log(f"tray wndproc: {e!r}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
