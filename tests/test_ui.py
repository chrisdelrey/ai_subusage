"""Smoke test for the widget, Settings and the Add-subscription picker. Skipped without a display."""
import gc
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
    del _root
    HAVE_DISPLAY = True
except Exception:  # no tkinter or no display
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "needs tkinter and a display")
class UiSmokeTests(HomeTestCase):
    # Each test makes (and destroys) its own Tk root. Collect the dead roots here, on the main thread:
    # if the cyclic GC happened to free one from a background fetch thread, Tcl would abort the process.
    # (The real widget has a single root for its whole life, so this only matters in tests.)
    def tearDown(self):
        gc.collect()
        super().tearDown()

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
            # Grouped by vendor, collapsed until opened, and a new row opens itself.
            d.add_provider("anthropic_api")
            self.assertIn("Anthropic", d.groups)
            self.assertEqual({r["cls"].type for r in d.groups["Anthropic"]["rows"]}, {"claude", "anthropic_api"})
            claude = next(r for r in d.rows if r["cls"].type == "claude")
            self.assertFalse(claude["built"])
            self.assertTrue(d.rows[-1]["built"])
            d._toggle_row(claude, True)
            self.assertTrue(claude["built"] and claude["body"].winfo_manager())
            d.show("add")
            d._set_kind("api")
            self.assertTrue(d._pick_hits and all(c.auth == "api" for c in d._pick_hits))
            for page in ("display", "tray", "data", "about", "providers"):
                d.show(page)
                self.pump(w.root)
            d._filter_rows("anthropic")
            self.assertEqual(d.filter_note.cget("text"), f"2 of {len(d.rows)}")
            d._filter_rows("")
            d.save()
            self.pump(w.root)
            saved = core.load_config()
            types = [p["type"] for p in saved["providers"]]
            self.assertIn("zai", types)
            self.assertEqual(types.index("anthropic_api"), types.index("claude") + 1)  # saved grouped by vendor
            self.assertNotIn("jetbrains", types)
            self.assertEqual(next(p for p in saved["providers"] if p["type"] == "zai")["api_key"], "env:ZAI_KEY")
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

    def test_expired_display_modes(self):
        from subusage import core
        from subusage.core import Snapshot, UsageWindow
        from subusage.ui import Widget
        cfg = core.load_config()
        w = Widget([], cfg)
        try:
            w.providers = core.build_providers({"providers": [{"type": "claude", "name": "A"},
                                                              {"type": "nous", "name": "Hermes"}]})
            a, h = w.providers
            a.snapshot = Snapshot(windows=[UsageWindow("Weekly", 40)], fetched_at=time.time())
            h.snapshot = Snapshot(error="Login expired")
            h.park("Login expired")

            def names():
                w.render(force=True)
                self.pump(w.root)
                out = []
                for row in w.body.winfo_children():
                    for c in row.winfo_children():
                        if isinstance(c, __import__("tkinter").Label):
                            out.append(c.cget("text"))
                return out
            self.assertNotIn("Hermes", names())                   # default: hidden
            w.cfg["expired_display"] = "dim"
            got = names()
            self.assertIn("Hermes", got)
            self.assertIn("sign-in expired · returns when used", got)
            w.cfg["expired_display"] = "show"
            self.assertIn("Hermes", names())
        finally:
            w.quit()

    def test_refresh_updates_in_place(self):
        """New data and ticking countdowns must not rebuild (that's what made the widget flash)."""
        import tkinter
        from subusage import core
        from subusage.core import Snapshot, UsageWindow
        from subusage.ui import Widget
        for view in ("full", "condensed", "compact"):
            cfg = core.load_config()
            cfg["view"] = view
            w = Widget([], cfg)
            try:
                w.providers = core.build_providers({"providers": [{"type": "claude", "name": "A"}]})
                a = w.providers[0]
                now = time.time()
                a.snapshot = Snapshot(plan="Max", windows=[UsageWindow("Session (5h)", 20, now + 3600, 18000),
                                                           UsageWindow("Weekly", 40, now + 86400, 604800)],
                                      fetched_at=now)
                w.render(force=True)
                self.pump(w.root)
                body, builds = w.body, w.rebuilds
                before = [str(x) for x in w._all_widgets(body)]

                a.snapshot = Snapshot(plan="Max", windows=[UsageWindow("Session (5h)", 55, now + 3000, 18000),
                                                           UsageWindow("Weekly", 41, now + 86000, 604800)],
                                      fetched_at=now + 300)
                w._vals = None          # as if the 30 s tick rolled over
                w.render()
                self.pump(w.root)
                self.assertEqual((w.rebuilds, w.body), (builds, body), view)
                self.assertEqual([str(x) for x in w._all_widgets(w.body)], before, view)
                if view == "condensed":
                    canv = [c for c in w._all_widgets(w.body) if isinstance(c, tkinter.Canvas)]
                    texts = [c.itemcget(i, "text") for c in canv for i in c.find_all() if c.type(i) == "text"]
                else:
                    texts = [x.cget("text") for x in w._all_widgets(w.body) if isinstance(x, tkinter.Label)]
                self.assertTrue(any("55%" in t for t in texts), (view, texts))

                w.hide_window(a, "Weekly")          # a real layout change still rebuilds
                self.assertEqual(w.rebuilds, builds + 1)
            finally:
                w.quit()

    def test_whats_new_after_upgrade(self):
        from subusage import __version__, core, settings
        from subusage.ui import Widget
        shown = []
        orig = settings.notes_dialog
        settings.notes_dialog = lambda parent, title, text, footer="": shown.append((title, text, footer))
        try:
            cfg = core.load_config()
            cfg["last_seen_version"] = __version__          # same version: nothing to show
            w = Widget([], cfg)
            w._whats_new()
            self.assertEqual(shown, [])
            w.cfg["pending_whats_new"] = {"from": "2.2.0", "to": __version__, "notes": "## x\n- fallback"}
            w._whats_new()
            self.pump(w.root)
            self.assertEqual(len(shown), 1)
            self.assertIn(__version__, shown[0][0])
            self.assertIn("No more flashing", shown[0][1])   # read from the local CHANGELOG.md
            self.assertEqual(shown[0][2], "Updated from 2.2.0")
            saved = core.load_config()
            self.assertEqual(saved["last_seen_version"], __version__)
            self.assertNotIn("pending_whats_new", saved)
            w.quit()
        finally:
            settings.notes_dialog = orig

    def test_notes_dialog_renders(self):
        import tkinter
        from subusage.settings import notes_dialog
        root = tkinter.Tk()
        try:
            top = notes_dialog(root, "What's new", "## 2.3.0\n- **Bold** item with `code`\n- plain")
            self.pump(root)
            text = [c for c in top.winfo_children()[0].winfo_children() if isinstance(c, tkinter.Text)][0]
            body = text.get("1.0", "end")
            self.assertIn("Version 2.3.0", body)
            self.assertIn("•  Bold item with code", body)
        finally:
            root.destroy()

    def test_tray_modes_with_fake_tray(self):
        import queue as q
        from subusage import core, tray
        from subusage.core import Snapshot, UsageWindow
        from subusage.ui import Widget

        class FakeTray:
            instances = []

            def __init__(self, events):
                self.events, self.ok, self.widget_visible, self.states, self.stopped = events, True, True, [], False
                self.ready = __import__("threading").Event()
                FakeTray.instances.append(self)

            def start(self):
                self.ready.set()

            def set_state(self, *state):
                self.states.append(state)

            def set_menu_state(self, visible, paused, startup):
                self.widget_visible, self.paused, self.startup = visible, paused, startup

            def stop(self):
                self.stopped = True

        saved = (tray.available, tray.create)
        tray.available, tray.create = (lambda: True), FakeTray
        from subusage.ui import tray_mode
        self.assertEqual(tray_mode({"tray": "both", "background": True}), "tray")
        self.assertEqual(tray_mode({"tray": "both"}), "both")
        try:
            cfg = core.load_config()
            cfg["tray"] = "tray"
            w = Widget([], cfg)
            try:
                self.pump(w.root)
                t = FakeTray.instances[-1]
                self.assertEqual(w.root.state(), "withdrawn")        # icon-only mode starts hidden
                self.assertFalse(t.widget_visible)
                t.events.put("toggle")
                w._poll_events()
                self.pump(w.root)
                self.assertTrue(w.visible)
                self.assertNotEqual(w.root.state(), "withdrawn")
                w.hide_widget()
                core.instance_messages.put("show")                  # a second launch
                w._poll_events()
                self.assertTrue(w.visible)

                w.providers = core.build_providers({"providers": [{"type": "claude", "name": "Claude"}]})
                w.providers[0].snapshot = Snapshot(windows=[UsageWindow("Weekly", 93, time.time() + 3600, 604800)],
                                                   fetched_at=time.time())
                w.render(force=True)
                tip, pct, rgb = t.states[-1]
                self.assertEqual((tip, pct, rgb), ("SubUsage\nClaude: 93%", 93, (0xe4, 0x58, 0x58)))

                t.events.put("pause")                               # tray menu: Pause updates
                w._poll_events()
                self.assertTrue(w.paused and w.scheduler.paused and t.paused)
                self.assertEqual(t.states[-1][0], "SubUsage\n⏸ updates paused\nClaude: 93%")
                self.assertIsNone(t.states[-1][1])                  # grey ring while paused
                core.instance_messages.put("start")                 # python -m subusage --resume
                w._poll_events()
                self.assertFalse(w.paused or w.scheduler.paused or t.paused)

                w.hide_widget()
                w.cfg["tray"] = "off"
                w.apply_config(dict(w.cfg))                         # turning the tray off brings the widget back
                self.assertTrue(t.stopped)
                self.assertIsNone(w.tray)
                self.assertTrue(w.visible)
                w.hide_widget()                                     # no tray: refuses to hide
                self.assertTrue(w.visible)
            finally:
                w.quit()
        finally:
            tray.available, tray.create = saved

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
