"""System tray / menu bar icon, standard library only, on all three platforms.

The icon is a small ring that fills with your tightest limit's percentage (green → amber → red), and
its tooltip lists each subscription. Its menu: show/hide the widget, pause/resume updates, refresh,
settings, add a subscription, start at login, quit.

- Windows: ctypes + Shell_NotifyIcon, with its own Win32 message loop on a background thread
  (left-click shows/hides, right-click opens the menu).
- macOS: ctypes + the Objective-C runtime (NSStatusItem in the menu bar). AppKit objects must live on
  the main thread, which is Tk's, so this one is created and updated from the UI thread.
- Linux: a StatusNotifierItem over D-Bus (see sni.py), on a background thread. Needs a panel that
  supports it (KDE, Ubuntu's GNOME, Cinnamon, XFCE, waybar...).

No tray ever touches Tk: they put event names on a queue that the widget polls on the UI thread.
Every backend has the same interface: start(), ready / ok, set_state(), set_menu_state(), stop().
"""
from __future__ import annotations

import ctypes
import math
import queue
import struct
import threading
from typing import Any, Optional

from .core import IS_LINUX, IS_MAC, IS_WIN, log

TRACK_RGB = (150, 156, 168)
MENU = ((1, "toggle"), (2, "pause"), (3, "refresh"), (4, "settings"), (5, "add"), (6, "startup"), (9, "quit"))


def menu_labels(visible: bool, paused: bool) -> dict[int, str]:
    return {1: "Hide widget" if visible else "Show widget", 2: "Resume updates" if paused else "Pause updates",
            3: "Refresh now", 4: "Settings…", 5: "Add subscription…", 6: "Start at login", 9: "Quit SubUsage"}


def available() -> bool:
    """Whether a tray icon can be shown here. On Linux this needs a StatusNotifier host (checked live)."""
    if IS_WIN or IS_MAC:
        return True
    if IS_LINUX:
        from . import sni
        return sni.watcher_present()
    return False


def unavailable_reason() -> str:
    if IS_LINUX:
        return ("No tray host found. KDE, Cinnamon, XFCE and Ubuntu show tray icons; on stock GNOME install the "
                "'AppIndicator and KStatusNotifierItem Support' extension.")
    return "Not supported on this platform"


def create(events: "queue.Queue[str]"):
    """The tray backend for this platform (call start() on it)."""
    if IS_WIN:
        return WinTray(events)
    if IS_MAC:
        return MacTray(events)
    from .sni import SNITray
    return SNITray(events)


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


def png_bytes(n: int, pct: Optional[float], rgb: tuple[int, int, int]) -> bytes:
    """The ring as a PNG (RGBA), for the macOS menu bar."""
    import zlib
    bgra = ring_pixels(n, pct, rgb)
    raw = bytearray()
    for row in range(n - 1, -1, -1):  # ring_pixels is bottom-up
        raw.append(0)
        line = bgra[row * n * 4:(row + 1) * n * 4]
        for i in range(0, len(line), 4):
            b, g, r, a = line[i:i + 4]
            raw += bytes((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


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
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
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
        self.paused = False
        self.startup = False
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

    def set_menu_state(self, visible: bool, paused: bool, startup: bool) -> None:
        self.widget_visible, self.paused, self.startup = visible, paused, startup  # read when the menu opens

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
        labels = menu_labels(self.widget_visible, self.paused)
        for cmd, _ in MENU:
            if cmd in (4, 9):
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            flags = MF_STRING
            if cmd == 6 and self.startup:
                flags |= MF_CHECKED
            if cmd == 3 and self.paused:
                flags |= MF_GRAYED
            user32.AppendMenuW(hmenu, flags, cmd, labels[cmd])
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


# --------------------------------------------------------------------------------------------------
# macOS: NSStatusItem through the Objective-C runtime
# --------------------------------------------------------------------------------------------------

class _ObjC:
    """Just enough of the Objective-C runtime to drive AppKit from ctypes."""

    def __init__(self):
        import ctypes.util
        self.lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        v = ctypes.c_void_p
        self.lib.objc_getClass.restype, self.lib.objc_getClass.argtypes = v, [ctypes.c_char_p]
        self.lib.sel_registerName.restype, self.lib.sel_registerName.argtypes = v, [ctypes.c_char_p]
        self.lib.objc_allocateClassPair.restype = v
        self.lib.objc_allocateClassPair.argtypes = [v, ctypes.c_char_p, ctypes.c_size_t]
        self.lib.objc_registerClassPair.restype, self.lib.objc_registerClassPair.argtypes = None, [v]
        self.lib.class_addMethod.restype = ctypes.c_bool
        self.lib.class_addMethod.argtypes = [v, v, v, ctypes.c_char_p]
        self._fns: dict = {}

    def cls(self, name: str):
        return self.lib.objc_getClass(name.encode())

    def sel(self, name: str):
        return self.lib.sel_registerName(name.encode())

    def send(self, obj, sel: str, *args, restype=ctypes.c_void_p, argtypes=()):
        """objc_msgSend with an explicit signature (required on arm64)."""
        key = (restype, tuple(argtypes))
        fn = self._fns.get(key)
        if fn is None:
            proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
            fn = self._fns[key] = proto(("objc_msgSend", self.lib))
        return fn(obj, self.sel(sel), *args)

    def string(self, text: str):
        return self.send(self.cls("NSString"), "stringWithUTF8String:", text.encode("utf-8"),
                         argtypes=(ctypes.c_char_p,))


class NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


_MAC_TARGET = {"cls": None, "imp": None, "trays": {}}


class MacTray:
    """The menu bar item. Must be created, updated and stopped on the main (Tk) thread."""

    def __init__(self, events: "queue.Queue[str]"):
        self.events = events
        self.ready = threading.Event()
        self.ok = False
        self._objc: Optional[_ObjC] = None
        self._item = self._menu = self._target = None
        self._entries: dict[int, Any] = {}
        self._state: tuple = ("SubUsage", None, TRACK_RGB)
        self._applied: Optional[tuple] = None
        self._menu_state = (True, False, False)

    def start(self) -> None:
        try:
            self._start()
            self.ok = True
        except Exception as e:
            log(f"tray (macOS): {e!r}")
            self.ok = False
        self.ready.set()

    def _target_class(self, o: _ObjC):
        if _MAC_TARGET["cls"] is None:
            existing = o.cls("SubUsageTrayTarget")
            if existing:
                _MAC_TARGET["cls"] = existing
            else:
                imp_t = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

                def on_click(target, _sel, sender):
                    tray = _MAC_TARGET["trays"].get(target)
                    if tray is None:
                        return
                    tag = o.send(sender, "tag", restype=ctypes.c_long)
                    event = dict(MENU).get(int(tag))
                    if event:
                        tray.events.put(event)
                imp = imp_t(on_click)
                cls = o.lib.objc_allocateClassPair(o.cls("NSObject"), b"SubUsageTrayTarget", 0)
                o.lib.class_addMethod(cls, o.sel("menuAction:"), ctypes.cast(imp, ctypes.c_void_p), b"v@:@")
                o.lib.objc_registerClassPair(cls)
                _MAC_TARGET["cls"], _MAC_TARGET["imp"] = cls, imp  # keep the callback alive
        return _MAC_TARGET["cls"]

    def _start(self) -> None:
        o = self._objc = _ObjC()
        v, d, L = ctypes.c_void_p, ctypes.c_double, ctypes.c_long
        bar = o.send(o.cls("NSStatusBar"), "systemStatusBar")
        self._item = o.send(bar, "statusItemWithLength:", d(-1.0), argtypes=(d,))  # NSVariableStatusItemLength
        o.send(self._item, "retain")
        self._target = o.send(o.send(self._target_class(o), "alloc"), "init")
        _MAC_TARGET["trays"][self._target] = self
        menu = self._menu = o.send(o.send(o.cls("NSMenu"), "alloc"), "init")
        o.send(menu, "setAutoenablesItems:", False, argtypes=(ctypes.c_bool,))
        labels = menu_labels(True, False)
        for tag, _event in MENU:
            if tag in (4, 9):
                o.send(menu, "addItem:", o.send(o.cls("NSMenuItem"), "separatorItem"), argtypes=(v,))
            item = o.send(o.send(o.cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
                          o.string(labels[tag]), o.sel("menuAction:"), o.string(""), argtypes=(v, v, v))
            o.send(item, "setTarget:", self._target, argtypes=(v,))
            o.send(item, "setTag:", L(tag), argtypes=(L,))
            o.send(menu, "addItem:", item, argtypes=(v,))
            self._entries[tag] = item
        o.send(self._item, "setMenu:", menu, argtypes=(v,))
        self._apply()
        self.set_menu_state(*self._menu_state, force=True)

    def _apply(self) -> None:
        o = self._objc
        if not (o and self._item) or self._state == self._applied:
            return
        tip, pct, rgb = self._state
        png = png_bytes(36, pct, rgb)  # @2x for Retina; displayed at 18 pt
        data = o.send(o.cls("NSData"), "dataWithBytes:length:", png, len(png),
                      argtypes=(ctypes.c_char_p, ctypes.c_ulong))
        img = o.send(o.send(o.cls("NSImage"), "alloc"), "initWithData:", data, argtypes=(ctypes.c_void_p,))
        o.send(img, "setSize:", NSSize(18, 18), argtypes=(NSSize,))
        button = o.send(self._item, "button")
        if button:
            o.send(button, "setImage:", img, argtypes=(ctypes.c_void_p,))
            o.send(button, "setToolTip:", o.string(tip), argtypes=(ctypes.c_void_p,))
        o.send(img, "release")
        self._applied = self._state

    # ----- same interface as the other backends (all on the UI thread here)
    def set_state(self, tooltip: str, pct: Optional[float], rgb: tuple[int, int, int]) -> None:
        bucket = None if pct is None else int(round(pct / 2.0)) * 2
        self._state = ((tooltip or "SubUsage"), bucket, rgb)
        if self.ok:
            try:
                self._apply()
            except Exception as e:
                log(f"tray (macOS): {e!r}")

    def set_menu_state(self, visible: bool, paused: bool, startup: bool, force: bool = False) -> None:
        new = (visible, paused, startup)
        if (new == self._menu_state and not force) or not self._objc or not self._entries:
            self._menu_state = new
            return
        self._menu_state = new
        o, labels = self._objc, menu_labels(visible, paused)
        for tag in (1, 2):
            o.send(self._entries[tag], "setTitle:", o.string(labels[tag]), argtypes=(ctypes.c_void_p,))
        o.send(self._entries[3], "setEnabled:", not paused, argtypes=(ctypes.c_bool,))
        o.send(self._entries[6], "setState:", ctypes.c_long(1 if startup else 0), argtypes=(ctypes.c_long,))

    def stop(self) -> None:
        o = self._objc
        if not (o and self._item):
            return
        try:
            bar = o.send(o.cls("NSStatusBar"), "systemStatusBar")
            o.send(bar, "removeStatusItem:", self._item, argtypes=(ctypes.c_void_p,))
            o.send(self._item, "release")
            _MAC_TARGET["trays"].pop(self._target, None)
        except Exception as e:
            log(f"tray (macOS): {e!r}")
        self._item = None
        self.ok = False

    def is_alive(self) -> bool:  # parity with the thread-based backends
        return self.ok
