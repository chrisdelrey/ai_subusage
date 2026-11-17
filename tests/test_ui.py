"""Smoke test for the widget, Settings and the Add-subscription picker. Skipped without a display."""
import os
import sys
import time
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
            w.quit()

    def test_views_and_update_footer(self):
        from subusage import update
        from subusage.providers.command import demo_providers
        from subusage.ui import Widget
        w = Widget(demo_providers(), {"view": "condensed"}, demo=True)
        try:
            for p in w.providers:
                p.run_fetch()
            heights = {}
            for view in ("full", "condensed", "compact"):
                w.cfg["view"] = view
                w.render(force=True)
                self.pump(w.root)
                heights[view] = w.root.winfo_reqheight()
            self.assertGreater(heights["full"], heights["condensed"])
            self.assertGreater(heights["condensed"], heights["compact"])
            w.update_info = update.UpdateInfo("2.1.0", "9.0.0", "v9.0.0", "", "https://x", checked_at=__import__("time").time())
            w.render(force=True)
            texts = [c.cget("text") for c in w.body.winfo_children()[-1].winfo_children()]
            self.assertIn("⬆ Update 9.0.0", texts)
            w.open_settings(about=True)
            self.pump(w.root)
            self.assertIn("9.0.0", w._settings.upd_status.cget("text"))
            w._settings.v_view.set("condensed")
            w._settings.save()
            self.assertEqual(w.cfg["view"], "condensed")
        finally:
            w.quit()

    def test_declutter_and_history(self):
        from subusage import core, history
        from subusage.core import Snapshot, UsageWindow
        from subusage.ui import Widget
        cfg = core.load_config()
        w = Widget([], cfg)
        try:
            w.providers = core.build_providers({"providers": [{"type": "claude", "name": "A"},
                                                              {"type": "codex", "name": "B"}]})
            cfg["providers"] = [p.cfg for p in w.providers]
            a, b = w.providers
            now = time.time()
            a.snapshot = Snapshot(windows=[UsageWindow("Session (5h)", 20, now + 3600, 18000),
                                           UsageWindow("Weekly", 40, now + 86400, 604800)], fetched_at=now)
            b.snapshot = Snapshot(windows=[UsageWindow("Session (5h)", 95, now + 3600, 18000)], fetched_at=now)
            key = history.provider_key("claude", "A")
            for ago, used in ((1800, 10), (900, 15), (0, 20)):
                history.record(key, Snapshot(windows=[UsageWindow("Session (5h)", used, now + 3600)]), ts=now - ago)
            w.render(force=True)
            self.pump(w.root)

            w.hide_window(a, "Weekly")
            self.assertEqual(core.load_config()["providers"][0]["hidden"], ["Weekly"])
            w.toggle_collapse(b)
            self.assertTrue(core.load_config()["providers"][1]["collapsed"])
            w.cfg["sort"] = "tightest"
            w.render(force=True)
            self.pump(w.root)
            self.assertEqual(w._tightness(b)[0], False)          # 95% with 1 h left → at risk → first

            proj = w.projection(a, a.snapshot.windows[0], now)
            self.assertEqual(proj.source, "recent")
            self.assertAlmostEqual(proj.rate, 20, delta=0.5)      # 10 → 20 over 30 min
            w.tooltip.show(100, 100, a, a.snapshot.windows[0])
            self.pump(w.root)
            self.assertIsNotNone(w.tooltip.top)
            w.tooltip.hide()
            w.open_history(a, "Session (5h)")
            self.pump(w.root)

            w.open_settings()
            d = w._settings
            row = next(r for r in d.rows if r["cls"].type == "claude")
            self.assertEqual(row["cfg"]["hidden"], ["Weekly"])
            d.save()
            self.assertEqual(core.load_config()["providers"][0]["hidden"], ["Weekly"])
            w.unhide_all(w.providers[0])
            w.expand_all()
            self.assertNotIn("hidden", core.load_config()["providers"][0])
        finally:
            w.quit()

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
            w.quit()


if __name__ == "__main__":
    unittest.main()
