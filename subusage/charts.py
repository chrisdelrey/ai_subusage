"""Hover tooltip with a mini chart, and the per-limit History window."""
from __future__ import annotations

import datetime as dt
import time
import tkinter as tk
from typing import Optional

from . import history
from .core import UsageWindow, fmt_delta
from .ui import FONT, THEME, fsize


def draw_series(c: tk.Canvas, samples: list[history.Sample], x0: float, x1: float, y0: float, y1: float,
                t0: float, t1: float, color: str, width: int = 2) -> None:
    """Step line of used% between times t0..t1 inside the box (x0,y0)-(x1,y1); y1 is the 0% baseline."""
    if t1 <= t0 or not samples:
        return

    def xy(t: float, u: float) -> tuple[float, float]:
        return (x0 + (x1 - x0) * (t - t0) / (t1 - t0), y1 - (y1 - y0) * max(0.0, min(100.0, u)) / 100)
    pts: list[float] = []
    before: Optional[float] = None
    for t, u, _ in samples:
        if t < t0:
            before = u
            continue
        if t > t1:
            break
        if not pts and before is not None:
            pts += xy(t0, before)
        if pts:
            pts += [xy(t, 0)[0], pts[-1]]  # hold the previous level until this sample (step line)
        pts += xy(t, u)
    if len(pts) >= 4:
        c.create_line(*pts, fill=color, width=width)
    elif len(pts) == 2:
        c.create_oval(pts[0] - 2, pts[1] - 2, pts[0] + 2, pts[1] + 2, fill=color, width=0)


class RowTooltip:
    """Shows details for a limit on hover: usage, reset, projection, and this period's chart."""

    def __init__(self, widget):
        self.w = widget
        self.top: Optional[tk.Toplevel] = None
        self.after_id = None
        self.key = None

    def bind(self, widgets: list[tk.Misc], provider, label: str) -> None:
        k = (id(provider), label)
        for wd in widgets:
            wd.bind("<Enter>", lambda e, k=k: self._enter(e, k, provider, label), add="+")
            wd.bind("<Leave>", lambda e: self._leave(), add="+")

    def _enter(self, e, k, provider, label) -> None:
        if self.key == k and self.top is not None:
            self._cancel()
            return
        self.hide()
        self.key = k

        def open_tip():
            window = self.w.window_for(provider, label)  # latest data, not what was there at build time
            if window is not None:
                self.show(e.x_root, e.y_root, provider, window)
        self.after_id = self.w.root.after(450, open_tip)

    def _leave(self) -> None:
        self._cancel()
        self.after_id = self.w.root.after(150, self.hide)

    def _cancel(self) -> None:
        if self.after_id:
            self.w.root.after_cancel(self.after_id)
            self.after_id = None

    def hide(self) -> None:
        self._cancel()
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
        self.top, self.key = None, None

    def show(self, x: int, y: int, provider, window: UsageWindow) -> None:
        self.after_id = None
        now = time.time()
        key = history.provider_key(provider.type, provider.name)
        samples = history.series(key, window.label, now - (window.length_s or 7 * 86400) - 600) \
            if self.w.cfg.get("history", True) else []
        proj = history.project(window, samples, now)
        top = self.top = tk.Toplevel(self.w.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=THEME["track"])
        box = tk.Frame(top, bg=THEME["panel"], padx=10, pady=8)
        box.pack(padx=1, pady=1)

        def line(text, color=THEME["fg"], size=8, bold=False):
            tk.Label(box, text=text, fg=color, bg=THEME["panel"], justify="left",
                     font=(FONT, fsize(size), "bold" if bold else "normal")).pack(anchor="w")
        line(f"{provider.name} · {window.label}", bold=True, size=9)
        bits = []
        if window.used_pct is not None:
            bits.append(f"{window.used_pct:.1f}% used")
        if window.detail:
            bits.append(window.detail)
        if window.resets_at:
            bits.append(f"resets in {fmt_delta(window.resets_at - now)}")
        line(" · ".join(bits), THEME["muted"])
        if proj:
            src = {"recent": "last hour", "average": "since window start"}.get(proj.source, "")
            line(proj.summary(now) + (f"  ({proj.rate:.1f}%/h, {src})" if proj.rate and src else ""),
                 THEME["warn"] if proj.at_risk else THEME["ok"])
        period = history.current_period(samples, window)
        if window.resets_at and window.length_s:
            W, H = 240, 64
            c = tk.Canvas(box, width=W, height=H, bg=THEME["panel"], highlightthickness=0)
            c.pack(anchor="w", pady=(6, 0))
            t0, t1 = window.resets_at - window.length_s, window.resets_at
            c.create_rectangle(0, 0, W, H, outline=THEME["track"])
            c.create_line(0, H * 0.5, W, H * 0.5, fill=THEME["track"], dash=(2, 3))
            nx = W * (now - t0) / (t1 - t0)
            c.create_line(nx, 0, nx, H, fill=THEME["track"])
            pts = period + ([(now, window.used_pct, window.resets_at)] if window.used_pct is not None else [])
            draw_series(c, pts, 0, W, 2, H - 2, t0, t1, THEME["ok"] if not (proj and proj.at_risk) else THEME["warn"])
            if proj and proj.rate > 0 and window.used_pct is not None:
                end_t = proj.hits_at or t1
                end_u = 100 if proj.hits_at else (proj.at_reset or window.used_pct)
                ex = W * (end_t - t0) / (t1 - t0)
                ny, ey = (H - 2) - (H - 4) * window.used_pct / 100, (H - 2) - (H - 4) * end_u / 100
                c.create_line(nx, ny, ex, ey, fill=THEME["muted"], dash=(3, 3))
            if not period:
                c.create_text(W / 2, H / 2, text="history builds up as the widget runs", fill=THEME["muted"],
                              font=(FONT, fsize(7)))
        top.update_idletasks()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        tw, th = top.winfo_reqwidth(), top.winfo_reqheight()
        px = x - tw - 16 if x + tw + 16 > sw else x + 16
        top.geometry(f"+{max(0, px)}+{max(0, min(y + 12, sh - th - 40))}")


class HistoryWindow:
    """A larger chart of one limit over the last day / week / month."""

    def __init__(self, widget, provider, label: str):
        self.w, self.provider, self.label = widget, provider, label
        self.key = history.provider_key(provider.type, provider.name)
        top = self.top = tk.Toplevel(widget.root)
        top.title(f"{provider.name} · {label}: history")
        top.configure(bg=THEME["bg"])
        top.attributes("-topmost", True)
        top.bind("<Escape>", lambda _e: top.destroy())
        outer = tk.Frame(top, bg=THEME["bg"], padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=THEME["bg"])
        head.pack(fill="x")
        tk.Label(head, text=f"{provider.name} · {label}", fg=THEME["fg"], bg=THEME["bg"],
                 font=(FONT, fsize(10), "bold")).pack(side="left")
        self.range_btns = {}
        for days, name in ((30, "30 days"), (7, "7 days"), (1, "24 hours")):
            b = tk.Label(head, text=name, fg=THEME["muted"], bg=THEME["bg"], font=(FONT, fsize(8)), padx=6,
                         cursor="hand2")
            b.pack(side="right")
            b.bind("<Button-1>", lambda _e, d=days: self.draw(d))
            self.range_btns[days] = b
        self.c = tk.Canvas(outer, width=560, height=220, bg=THEME["bg"], highlightthickness=0)
        self.c.pack(pady=(8, 0))
        self.note = tk.Label(outer, text="", fg=THEME["muted"], bg=THEME["bg"], font=(FONT, fsize(8)))
        self.note.pack(anchor="w")
        self.draw(7)
        top.update_idletasks()
        top.geometry(f"+{max(0, widget.root.winfo_x() - top.winfo_reqwidth() - 12)}+{widget.root.winfo_y()}")

    def draw(self, days: int) -> None:
        for d, b in self.range_btns.items():
            b.configure(fg=THEME["fg"] if d == days else THEME["muted"])
        c = self.c
        c.delete("all")
        W, H, L, B = 560, 220, 34, 20
        now = time.time()
        t0 = now - days * 86400
        samples = history.series(self.key, self.label, t0 - 86400)
        for pct in (0, 50, 100):
            y = (H - B) - (H - B - 6) * pct / 100
            c.create_line(L, y, W, y, fill=THEME["track"], dash=() if pct == 0 else (2, 3))
            c.create_text(L - 6, y, text=f"{pct}%", anchor="e", fill=THEME["muted"], font=(FONT, fsize(7)))
        step = 86400 if days > 1 else 6 * 3600
        t = (int(t0 // step) + 1) * step
        while t < now:
            x = L + (W - L) * (t - t0) / (now - t0)
            c.create_line(x, H - B, x, H - B + 4, fill=THEME["muted"])
            d = dt.datetime.fromtimestamp(t)
            label = d.strftime("%a %d") if days > 1 else d.strftime("%H:%M")
            if days <= 7 or d.day % 5 == 0:
                c.create_text(x, H - B + 12, text=label, fill=THEME["muted"], font=(FONT, fsize(7)))
            t += step
        draw_series(c, samples, L, W, 6, H - B, t0, now, THEME["ok"])
        n = len([s for s in samples if s[0] >= t0])
        peak = max((s[1] for s in samples if s[0] >= t0), default=None)
        self.note.configure(text=f"{n} samples" + (f" · peak {peak:.0f}%" if peak is not None else "") +
                            ("" if n else " · history builds up while the widget runs"))
