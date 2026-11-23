"""Linux tray icon: a StatusNotifierItem over D-Bus, standard library only.

StatusNotifierItem (SNI) is what KDE Plasma, Ubuntu's GNOME (AppIndicator extension), Cinnamon, XFCE,
LXQt, Budgie, swaybar/waybar and most other panels use. It needs a session bus and a running
org.kde.StatusNotifierWatcher; without one, available() is False and the widget simply stays visible.

This file has a small D-Bus wire-protocol client (unix socket, EXTERNAL auth, marshalling for the
handful of types SNI and com.canonical.dbusmenu use) and the item itself, which runs on its own thread.
Like the Windows tray it never touches Tk: events go on a queue that the widget polls.
"""
from __future__ import annotations

import os
import queue
import socket
import struct
import threading
from typing import Any, Callable, Optional

from .core import log

# --------------------------------------------------------------------------------------------------
# Marshalling (little-endian only; that's what we send, and we decode either)
# --------------------------------------------------------------------------------------------------

ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "s": 4, "o": 4, "g": 1, "a": 4,
         "(": 8, "{": 8, "v": 1, "h": 4}
FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


def split_types(sig: str) -> list[str]:
    """'sa{sv}(ii)' -> ['s', 'a{sv}', '(ii)']"""
    out, i = [], 0
    while i < len(sig):
        j = _end(sig, i)
        out.append(sig[i:j])
        i = j
    return out


def _end(sig: str, i: int) -> int:
    c = sig[i]
    if c == "a":
        return _end(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        depth, j = 0, i
        while j < len(sig):
            if sig[j] in "({":
                depth += 1
            elif sig[j] in ")}":
                depth -= 1
                if depth == 0 and sig[j] == close:
                    return j + 1
            j += 1
        raise ValueError(f"bad signature {sig!r}")
    return i + 1


class Writer:
    def __init__(self, offset: int = 0):
        self.buf = bytearray()
        self.offset = offset  # alignment is relative to the start of the message

    def pad(self, n: int) -> None:
        while (self.offset + len(self.buf)) % n:
            self.buf.append(0)

    def write(self, sig: str, value: Any) -> None:
        c = sig[0]
        self.pad(ALIGN[c])
        if c in FIXED:
            v = (1 if value else 0) if c == "b" else value
            self.buf += struct.pack("<" + FIXED[c], v)
        elif c in "so":
            b = value.encode("utf-8")
            self.buf += struct.pack("<I", len(b)) + b + b"\0"
        elif c == "g":
            b = value.encode("ascii")
            self.buf += struct.pack("<B", len(b)) + b + b"\0"
        elif c == "v":
            vsig, v = value
            self.write("g", vsig)
            self.write(vsig, v)
        elif c == "(":
            for t, v in zip(split_types(sig[1:-1]), value):
                self.write(t, v)
        elif c == "a":
            elem = sig[1:]
            self.buf += b"\0\0\0\0"
            at = len(self.buf)
            self.pad(ALIGN[elem[0]])
            start = len(self.buf)
            if elem == "y":
                self.buf += bytes(value)
            elif elem[0] == "{":
                kt, vt = split_types(elem[1:-1])
                for k, v in (value.items() if isinstance(value, dict) else value):
                    self.pad(8)
                    self.write(kt, k)
                    self.write(vt, v)
            else:
                for v in value:
                    self.write(elem, v)
            struct.pack_into("<I", self.buf, at - 4, len(self.buf) - start)
        else:
            raise ValueError(f"can't marshal {sig!r}")


class Reader:
    def __init__(self, data: bytes, endian: str = "<", offset: int = 0):
        self.data, self.e, self.pos, self.base = data, endian, offset, 0

    def align(self, n: int) -> None:
        self.pos += (-self.pos) % n

    def read(self, sig: str) -> Any:
        c = sig[0]
        self.align(ALIGN[c])
        if c in FIXED:
            fmt = self.e + FIXED[c]
            v = struct.unpack_from(fmt, self.data, self.pos)[0]
            self.pos += struct.calcsize(fmt)
            return bool(v) if c == "b" else v
        if c in "so":
            n = struct.unpack_from(self.e + "I", self.data, self.pos)[0]
            s = self.data[self.pos + 4: self.pos + 4 + n].decode("utf-8", "replace")
            self.pos += 4 + n + 1
            return s
        if c == "g":
            n = self.data[self.pos]
            s = self.data[self.pos + 1: self.pos + 1 + n].decode("ascii")
            self.pos += 1 + n + 1
            return s
        if c == "v":
            vsig = self.read("g")
            return (vsig, self.read(vsig))
        if c == "(":
            return tuple(self.read(t) for t in split_types(sig[1:-1]))
        if c == "a":
            n = struct.unpack_from(self.e + "I", self.data, self.pos)[0]
            self.pos += 4
            elem = sig[1:]
            self.align(ALIGN[elem[0]])
            end = self.pos + n
            if elem == "y":
                v = bytes(self.data[self.pos:end])
                self.pos = end
                return v
            if elem[0] == "{":
                kt, vt = split_types(elem[1:-1])
                d = {}
                while self.pos < end:
                    self.align(8)
                    k = self.read(kt)
                    d[k] = self.read(vt)
                return d
            out = []
            while self.pos < end:
                out.append(self.read(elem))
            return out
        raise ValueError(f"can't unmarshal {sig!r}")


METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
F_PATH, F_IFACE, F_MEMBER, F_ERROR, F_REPLY, F_DEST, F_SENDER, F_SIG = 1, 2, 3, 4, 5, 6, 7, 8
NO_REPLY = 0x1


class Message:
    def __init__(self, mtype: int, serial: int = 0, flags: int = 0, fields: Optional[dict[int, Any]] = None,
                 sig: str = "", body: tuple = ()):
        self.type, self.serial, self.flags, self.fields, self.sig, self.body = mtype, serial, flags, fields or {}, sig, body

    path = property(lambda s: s.fields.get(F_PATH))
    iface = property(lambda s: s.fields.get(F_IFACE))
    member = property(lambda s: s.fields.get(F_MEMBER))
    sender = property(lambda s: s.fields.get(F_SENDER))
    reply_to = property(lambda s: s.fields.get(F_REPLY))
    error = property(lambda s: s.fields.get(F_ERROR))

    def encode(self) -> bytes:
        body = Writer(0)
        for t, v in zip(split_types(self.sig), self.body):
            body.write(t, v)
        codes = {F_PATH: "o", F_IFACE: "s", F_MEMBER: "s", F_ERROR: "s", F_REPLY: "u", F_DEST: "s", F_SENDER: "s",
                 F_SIG: "g"}
        fields = dict(self.fields)
        if self.sig:
            fields[F_SIG] = self.sig
        head = Writer(0)
        head.write("y", ord("l"))
        head.write("y", self.type)
        head.write("y", self.flags)
        head.write("y", 1)
        head.write("u", len(body.buf))
        head.write("u", self.serial)
        head.write("a(yv)", [(k, (codes[k], v)) for k, v in sorted(fields.items())])
        head.pad(8)
        return bytes(head.buf) + bytes(body.buf)

    @staticmethod
    def decode(data: bytes) -> tuple[Optional["Message"], int]:
        """(message, bytes used), or (None, 0) if data doesn't hold a whole message yet."""
        if len(data) < 16:
            return None, 0
        e = "<" if data[0:1] == b"l" else ">"
        body_len, serial, flen = struct.unpack_from(e + "III", data, 4)
        head_end = 16 + flen
        head_end += (-head_end) % 8
        total = head_end + body_len
        if len(data) < total:
            return None, 0
        r = Reader(data, e, 12)
        fields = {k: v[1] for k, v in r.read("a(yv)")}
        m = Message(data[1], serial, data[2], fields, fields.get(F_SIG, ""))
        br = Reader(data[head_end:total], e, 0)
        m.body = tuple(br.read(t) for t in split_types(m.sig)) if m.sig else ()
        return m, total


# --------------------------------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------------------------------

def session_bus_socket() -> socket.socket:
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not addr:
        p = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(p):
            addr = f"unix:path={p}"
    for part in addr.split(";"):
        kind, _, rest = part.partition(":")
        if kind != "unix":
            continue
        opts = dict(kv.split("=", 1) for kv in rest.split(",") if "=" in kv)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if "path" in opts:
                s.connect(opts["path"])
            elif "abstract" in opts:
                s.connect("\0" + opts["abstract"])
            else:
                s.close()
                continue
            return s
        except OSError:
            s.close()
    raise OSError("no D-Bus session bus")


class Bus:
    """A blocking D-Bus connection. call() is for setup; serve() runs the handler loop on one thread."""

    def __init__(self):
        self.sock = session_bus_socket()
        self.sock.settimeout(5)
        self._serial = 0
        self._buf = b""
        self._lock = threading.Lock()
        self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        line = self._readline()
        if not line.startswith(b"OK"):
            raise OSError(f"D-Bus auth failed: {line!r}")
        self.sock.sendall(b"BEGIN\r\n")
        self.unique_name = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     "Hello")[0]

    def _readline(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("D-Bus closed during auth")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\r\n")
        return line

    def next_serial(self) -> int:
        with self._lock:
            self._serial += 1
            return self._serial

    def send(self, m: Message) -> int:
        if not m.serial:
            m.serial = self.next_serial()
        data = m.encode()
        with self._lock:
            self.sock.sendall(data)
        return m.serial

    def recv(self) -> Message:
        while True:
            m, used = Message.decode(self._buf)
            if m:
                self._buf = self._buf[used:]
                return m
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("D-Bus connection closed")
            self._buf += chunk

    def call(self, dest: str, path: str, iface: str, member: str, sig: str = "", *args: Any,
             on_other: Optional[Callable[[Message], None]] = None) -> tuple:
        serial = self.send(Message(METHOD_CALL, fields={F_DEST: dest, F_PATH: path, F_IFACE: iface, F_MEMBER: member},
                                   sig=sig, body=args))
        while True:
            m = self.recv()
            if m.reply_to == serial and m.type in (METHOD_RETURN, ERROR):
                if m.type == ERROR:
                    raise OSError(f"{member}: {m.error} {m.body[:1]}")
                return m.body
            if on_other:
                on_other(m)

    def reply(self, to: Message, sig: str = "", *args: Any) -> None:
        if to.flags & NO_REPLY:
            return
        self.send(Message(METHOD_RETURN, fields={F_REPLY: to.serial, F_DEST: to.sender}, sig=sig, body=args))

    def error(self, to: Message, name: str, text: str) -> None:
        if to.flags & NO_REPLY:
            return
        self.send(Message(ERROR, fields={F_REPLY: to.serial, F_DEST: to.sender, F_ERROR: name}, sig="s", body=(text,)))

    def signal(self, path: str, iface: str, member: str, sig: str = "", *args: Any) -> None:
        self.send(Message(SIGNAL, fields={F_PATH: path, F_IFACE: iface, F_MEMBER: member}, sig=sig, body=args))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------------------------------
# The tray item
# --------------------------------------------------------------------------------------------------

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS = "org.freedesktop.DBus.Properties"
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"

INTROSPECT = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/><property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/><property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewToolTip"/><signal name="NewTitle"/><signal name="NewStatus"><arg type="s"/></signal>
 </interface>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
   <arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
   <arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
   <arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 </interface>
 <interface name="org.freedesktop.DBus.Properties">
  <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
 </interface>
 <interface name="org.freedesktop.DBus.Introspectable">
  <method name="Introspect"><arg type="s" direction="out"/></method>
 </interface>
</node>"""


def argb_pixmap(n: int, pct: Optional[float], rgb: tuple[int, int, int]) -> bytes:
    """SNI wants ARGB32 in network byte order, rows top-down."""
    from .tray import ring_pixels
    bgra = ring_pixels(n, pct, rgb)  # bottom-up BGRA
    rows = [bgra[r * n * 4:(r + 1) * n * 4] for r in range(n)][::-1]
    out = bytearray()
    for row in rows:
        for i in range(0, len(row), 4):
            b, g, r, a = row[i:i + 4]
            out += bytes((a, r, g, b))
    return bytes(out)


class SNITray(threading.Thread):
    """Same interface as tray.WinTray: events go to `events`; set_state() / set_menu_state() / stop()."""

    def __init__(self, events: "queue.Queue[str]"):
        super().__init__(daemon=True, name="SubUsageTray")
        self.events = events
        self.ready = threading.Event()
        self.ok = False
        self.bus: Optional[Bus] = None
        self._state: tuple[str, Optional[float], tuple[int, int, int]] = ("SubUsage", None, (150, 156, 168))
        self._menu = {"visible": True, "paused": False, "startup": False}
        self._revision = 1
        self._stopping = False
        self._name = ""

    # ----- called from the UI thread
    def set_state(self, tooltip: str, pct: Optional[float], rgb: tuple[int, int, int]) -> None:
        bucket = None if pct is None else int(round(pct / 2.0)) * 2
        state = (tooltip or "SubUsage", bucket, rgb)
        if state != self._state:
            old = self._state
            self._state = state
            if self.bus and self.ok:
                try:
                    if old[1:] != state[1:]:
                        self.bus.signal(ITEM_PATH, ITEM_IFACE, "NewIcon")
                    if old[0] != state[0]:
                        self.bus.signal(ITEM_PATH, ITEM_IFACE, "NewToolTip")
                except OSError as e:
                    log(f"tray: {e}")

    def set_menu_state(self, visible: bool, paused: bool, startup: bool) -> None:
        new = {"visible": visible, "paused": paused, "startup": startup}
        if new != self._menu:
            self._menu = new
            self._revision += 1
            if self.bus and self.ok:
                try:
                    self.bus.signal(MENU_PATH, MENU_IFACE, "LayoutUpdated", "ui", self._revision, 0)
                except OSError as e:
                    log(f"tray: {e}")

    def stop(self) -> None:
        self._stopping = True
        bus = self.bus
        if bus:
            try:
                bus.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ----- tray thread
    def run(self) -> None:
        try:
            self.bus = Bus()
            self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                          "su", self._name, 4)
            self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                          f"type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{WATCHER}'",
                          on_other=self._dispatch)
            self._register()
            self.ok = True
        except Exception as e:
            log(f"tray: StatusNotifierItem unavailable ({e})")
            self.ok = False
            self.ready.set()
            if self.bus:
                self.bus.close()
            return
        self.ready.set()
        self.bus.sock.settimeout(None)
        try:
            while not self._stopping:
                self._dispatch(self.bus.recv())
        except OSError:
            pass
        except Exception as e:
            log(f"tray loop: {e!r}")
        finally:
            self.ok = False
            self.bus.close()

    def _register(self) -> None:
        assert self.bus
        self.bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem", "s", self._name,
                      on_other=self._dispatch)

    def _menu_items(self) -> list[tuple[int, str, str, dict[str, Any]]]:
        m = self._menu
        return [
            (1, "toggle", "Hide widget" if m["visible"] else "Show widget", {}),
            (2, "pause", "Resume updates" if m["paused"] else "Pause updates", {}),
            (3, "refresh", "Refresh now", {"enabled": not m["paused"]}),
            (10, "", "", {"type": "separator"}),
            (4, "settings", "Settings…", {}),
            (5, "add", "Add subscription…", {}),
            (6, "startup", "Start at login", {"toggle-type": "checkmark", "toggle-state": 1 if m["startup"] else 0}),
            (11, "", "", {"type": "separator"}),
            (9, "quit", "Quit SubUsage", {}),
        ]

    def _item_props(self, label: str, extra: dict[str, Any]) -> dict[str, tuple[str, Any]]:
        props: dict[str, tuple[str, Any]] = {}
        if extra.get("type") == "separator":
            props["type"] = ("s", "separator")
            return props
        props["label"] = ("s", label)
        if "enabled" in extra:
            props["enabled"] = ("b", bool(extra["enabled"]))
        if "toggle-type" in extra:
            props["toggle-type"] = ("s", extra["toggle-type"])
            props["toggle-state"] = ("i", int(extra["toggle-state"]))
        return props

    def _layout(self) -> tuple:
        children = [("(ia{sv}av)", (i, self._item_props(lbl, x), [])) for i, _ev, lbl, x in self._menu_items()]
        return (0, {"children-display": ("s", "submenu")}, children)

    def item_properties(self) -> dict[str, tuple[str, Any]]:
        tip, pct, rgb = self._state
        pix = [(n, n, argb_pixmap(n, pct, rgb)) for n in (22, 32, 48)]
        title, _, rest = tip.partition("\n")
        return {"Category": ("s", "ApplicationStatus"), "Id": ("s", "subusage"), "Title": ("s", "SubUsage"),
                "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""),
                "IconPixmap": ("a(iiay)", pix), "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH),
                "ToolTip": ("(sa(iiay)ss)", ("", [], title, rest.replace("\n", "<br/>")))}

    def _dispatch(self, m: Message) -> None:
        bus = self.bus
        assert bus
        if m.type == SIGNAL:
            if m.member == "NameOwnerChanged" and m.body and m.body[0] == WATCHER and m.body[2]:
                try:  # the panel (and its watcher) restarted
                    bus.send(Message(METHOD_CALL, fields={F_DEST: WATCHER, F_PATH: "/StatusNotifierWatcher",
                                                          F_IFACE: WATCHER, F_MEMBER: "RegisterStatusNotifierItem"},
                                     flags=NO_REPLY, sig="s", body=(self._name,)))
                except OSError:
                    pass
            return
        if m.type != METHOD_CALL:
            return
        iface, member, path = m.iface, m.member, m.path
        try:
            if member == "Introspect":
                return bus.reply(m, "s", INTROSPECT)
            if iface == "org.freedesktop.DBus.Peer" or member == "Ping":
                return bus.reply(m)
            if iface == PROPS and member in ("Get", "GetAll"):
                which = m.body[0]
                props = self.item_properties() if which == ITEM_IFACE else {
                    "Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                    "IconThemePath": ("as", [])} if which == MENU_IFACE else {}
                if member == "GetAll":
                    return bus.reply(m, "a{sv}", props)
                if m.body[1] in props:
                    return bus.reply(m, "v", props[m.body[1]])
                return bus.error(m, "org.freedesktop.DBus.Error.UnknownProperty", m.body[1])
            if path == ITEM_PATH and member in ("Activate", "SecondaryActivate"):
                self.events.put("toggle")
                return bus.reply(m)
            if path == ITEM_PATH and member in ("ContextMenu", "Scroll", "ProvideXdgActivationToken"):
                return bus.reply(m)
            if path == MENU_PATH:
                if member == "GetLayout":
                    return bus.reply(m, "u(ia{sv}av)", self._revision, self._layout())
                if member == "GetGroupProperties":
                    ids = set(m.body[0])
                    rows = [(i, self._item_props(lbl, x)) for i, _e, lbl, x in self._menu_items() if not ids or i in ids]
                    return bus.reply(m, "a(ia{sv})", rows)
                if member == "GetProperty":
                    props = dict((i, self._item_props(lbl, x)) for i, _e, lbl, x in self._menu_items())
                    p = props.get(m.body[0], {}).get(m.body[1])
                    return bus.reply(m, "v", p) if p else bus.error(m, "org.freedesktop.DBus.Error.InvalidArgs", "")
                if member in ("Event", "EventGroup"):
                    events = [m.body] if member == "Event" else list(m.body[0])
                    for ev in events:
                        if ev[1] == "clicked":
                            hit = next((e for i, e, _l, _x in self._menu_items() if i == ev[0] and e), None)
                            if hit:
                                self.events.put(hit)
                    return bus.reply(m) if member == "Event" else bus.reply(m, "ai", [])
                if member == "AboutToShow":
                    return bus.reply(m, "b", False)
                if member == "AboutToShowGroup":
                    return bus.reply(m, "aiai", [], [])
            bus.error(m, "org.freedesktop.DBus.Error.UnknownMethod", f"{iface}.{member}")
        except OSError:
            raise
        except Exception as e:
            log(f"tray: {member}: {e!r}")
            try:
                bus.error(m, "org.freedesktop.DBus.Error.Failed", str(e))
            except OSError:
                pass


def watcher_present() -> bool:
    try:
        bus = Bus()
    except Exception:
        return False
    try:
        return bool(bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                             "s", WATCHER)[0])
    except Exception:
        return False
    finally:
        bus.close()
