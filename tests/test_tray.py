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
        time.sleep(0.5)
        t.stop()
        t.join(10)
        self.assertFalse(t.is_alive(), "tray thread didn't exit")


if __name__ == "__main__":
    unittest.main()
