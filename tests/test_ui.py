"""Smoke test for the widget, Settings and the Add-subscription picker. Skipped without a display."""
import os
import sys
import unittest

from .helpers import HomeTestCase

try:
    if os.environ.get("CI") and not sys.platform.startswith("linux"):
        raise RuntimeError("UI tests run on Linux CI only (under xvfb)")
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # no tkinter or no display
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "needs tkinter and a display")
class UiSmokeTests(HomeTestCase):
    def pump(self, root, n=20):
        for _ in range(n):
            root.update()

    def test_widget_settings_picker(self):
        from subusage import core
        from subusage.ui import Widget
        cfg = core.load_config()
        w = Widget(core.build_providers(cfg), cfg)
        try:
            self.pump(w.root)
            w.open_settings()
            d = w._settings
            self.assertEqual([r["cls"].type for r in d.rows], [p["type"] for p in cfg["providers"]])
            d.open_picker()
            self.pump(w.root)
            d.add_provider("zai")
            self.assertEqual(d.rows[-1]["cls"].type, "zai")
            d._set(d.rows[-1], "api_key", "env:ZAI_KEY")
            d._remove(next(r for r in d.rows if r["cls"].type == "jetbrains"))
            d.save()
            self.pump(w.root)
            saved = core.load_config()
            types = [p["type"] for p in saved["providers"]]
            self.assertIn("zai", types)
            self.assertNotIn("jetbrains", types)
            self.assertEqual(saved["providers"][-1]["api_key"], "env:ZAI_KEY")
            self.assertEqual(len(w.providers), len(types))
        finally:
            w.root.destroy()

    def test_demo_compact(self):
        from subusage.providers.command import demo_providers
        from subusage.ui import Widget
        w = Widget(demo_providers(), {"compact": True}, demo=True)
        try:
            for p in w.providers:
                p.run_fetch()
            w.render(force=True)
            self.pump(w.root)
            self.assertGreater(w.root.winfo_height(), 50)
        finally:
            w.root.destroy()


if __name__ == "__main__":
    unittest.main()
