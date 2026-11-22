import struct
import sys
import time
import unittest
import queue

from subusage import core, tray

from .helpers import HomeTestCase


class IconTests(unittest.TestCase):
    def test_icon_image_layout(self):
        n = 16
        img = tray.icon_image(n, 50, (79, 179, 127))
        size, w, h, planes, bpp = struct.unpack("<IiiHH", img[:16])
        self.assertEqual((size, w, h, planes, bpp), (40, 16, 32, 1, 32))   # height doubled for the AND mask
        self.assertEqual(len(img), 40 + n * n * 4 + 4 * n)

    def test_ring_fill(self):
        n = 32
        px = tray.ring_pixels(n, 25, (255, 0, 0))

        def at(x, y):  # (x, y) from the top-left; pixels are stored bottom-up as BGRA
            i = ((n - 1 - y) * n + x) * 4
            b, g, r, a = px[i:i + 4]
            return r, g, b, a
        self.assertEqual(at(n // 2, n // 2)[3], 0)               # transparent center
        self.assertEqual(at(0, 0)[3], 0)                         # transparent corner
        r, g, b, a = at(n - 3, n // 2 - 4)                       # ring, ~2 o'clock: inside the first 25%
        self.assertEqual((r, g, b), (255, 0, 0))
        self.assertGreater(a, 200)
        r, g, b, a = at(2, n // 2)                               # ring, 9 o'clock: track
        self.assertEqual((r, g, b), tray.TRACK_RGB)

    def test_ico_file(self):
        data = tray.ico_file(80, (228, 88, 88))
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind, count), (0, 1, 4))
        w, _h, _c, _r, _p, bpp, size, offset = struct.unpack("<BBBBHHII", data[6:22])
        self.assertEqual((w, bpp), (16, 32))
        self.assertEqual(struct.unpack("<I", data[offset:offset + 4])[0], 40)

    def test_tooltip_cap(self):
        self.assertEqual(tray.summary_tooltip(["Claude: 37%"]), "SubUsage\nClaude: 37%")
        long = tray.summary_tooltip([f"Subscription {i}: 99% ⚠" for i in range(20)])
        self.assertLessEqual(len(long), 127)
        self.assertTrue(long.endswith("…"))


class InstanceSignalTests(unittest.TestCase):
    def test_second_launch_sends_show(self):
        port = 47000 + int(time.time()) % 900
        self.assertTrue(core.acquire_single_instance(port))
        try:
            self.assertFalse(core.acquire_single_instance(port))  # already held
            core.start_instance_listener()
            self.assertTrue(core.signal_running_instance("show", port))
            self.assertEqual(core.instance_messages.get(timeout=3), "show")
        finally:
            core._instance_sock.close()
            core._instance_sock = None


@unittest.skipUnless(sys.platform == "win32", "Win32 tray")
class WinTraySmokeTests(unittest.TestCase):
    """Runs on the Windows CI runner: exercises every ctypes signature on a real (64-bit) Windows."""

    def test_lifecycle(self):
        events: "queue.Queue[str]" = queue.Queue()
        t = tray.WinTray(events)
        t.start()
        self.assertTrue(t.ready.wait(10))
        self.assertTrue(t.hwnd, "hidden tray window wasn't created")
        # ok can be False on a CI runner without an Explorer taskbar; the window and loop must still work
        t.set_state("SubUsage\nClaude: 37%", 37, (79, 179, 127))
        t.set_state("SubUsage\nClaude: 95% ⚠", 95, (228, 88, 88))
        t.set_menu_state(False, True, True)
        time.sleep(0.5)
        t.stop()
        t.join(10)
        self.assertFalse(t.is_alive(), "tray thread didn't exit")



class PngTests(unittest.TestCase):
    def test_png(self):
        import zlib
        png = tray.png_bytes(36, 40, (220, 160, 40))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h, depth, ctype = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((w, h, depth, ctype), (36, 36, 8, 6))
        idat_len = struct.unpack(">I", png[33:37])[0]
        raw = zlib.decompress(png[41:41 + idat_len])
        self.assertEqual(len(raw), 36 * (1 + 36 * 4))

    def test_menu_labels(self):
        self.assertEqual(tray.menu_labels(True, False)[1], "Hide widget")
        self.assertEqual(tray.menu_labels(False, True)[2], "Resume updates")
        self.assertEqual({e for _, e in tray.MENU}, {"toggle", "pause", "refresh", "settings", "add", "startup", "quit"})


class DBusMarshalTests(unittest.TestCase):
    def roundtrip(self, sig, *values):
        from subusage import sni
        m = sni.Message(sni.METHOD_CALL, 7, 0, {sni.F_PATH: "/x", sni.F_MEMBER: "M"}, sig, values)
        back, used = sni.Message.decode(m.encode() + b"tail")
        self.assertEqual(used, len(m.encode()))
        return back

    def test_types(self):
        from subusage import sni
        self.assertEqual(sni.split_types("sa{sv}(ii)aiai"), ["s", "a{sv}", "(ii)", "ai", "ai"])
        b = self.roundtrip("ysbuxda{sv}(iiay)as", 7, "héllo", True, 2 ** 32 - 1, -5, 1.5,
                           {"a": ("s", "x"), "b": ("i", 3), "c": ("as", ["p", "q"])}, (1, 2, b"\x00\xff"), ["z"])
        self.assertEqual(b.body, (7, "héllo", True, 2 ** 32 - 1, -5, 1.5,
                                  {"a": ("s", "x"), "b": ("i", 3), "c": ("as", ["p", "q"])}, (1, 2, b"\x00\xff"), ["z"]))
        self.assertEqual((b.path, b.member, b.serial), ("/x", "M", 7))

    def test_layout_shape(self):
        from subusage import sni
        t = sni.SNITray(queue.Queue())
        b = self.roundtrip("u(ia{sv}av)", 1, t._layout())
        children = b.body[1][2]
        self.assertEqual(children[0], ("(ia{sv}av)", (1, {"label": ("s", "Hide widget")}, [])))
        props = t.item_properties()
        self.assertEqual(len(props["IconPixmap"][1][0][2]), 22 * 22 * 4)
        self.assertEqual(self.roundtrip("a{sv}", props).body[0]["Menu"], ("o", "/MenuBar"))

    def test_partial_message(self):
        from subusage import sni
        data = sni.Message(sni.SIGNAL, 1, 0, {sni.F_PATH: "/x", sni.F_IFACE: "a.b", sni.F_MEMBER: "S"}, "s", ("x" * 50,)).encode()
        self.assertEqual(sni.Message.decode(data[:-3]), (None, 0))


def _dbus_daemon():
    import shutil
    return shutil.which("dbus-daemon") if sys.platform.startswith("linux") else None


@unittest.skipUnless(_dbus_daemon(), "needs dbus-daemon (Linux)")
class SNIIntegrationTests(unittest.TestCase):
    """A private session bus, a fake panel (StatusNotifierWatcher) and the real tray item."""

    def setUp(self):
        import os
        import subprocess
        import tempfile
        self.tmp = tempfile.mkdtemp()
        sock = os.path.join(self.tmp, "bus")
        cfg = os.path.join(self.tmp, "bus.conf")
        with open(cfg, "w") as f:
            f.write(f"""<busconfig><type>session</type><listen>unix:path={sock}</listen>
<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*" eavesdrop="true"/>
<allow eavesdrop="true"/><allow own="*"/></policy></busconfig>""")
        self.daemon = subprocess.Popen([_dbus_daemon(), "--config-file", cfg, "--nofork"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.saved = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sock}"
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.05)

    def tearDown(self):
        import os
        import shutil
        self.daemon.kill()
        self.daemon.wait(5)
        if self.saved is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_item(self):
        import threading
        from subusage import sni
        self.assertFalse(sni.watcher_present())
        w = sni.Bus()
        w.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName", "su",
               sni.WATCHER, 4)
        registered = []

        def panel():
            w.sock.settimeout(None)
            while True:
                try:
                    m = w.recv()
                except OSError:
                    return
                if m.type == sni.METHOD_CALL and m.member == "RegisterStatusNotifierItem":
                    registered.append(m.body[0])
                    w.reply(m)
        threading.Thread(target=panel, daemon=True).start()
        self.assertTrue(sni.watcher_present())

        events = queue.Queue()
        t = sni.SNITray(events)
        t.start()
        self.assertTrue(t.ready.wait(5))
        self.assertTrue(t.ok)
        self.assertEqual(len(registered), 1)
        c = sni.Bus()  # plays the panel's client side
        name = registered[0]
        t.set_state("SubUsage\nClaude: 40%", 40, (220, 160, 40))
        tip = c.call(name, sni.ITEM_PATH, sni.PROPS, "Get", "ss", sni.ITEM_IFACE, "ToolTip")[0]
        self.assertEqual(tip, ("(sa(iiay)ss)", ("", [], "SubUsage", "Claude: 40%")))
        props = c.call(name, sni.ITEM_PATH, sni.PROPS, "GetAll", "s", sni.ITEM_IFACE)[0]
        self.assertEqual(props["Status"], ("s", "Active"))
        t.set_menu_state(False, True, False)
        rev, layout = c.call(name, sni.MENU_PATH, sni.MENU_IFACE, "GetLayout", "iias", 0, -1, [])
        self.assertEqual(layout[2][0][1][1]["label"], ("s", "Show widget"))
        self.assertEqual(layout[2][1][1][1]["label"], ("s", "Resume updates"))
        c.call(name, sni.MENU_PATH, sni.MENU_IFACE, "Event", "isvu", 6, "clicked", ("i", 0), 0)
        c.call(name, sni.ITEM_PATH, sni.ITEM_IFACE, "Activate", "ii", 0, 0)
        self.assertEqual([events.get(timeout=2), events.get(timeout=2)], ["startup", "toggle"])
        self.assertIn("StatusNotifierItem", c.call(name, sni.ITEM_PATH, "org.freedesktop.DBus.Introspectable",
                                                   "Introspect")[0])
        t.stop()
        t.join(5)
        self.assertFalse(t.is_alive())
        c.close()
        w.close()


@unittest.skipUnless(sys.platform == "darwin", "macOS menu bar")
class MacTraySmokeTests(unittest.TestCase):
    """Runs on the macOS CI runner: every Objective-C call against real AppKit."""

    def test_lifecycle(self):
        try:
            import tkinter
            root = tkinter.Tk()  # Tk sets up NSApplication on the main thread, as the widget does
        except Exception as e:
            self.skipTest(f"no Tk display: {e}")
        try:
            events: "queue.Queue[str]" = queue.Queue()
            t = tray.MacTray(events)
            t.start()
            self.assertTrue(t.ready.is_set())
            self.assertTrue(t.ok, "status item wasn't created (see widget.log)")
            t.set_state("SubUsage\nClaude: 37%", 37, (79, 179, 127))
            t.set_menu_state(False, True, True)
            root.update()
            t.stop()
            self.assertFalse(t.ok)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
