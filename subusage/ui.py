"""The floating widget (tkinter)."""
from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from typing import Any, Optional

from . import APP
from .core import (IS_MAC, IS_WIN, Provider, Scheduler, Snapshot, build_providers, config_path, fmt_delta,
                   log_path, open_path, save_config, set_startup, startup_enabled)

THEME = {
    "bg": "#16181d", "panel": "#1d2027", "fg": "#e8e9ec", "muted": "#8b909a", "track": "#2b2f38",
    "ok": "#4fb37f", "warn": "#e2a336", "crit": "#e45858", "pace": "#c9ccd3", "err": "#e88a5a",
    "button": "#2b2f38", "button_hover": "#383d48", "primary": "#3d6fd6", "primary_hover": "#4c7ee6",
}
if IS_WIN:
    FONT, ICON_FONT = "Segoe UI", "Segoe UI Symbol"
elif IS_MAC:
    FONT, ICON_FONT = "Helvetica Neue", "Apple Symbols"
else:
    FONT, ICON_FONT = "DejaVu Sans", "DejaVu Sans"
SCALE = 1.25 if IS_MAC else 1.0  # macOS Tk renders point sizes smaller


def fsize(n: float) -> int:
    return int(round(n * SCALE))


def context_events() -> tuple[str, ...]:
    return ("<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)


VIEWS = (("full", "Full"), ("condensed", "Condensed"), ("compact", "Compact"))


def view_of(cfg: dict[str, Any]) -> str:
    v = cfg.get("view")
    if v in dict(VIEWS):
        return v
    return "compact" if cfg.get("compact") else "full"  # pre-2.1 configs


class Widget:
    def __init__(self, providers: list[Provider], cfg: dict[str, Any], demo: bool = False):
        self.providers, self.cfg, self.demo = providers, cfg, demo
        self.scheduler = Scheduler(providers)
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.overrideredirect(True)
        self.root.configure(bg=THEME["bg"])
        self.root.attributes("-topmost", bool(cfg.get("always_on_top", True)))
        try:
            self.root.attributes("-alpha", float(cfg.get("opacity", 0.95)))
        except tk.TclError:
            pass
        self.width = int(cfg.get("width", 300))
        self.body = tk.Frame(self.root, bg=THEME["bg"], padx=12, pady=10)
        self.body.pack(fill="both", expand=True)
        self._drag = (0, 0)
        self._last_sig: Any = None
        self._settings = None
        self.update_info = None
        self.update_error = ""
        self._build_menu()
        for ev in context_events():
            self.root.bind(ev, self._popup)
        self.render(force=True)
        self._place()
        self.scheduler.start()
        self.root.after(1000, self._tick)
        if not demo:
            self.root.after(15_000, self._auto_check)

    # ----- window plumbing
    def _place(self) -> None:
        self.root.update_idletasks()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if x is None or y is None or not (0 <= x < sw - 40 and 0 <= y < sh - 40):
            x, y = sw - self.width - 24, 48
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _bind_drag(self, w) -> None:
        if getattr(w, "_nodrag", False):
            return
        w.bind("<ButtonPress-1>", self._start_drag)
        w.bind("<B1-Motion>", self._on_drag)
        w.bind("<ButtonRelease-1>", self._end_drag)
        for ev in context_events():
            w.bind(ev, self._popup)
        for c in w.winfo_children():
            self._bind_drag(c)

    def _start_drag(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _end_drag(self, _e) -> None:
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        if not self.demo:
            save_config(self.cfg)

    def _build_menu(self) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Refresh now", command=self.scheduler.refresh_all)
        m.add_command(label="Settings…", command=self.open_settings)
        m.add_command(label="Add subscription…", command=lambda: self.open_settings(picker=True))
        m.add_separator()
        self.v_top = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", True)))
        self.v_view = tk.StringVar(value=view_of(self.cfg))
        views = tk.Menu(m, tearoff=0)
        for key, label in VIEWS:
            views.add_radiobutton(label=label, value=key, variable=self.v_view, command=self._set_view)
        m.add_cascade(label="View", menu=views)
        m.add_checkbutton(label="Always on top", variable=self.v_top, command=self._toggle_top)
        op = tk.Menu(m, tearoff=0)
        for v in (1.0, 0.95, 0.85, 0.75, 0.6):
            op.add_command(label=f"{int(v * 100)}%", command=lambda v=v: self._set_opacity(v))
        m.add_cascade(label="Opacity", menu=op)
        links = tk.Menu(m, tearoff=0)
        for p in self.providers:
            links.add_command(label=p.name, command=lambda p=p: p.usage_url and webbrowser.open(p.usage_url))
        m.add_cascade(label="Open usage page", menu=links)
        self.v_startup = tk.BooleanVar(value=startup_enabled())
        m.add_checkbutton(label="Start at login", variable=self.v_startup, command=self._toggle_startup)
        m.add_separator()
        m.add_command(label="Open config file", command=lambda: open_path(config_path()))
        m.add_command(label="Open log", command=lambda: open_path(log_path()))
        m.add_command(label="About & updates…", command=lambda: self.open_settings(about=True))
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)
        self.menu = m

    def _popup(self, e) -> None:
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_top(self) -> None:
        self.cfg["always_on_top"] = self.v_top.get()
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        save_config(self.cfg)

    def _set_view(self) -> None:
        self.cfg["view"] = self.v_view.get()
        self.cfg.pop("compact", None)
        save_config(self.cfg)
        self.render(force=True)

    def _set_opacity(self, v: float) -> None:
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)
        save_config(self.cfg)

    def _toggle_startup(self) -> None:
        try:
            set_startup(self.v_startup.get())
        except OSError as e:
            self.v_startup.set(not self.v_startup.get())
            from .core import log
            log(f"start at login failed: {e}")

    def quit(self) -> None:
        if not self.demo:
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
            save_config(self.cfg)
        self.root.destroy()

    # ----- settings
    def open_settings(self, picker: bool = False, about: bool = False) -> None:
        from .settings import SettingsDialog
        if self._settings and self._settings.alive():
            self._settings.top.lift()
            self._settings.top.focus_force()
        else:
            self._settings = SettingsDialog(self)
        if picker:
            self._settings.open_picker()
        if about:
            self._settings.show_about()

    def apply_config(self, cfg: dict[str, Any]) -> None:
        cfg["x"], cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.cfg.clear()
        self.cfg.update(cfg)
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        try:
            self.root.attributes("-alpha", float(self.cfg.get("opacity", 0.95)))
        except tk.TclError:
            pass
        self.width = int(self.cfg.get("width", 300))
        if not self.demo:
            save_config(self.cfg)
            self.providers = build_providers(self.cfg)
            self.scheduler.providers = self.providers
            self.scheduler.wake.set()
        self._build_menu()
        self.render(force=True)

    # ----- updates
    def _auto_check(self) -> None:
        if self.cfg.get("check_updates", True):
            self.check_updates()
        self.root.after(24 * 3600 * 1000, self._auto_check)

    def check_updates(self, on_done=None) -> None:
        from . import update

        def work():
            try:
                self.update_info, self.update_error = update.check(), ""
            except Exception as e:  # offline, rate limited...
                self.update_error = str(e)
                from .core import log
                log(f"update check failed: {e}")
            self._last_sig = None  # re-render footer
            if on_done:
                self.root.after(0, on_done)
        threading.Thread(target=work, daemon=True).start()

    # ----- rendering
    def _tick(self) -> None:
        self.render()
        self.root.after(1000, self._tick)

    def _signature(self):
        parts: list[Any] = [int(time.time() // 60), view_of(self.cfg), self.width,
                            bool(self.update_info and self.update_info.available)]
        for p in self.providers:
            with p.lock:
                s = p.snapshot
                parts.append((id(p), p.configured, s.fetched_at, s.error, len(s.windows)))
        return tuple(parts)

    def render(self, force: bool = False) -> None:
        sig = self._signature()
        if not force and sig == self._last_sig:
            return
        self._last_sig = sig
        for c in self.body.winfo_children():
            c.destroy()
        visible = [p for p in self.providers if p.configured or not self.cfg.get("hide_unconfigured", True)]
        if not visible:
            self._label(self.body, "No subscriptions found yet.\nClick ⚙ to add one or point the widget\n"
                        "at your Claude Code, Codex, Copilot… login.", THEME["muted"], 9).pack(anchor="w")
        view = view_of(self.cfg)
        self.body.configure(pady=10 if view == "full" else 7, padx=12 if view == "full" else 10)
        draw = {"full": self._render_full, "condensed": self._render_condensed, "compact": self._render_compact}[view]
        for i, p in enumerate(visible):
            if i:
                if view == "full":
                    tk.Frame(self.body, bg=THEME["track"], height=1).pack(fill="x", pady=(8, 8))
                else:
                    tk.Frame(self.body, bg=THEME["bg"], height=4 if view == "compact" else 7).pack(fill="x")
            draw(p)
        self._footer()
        self._bind_drag(self.body)

    def _footer(self) -> None:
        foot = tk.Frame(self.body, bg=THEME["bg"])
        foot.pack(fill="x", pady=(6, 0))
        info = self.update_info
        if info and info.available:
            up = tk.Label(foot, text=f"⬆ Update {info.latest}", fg=THEME["primary_hover"], bg=THEME["bg"],
                          font=(FONT, fsize(8), "bold"), cursor="hand2")
            up._nodrag = True  # type: ignore[attr-defined]
            up.pack(side="left")
            up.bind("<Button-1>", lambda _e: self.open_settings(about=True))
        for text, cmd in (("⚙", self.open_settings), ("⟳", self.scheduler.refresh_all)):
            b = tk.Label(foot, text=text, fg=THEME["muted"], bg=THEME["bg"], font=(ICON_FONT, fsize(11)),
                         cursor="hand2")
            b._nodrag = True  # type: ignore[attr-defined]
            b.pack(side="right", padx=(8, 0))
            b.bind("<Button-1>", lambda _e, c=cmd: c())
            b.bind("<Enter>", lambda _e, b=b: b.configure(fg=THEME["fg"]))
            b.bind("<Leave>", lambda _e, b=b: b.configure(fg=THEME["muted"]))

    def _label(self, parent, text, color, size=9, bold=False, **kw):
        return tk.Label(parent, text=text, fg=color, bg=THEME["bg"], justify="left",
                        font=(FONT, fsize(size), "bold" if bold else "normal"), **kw)

    def _color(self, pct: Optional[float]) -> str:
        if pct is None:
            return THEME["muted"]
        if pct >= float(self.cfg.get("crit_pct", 90)):
            return THEME["crit"]
        if pct >= float(self.cfg.get("warn_pct", 70)):
            return THEME["warn"]
        return THEME["ok"]

    def _header(self, p: Provider, snap: Snapshot):
        row = tk.Frame(self.body, bg=THEME["bg"])
        row.pack(fill="x")
        dot = tk.Canvas(row, width=8, height=8, bg=THEME["bg"], highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=p.accent, width=0)
        dot.pack(side="left", padx=(0, 6))
        self._label(row, p.name, THEME["fg"], 10, True).pack(side="left")
        if snap.plan:
            self._label(row, f"  {snap.plan}", THEME["muted"], 9).pack(side="left")
        status = ""
        if p.busy and not snap.fetched_at:
            status = "loading…"
        elif snap.fetched_at:
            age = time.time() - snap.fetched_at
            status = "just now" if age < 60 else f"{fmt_delta(age)} ago"
        self._label(row, status, THEME["muted"], 8).pack(side="right")
        row.bind("<Double-Button-1>", lambda _e: p.usage_url and webbrowser.open(p.usage_url))
        return row

    def _render_full(self, p: Provider) -> None:
        with p.lock:
            snap = p.snapshot
        self._header(p, snap)
        now = time.time()
        bar_w = self.width - 24
        for w in snap.windows:
            line = tk.Frame(self.body, bg=THEME["bg"])
            line.pack(fill="x", pady=(5, 0))
            self._label(line, w.label, THEME["muted"], 9).pack(side="left")
            if w.resets_at:
                left = w.resets_at - now
                self._label(line, f"  ↻ {fmt_delta(left)}" if left > 60 else "  resetting",
                            THEME["muted"], 9).pack(side="right")
            elif w.detail:
                self._label(line, f"  {w.detail}", THEME["muted"], 9).pack(side="right")
            if w.used_pct is not None:
                self._label(line, f"{w.used_pct:.0f}%", self._color(w.used_pct), 9, True).pack(side="right")
            c = tk.Canvas(self.body, width=bar_w, height=6, bg=THEME["bg"], highlightthickness=0)
            c.pack(anchor="w", pady=(2, 0))
            c.create_rectangle(0, 0, bar_w, 6, fill=THEME["track"], width=0)
            if w.used_pct is not None:
                c.create_rectangle(0, 0, bar_w * max(0.0, min(1.0, w.used_pct / 100)), 6,
                                   fill=self._color(w.used_pct), width=0)
            # Pace marker: where you'd be if you used the window evenly until it resets.
            if w.resets_at and w.length_s:
                elapsed = 1 - (w.resets_at - now) / w.length_s
                if 0 < elapsed < 1:
                    x = int(bar_w * elapsed)
                    c.create_rectangle(x - 1, 0, x + 1, 6, fill=THEME["pace"], width=0)
            if w.detail and w.resets_at:
                self._label(self.body, w.detail, THEME["muted"], 8).pack(anchor="w")
        for n in snap.notes:
            self._label(self.body, n, THEME["muted"], 8).pack(anchor="w", pady=(3, 0))
        if snap.error:
            self._label(self.body, f"⚠ {snap.error}", THEME["err"], 8, wraplength=bar_w).pack(anchor="w", pady=(4, 0))

    def _render_condensed(self, p: Provider) -> None:
        """One tight row per window: label · mini bar · % · reset."""
        with p.lock:
            snap = p.snapshot
        head = tk.Frame(self.body, bg=THEME["bg"])
        head.pack(fill="x")
        dot = tk.Canvas(head, width=7, height=7, bg=THEME["bg"], highlightthickness=0)
        dot.create_oval(0, 0, 7, 7, fill=p.accent, width=0)
        dot.pack(side="left", padx=(0, 5))
        self._label(head, p.name, THEME["fg"], 9, True).pack(side="left")
        if snap.plan:
            self._label(head, f" {snap.plan}", THEME["muted"], 8).pack(side="left")
        if snap.error:
            self._label(head, "⚠", THEME["err"], 9).pack(side="right")
        head.bind("<Double-Button-1>", lambda _e: p.usage_url and webbrowser.open(p.usage_url))

        font_t, bold_t = (FONT, fsize(8)), (FONT, fsize(8), "bold")
        font = tkfont.Font(root=self.root, font=font_t)   # for measuring only
        bold = tkfont.Font(root=self.root, font=bold_t)
        W = self.width - 20
        row_h = max(14, font.metrics("linespace") + 2)
        label_w = int(W * 0.36)
        pct_w = bold.measure("100%") + 6
        reset_w = font.measure("↻ 22h 59m") + 2
        bar_x0, bar_x1 = label_w, W - pct_w - reset_w - 6
        now = time.time()
        for w in snap.windows:
            c = tk.Canvas(self.body, width=W, height=row_h, bg=THEME["bg"], highlightthickness=0)
            c.pack(anchor="w")
            text = w.label
            while text and font.measure(text) > label_w - 6:
                text = text[:-2] + "…" if len(text) > 2 else ""
            mid = row_h // 2
            c.create_text(0, mid, text=text, anchor="w", fill=THEME["muted"], font=font_t)
            c.create_rectangle(bar_x0, mid - 2, bar_x1, mid + 2, fill=THEME["track"], width=0)
            color = self._color(w.used_pct)
            if w.used_pct is not None:
                c.create_rectangle(bar_x0, mid - 2, bar_x0 + (bar_x1 - bar_x0) * max(0.0, min(1.0, w.used_pct / 100)),
                                   mid + 2, fill=color, width=0)
                c.create_text(bar_x1 + 6 + pct_w - 4, mid, text=f"{w.used_pct:.0f}%", anchor="e", fill=color, font=bold_t)
            if w.resets_at and w.length_s:
                elapsed = 1 - (w.resets_at - now) / w.length_s
                if 0 < elapsed < 1:
                    x = bar_x0 + int((bar_x1 - bar_x0) * elapsed)
                    c.create_rectangle(x, mid - 3, x + 1, mid + 3, fill=THEME["pace"], width=0)
            if w.resets_at:
                left = w.resets_at - now
                c.create_text(W, mid, text=f"↻ {fmt_delta(left)}" if left > 60 else "now", anchor="e",
                              fill=THEME["muted"], font=font_t)
        if snap.error:
            self._label(self.body, snap.error, THEME["err"], 7, wraplength=W).pack(anchor="w")

    def _render_compact(self, p: Provider) -> None:
        with p.lock:
            snap = p.snapshot
        row = self._header(p, snap)
        for child in row.pack_slaves()[2:]:
            child.destroy()
        nums = tk.Frame(row, bg=THEME["bg"])
        nums.pack(side="right")
        for w in snap.windows[:3]:
            if w.used_pct is not None:
                self._label(nums, f" {w.used_pct:.0f}%", self._color(w.used_pct), 10, True).pack(side="left")
        if snap.error:
            self._label(nums, " ⚠", THEME["err"], 10).pack(side="left")

    def run(self) -> None:
        self.root.mainloop()
