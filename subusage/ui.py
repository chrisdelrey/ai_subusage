"""The floating widget (tkinter)."""
from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from typing import Any, Optional

from . import APP
from .core import (FETCH_HOOKS, IS_MAC, IS_WIN, Provider, Scheduler, Snapshot, UsageWindow, build_providers,
                   config_path, fmt_delta, log, log_path, open_path, save_config, set_startup, startup_enabled)

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
SORTS = (("manual", "As configured"), ("tightest", "Closest to limit first"))
EXPIRED = (("hide", "Hide until used again"), ("dim", "Show dimmed"), ("show", "Show the error"))


def view_of(cfg: dict[str, Any]) -> str:
    v = cfg.get("view")
    if v in dict(VIEWS):
        return v
    return "compact" if cfg.get("compact") else "full"  # pre-2.1 configs


def visible_windows(p: Provider, snap: Snapshot) -> list[UsageWindow]:
    hidden = set(p.cfg.get("hidden") or [])
    return [w for w in snap.windows if w.label not in hidden]


class Widget:
    def __init__(self, providers: list[Provider], cfg: dict[str, Any], demo: bool = False):
        from .charts import RowTooltip
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
        self._struct: Any = None
        self._vals: Any = None
        self._updaters: list = []
        self._settings = None
        self._proj_cache: dict[tuple, Any] = {}
        self.update_info = None
        self.update_error = ""
        self.tooltip = RowTooltip(self)
        self._hook = self._make_fetch_hook(cfg)
        if not demo:
            FETCH_HOOKS.append(self._hook)
            threading.Thread(target=self._prune_history, daemon=True).start()
        for ev in context_events():
            self.root.bind(ev, lambda e: self._popup(e, None))
        self.render(force=True)
        self._place()
        self.scheduler.start()
        self._timers = {"tick": self.root.after(1000, self._tick)}
        if not demo:
            self._timers["update"] = self.root.after(15_000, self._auto_check)
            self._timers["whatsnew"] = self.root.after(1500, self._whats_new)

    # ----- history / projections
    @staticmethod
    def _make_fetch_hook(cfg: dict[str, Any]):
        # Runs on fetch threads, so it must not hold a reference to the widget: if a worker thread dropped
        # the last reference, Tk objects would be destroyed off the main thread (Tcl aborts on that).
        def on_fetch(p: Provider, snap: Snapshot) -> None:
            if cfg.get("history", True):
                from . import history
                history.record(history.provider_key(p.type, p.name), snap)
        return on_fetch

    @staticmethod
    def _prune_history() -> None:
        from . import history
        history.prune()

    def projection(self, p: Provider, w: UsageWindow, fetched_at: Optional[float]):
        """Cached per fetch; None when projections are off or there isn't enough data."""
        if not self.cfg.get("projections", True) or w.used_pct is None:
            return None
        k = (id(p), w.label, fetched_at, int(time.time() // 60))
        if k not in self._proj_cache:
            from . import history
            samples = []
            if self.cfg.get("history", True) and not self.demo:
                samples = history.series(history.provider_key(p.type, p.name), w.label,
                                         time.time() - history.RECENT_S - 600)
            if len(self._proj_cache) > 500:
                self._proj_cache.clear()
            self._proj_cache[k] = history.project(w, samples)
        return self._proj_cache[k]

    # ----- window plumbing
    def _place(self) -> None:
        self.root.update_idletasks()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if x is None or y is None or not (0 <= x < sw - 40 and 0 <= y < sh - 40):
            x, y = sw - self.width - 24, 48
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _bind_drag(self, w, ctx=None) -> None:
        """Make w (and children) drag the window; right-click opens a menu for the row under the mouse."""
        ctx = getattr(w, "_ctx", ctx)
        if getattr(w, "_nodrag", False):
            return
        w.bind("<ButtonPress-1>", self._start_drag)
        w.bind("<B1-Motion>", self._on_drag)
        w.bind("<ButtonRelease-1>", self._end_drag)
        for ev in context_events():
            w.bind(ev, lambda e, c=ctx: self._popup(e, c))
        for c in w.winfo_children():
            self._bind_drag(c, ctx)

    def _start_drag(self, e) -> None:
        self.tooltip.hide()
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _end_drag(self, _e) -> None:
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        self._save()

    def _save(self) -> None:
        if not self.demo:
            save_config(self.cfg)

    # ----- menus
    def _popup(self, e, ctx) -> None:
        self.tooltip.hide()
        m = tk.Menu(self.root, tearoff=0)
        if ctx:
            p, label = ctx
            if p in self.providers:
                if label:
                    m.add_command(label=f"Hide “{label}”", command=lambda: self.hide_window(p, label))
                    m.add_command(label=f"History of “{label}”…", command=lambda: self.open_history(p, label))
                if p.parked:
                    m.add_command(label=f"{p.name}: {p.park_reason}"[:90], state="disabled")
                    m.add_command(label="Try again now", command=self.scheduler.refresh_all)
                collapsed = bool(p.cfg.get("collapsed"))
                m.add_command(label=f"{'Expand' if collapsed else 'Collapse'} {p.name}",
                              command=lambda: self.toggle_collapse(p))
                if p.cfg.get("hidden"):
                    m.add_command(label=f"Show hidden limits ({len(p.cfg['hidden'])})",
                                  command=lambda: self.unhide_all(p))
                if p.usage_url:
                    m.add_command(label=f"Open {p.name} usage page", command=lambda: webbrowser.open(p.usage_url))
                m.add_separator()
        self._fill_menu(m)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _fill_menu(self, m: tk.Menu) -> None:
        m.add_command(label="Refresh now", command=self.scheduler.refresh_all)
        m.add_command(label="Settings…", command=self.open_settings)
        m.add_command(label="Add subscription…", command=lambda: self.open_settings(picker=True))
        m.add_separator()
        self.v_view = tk.StringVar(value=view_of(self.cfg))
        views = tk.Menu(m, tearoff=0)
        for key, label in VIEWS:
            views.add_radiobutton(label=label, value=key, variable=self.v_view, command=self._set_view)
        m.add_cascade(label="View", menu=views)
        self.v_sort = tk.StringVar(value=self.cfg.get("sort", "manual"))
        sorts = tk.Menu(m, tearoff=0)
        for key, label in SORTS:
            sorts.add_radiobutton(label=label, value=key, variable=self.v_sort, command=self._set_sort)
        m.add_cascade(label="Sort", menu=sorts)
        self.v_top = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", True)))
        m.add_checkbutton(label="Always on top", variable=self.v_top, command=self._toggle_top)
        op = tk.Menu(m, tearoff=0)
        for v in (1.0, 0.95, 0.85, 0.75, 0.6):
            op.add_command(label=f"{int(v * 100)}%", command=lambda v=v: self._set_opacity(v))
        m.add_cascade(label="Opacity", menu=op)
        if any(p.cfg.get("collapsed") for p in self.providers):
            m.add_command(label="Expand all", command=self.expand_all)
        self.v_startup = tk.BooleanVar(value=startup_enabled())
        m.add_checkbutton(label="Start at login", variable=self.v_startup, command=self._toggle_startup)
        m.add_separator()
        m.add_command(label="Open config file", command=lambda: open_path(config_path()))
        m.add_command(label="Open log", command=lambda: open_path(log_path()))
        m.add_command(label="About & updates…", command=lambda: self.open_settings(about=True))
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)

    # ----- declutter actions
    def hide_window(self, p: Provider, label: str) -> None:
        hidden = list(p.cfg.get("hidden") or [])
        if label not in hidden:
            hidden.append(label)
        p.cfg["hidden"] = hidden
        self._save()
        self.render(force=True)

    def unhide_all(self, p: Provider) -> None:
        p.cfg.pop("hidden", None)
        self._save()
        self.render(force=True)

    def toggle_collapse(self, p: Provider) -> None:
        if p.cfg.get("collapsed"):
            p.cfg.pop("collapsed", None)
        else:
            p.cfg["collapsed"] = True
        self._save()
        self.render(force=True)

    def expand_all(self) -> None:
        for p in self.providers:
            p.cfg.pop("collapsed", None)
        self._save()
        self.render(force=True)

    def open_history(self, p: Provider, label: str) -> None:
        from .charts import HistoryWindow
        HistoryWindow(self, p, label)

    # ----- simple settings toggles
    def _toggle_top(self) -> None:
        self.cfg["always_on_top"] = self.v_top.get()
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self._save()

    def _set_view(self) -> None:
        self.cfg["view"] = self.v_view.get()
        self.cfg.pop("compact", None)
        self._save()
        self.render(force=True)

    def _set_sort(self) -> None:
        self.cfg["sort"] = self.v_sort.get()
        self._save()
        self.render(force=True)

    def _set_opacity(self, v: float) -> None:
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v)
        self._save()

    def _toggle_startup(self) -> None:
        try:
            set_startup(self.v_startup.get())
        except OSError as e:
            self.v_startup.set(not self.v_startup.get())
            log(f"start at login failed: {e}")

    def quit(self) -> None:
        for t in self._timers.values():
            try:
                self.root.after_cancel(t)
            except tk.TclError:
                pass
        if self._hook in FETCH_HOOKS:
            FETCH_HOOKS.remove(self._hook)
        if not self.demo:
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
            save_config(self.cfg)
        self.root.destroy()

    # ----- settings
    def open_settings(self, picker: bool = False, about: bool = False) -> None:
        from .settings import SettingsDialog
        self.tooltip.hide()
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
        self._proj_cache.clear()
        self.render(force=True)

    # ----- updates
    def _whats_new(self) -> None:
        """After an upgrade (in-app, git pull or pip), show that version's release notes once."""
        from . import __version__, update
        pending = self.cfg.pop("pending_whats_new", None)
        last = self.cfg.get("last_seen_version")
        self.cfg["last_seen_version"] = __version__
        self._save()
        since = (pending or {}).get("from") or last
        if not since or update.parse_version(__version__) <= update.parse_version(since):
            return  # first run, or no newer version: nothing to announce
        notes = ""
        local = update.local_changelog()
        if local:
            notes = update.notes_between(local, since, __version__)
        if not notes and pending:
            notes = pending.get("notes") or ""

        def show(text):
            if isinstance(text, str) and text.strip():
                from .settings import notes_dialog
                notes_dialog(self.root, f"What's new in SubUsage {__version__}", text,
                             footer=f"Updated from {since}")
        if notes:
            show(notes)
        else:  # pip installs don't ship CHANGELOG.md: fetch it for this version
            from .core import bg
            bg(lambda: update.notes_between(update.remote_changelog(f"v{__version__}") or "", since, __version__),
               show, self.root.after)

    def _auto_check(self) -> None:
        if self.cfg.get("check_updates", True):
            self.check_updates()
        self._timers["update"] = self.root.after(24 * 3600 * 1000, self._auto_check)

    def check_updates(self, on_done=None) -> None:
        from . import update

        def work():
            try:
                self.update_info, self.update_error = update.check(), ""
            except Exception as e:  # offline, rate limited...
                self.update_error = str(e)
                log(f"update check failed: {e}")
            self._struct = None  # rebuild so the footer shows/hides the update link
            if on_done:
                self.root.after(0, on_done)
        threading.Thread(target=work, daemon=True).start()

    # ----- rendering
    #
    # The layout is built once and then updated in place (text, colors, bar lengths), so the widget never
    # flashes on a refresh. It's rebuilt only when its *structure* changes (subscriptions added/removed,
    # collapsed, a limit hidden, view switched...), and then the new layout is assembled off-screen first.
    def _tick(self) -> None:
        self.render()
        self._timers["tick"] = self.root.after(1000, self._tick)

    def _snap(self, p: Provider) -> Snapshot:
        with p.lock:
            return p.snapshot

    def _win(self, p: Provider, label: str) -> Optional[UsageWindow]:
        for w in self._snap(p).windows:
            if w.label == label:
                return w
        return None

    def window_for(self, p: Provider, label: str) -> Optional[UsageWindow]:
        return self._win(p, label)

    def _tightness(self, p: Provider) -> tuple:
        snap = self._snap(p)
        wins = visible_windows(p, snap)
        risk = any((pr := self.projection(p, w, snap.fetched_at)) is not None and pr.at_risk for w in wins)
        return (not risk, -max((w.used_pct or 0) for w in wins) if wins else 0)

    def _visible(self) -> list[Provider]:
        expired = self.cfg.get("expired_display", "hide")
        visible = [p for p in self.providers
                   if (p.configured or not self.cfg.get("hide_unconfigured", True))
                   and not (p.parked and expired == "hide")]
        if self.cfg.get("sort") == "tightest":
            visible.sort(key=self._tightness)
        if expired == "dim":
            visible.sort(key=lambda p: p.parked)  # stable: dimmed ones sink to the bottom
        return visible

    def _structure(self, visible: list[Provider]) -> tuple:
        view, expired = view_of(self.cfg), self.cfg.get("expired_display", "hide")
        parts: list[Any] = [view, self.width, expired, bool(self.update_info and self.update_info.available)]
        for p in visible:
            snap = self._snap(p)
            wins = visible_windows(p, snap)
            if p.parked and expired == "dim":
                parts.append((id(p), "parked"))
                continue
            mode = "compact" if (p.cfg.get("collapsed") or view == "compact") else view
            if mode == "full":
                rows = tuple((w.label, bool(w.detail and w.resets_at),
                              bool((pr := self.projection(p, w, snap.fetched_at)) and pr.at_risk)) for w in wins)
            elif mode == "condensed":
                rows = tuple(w.label for w in wins)
            else:
                rows = tuple(w.label for w in wins[:3] if w.used_pct is not None)
            parts.append((id(p), mode, bool(p.cfg.get("collapsed")), snap.plan, rows, len(snap.notes),
                          bool(snap.error)))
        return tuple(parts)

    def _values(self) -> tuple:
        return (int(time.time() // 30),) + tuple(
            (id(p), p.busy, s.fetched_at, s.error, tuple((w.label, w.used_pct, w.resets_at) for w in s.windows))
            for p in self.providers for s in (self._snap(p),))

    def render(self, force: bool = False) -> None:
        visible = self._visible()
        struct = self._structure(visible)
        rebuilt = False
        if force or struct != self._struct:
            self._rebuild(visible)
            self._struct, rebuilt = struct, True
        vals = self._values()
        if rebuilt or vals != self._vals:
            self._vals = vals
            for update in self._updaters:
                try:
                    update()
                except tk.TclError:
                    pass

    def _rebuild(self, visible: list[Provider]) -> None:
        self.rebuilds = getattr(self, "rebuilds", 0) + 1
        self.tooltip.hide()
        self._updaters = []
        view = view_of(self.cfg)
        old = self.body
        self.body = tk.Frame(self.root, bg=THEME["bg"], padx=12 if view == "full" else 10,
                             pady=10 if view == "full" else 7)
        if not visible:
            self._label(self.body, "No subscriptions found yet.\nClick ⚙ to add one or point the widget\n"
                        "at your Claude Code, Codex, Copilot… login.", THEME["muted"], 9).pack(anchor="w")
        expired = self.cfg.get("expired_display", "hide")
        draw = {"full": self._render_full, "condensed": self._render_condensed, "compact": self._render_compact}[view]
        for i, p in enumerate(visible):
            if i:
                if view == "full":
                    tk.Frame(self.body, bg=THEME["track"], height=1).pack(fill="x", pady=(8, 8))
                else:
                    tk.Frame(self.body, bg=THEME["bg"], height=4 if view == "compact" else 7).pack(fill="x")
            if p.parked and expired == "dim":
                self._render_parked(p)
            else:
                (self._render_compact if p.cfg.get("collapsed") else draw)(p)
        self._footer()
        self._bind_drag(self.body)
        # Swap in one go: Tk only repaints when idle, so the user never sees an empty frame.
        old.destroy()
        self.body.pack(fill="both", expand=True)

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
        return tk.Label(parent, text=text, fg=color, bg=kw.pop("bg", THEME["bg"]), justify="left",
                        font=(FONT, fsize(size), "bold" if bold else "normal"), **kw)

    @staticmethod
    def _set(widget: tk.Label, **kw) -> None:
        """configure() only what changed: avoids needless redraws."""
        if any(widget.cget(k) != v for k, v in kw.items()):
            widget.configure(**kw)

    def _color(self, pct: Optional[float]) -> str:
        if pct is None:
            return THEME["muted"]
        if pct >= float(self.cfg.get("crit_pct", 90)):
            return THEME["crit"]
        if pct >= float(self.cfg.get("warn_pct", 70)):
            return THEME["warn"]
        return THEME["ok"]

    def _chevron(self, parent, p: Provider, size: int = 9) -> None:
        ch = tk.Label(parent, text="▸" if p.cfg.get("collapsed") else "▾", fg=THEME["muted"], bg=THEME["bg"],
                      font=(ICON_FONT, fsize(size)), cursor="hand2", padx=0)
        ch._nodrag = True  # type: ignore[attr-defined]
        ch.pack(side="left", padx=(0, 3))
        ch.bind("<Button-1>", lambda _e: self.toggle_collapse(p))
        for ev in context_events():
            ch.bind(ev, lambda e: self._popup(e, (p, None)))

    def _header(self, p: Provider, snap: Snapshot, size: int = 10, plan: bool = True):
        row = tk.Frame(self.body, bg=THEME["bg"])
        row._ctx = (p, None)  # type: ignore[attr-defined]
        row.pack(fill="x")
        self._chevron(row, p, size - 1)
        dot = tk.Canvas(row, width=8, height=8, bg=THEME["bg"], highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=p.accent, width=0)
        dot.pack(side="left", padx=(0, 6))
        self._label(row, p.name, THEME["fg"], size, True).pack(side="left")
        if plan and snap.plan:
            self._label(row, f"  {snap.plan}", THEME["muted"], size - 1).pack(side="left")
        row.bind("<Double-Button-1>", lambda _e: p.usage_url and webbrowser.open(p.usage_url))
        return row

    def _status_text(self, p: Provider, snap: Snapshot) -> str:
        if p.busy and not snap.fetched_at:
            return "loading…"
        if snap.fetched_at:
            age = time.time() - snap.fetched_at
            return "just now" if age < 60 else f"{fmt_delta(age)} ago"
        return ""

    def _notes_and_error(self, p: Provider, snap: Snapshot, wrap: int) -> None:
        for i in range(len(snap.notes)):
            lab = self._label(self.body, "", THEME["muted"], 8)
            lab.pack(anchor="w", pady=(3, 0))
            self._updaters.append(lambda lab=lab, i=i: self._set(
                lab, text=(self._snap(p).notes[i] if i < len(self._snap(p).notes) else "")))
        if snap.error:
            lab = self._label(self.body, "", THEME["err"], 8, wraplength=wrap)
            lab.pack(anchor="w", pady=(4, 0))
            self._updaters.append(lambda lab=lab: self._set(lab, text=f"⚠ {self._snap(p).error}"
                                                              if self._snap(p).error else ""))

    def _render_full(self, p: Provider) -> None:
        snap = self._snap(p)
        head = self._header(p, snap)
        status = self._label(head, "", THEME["muted"], 8)
        status.pack(side="right")
        self._updaters.append(lambda: self._set(status, text=self._status_text(p, self._snap(p))))
        bar_w = self.width - 24
        for w in visible_windows(p, snap):
            group = tk.Frame(self.body, bg=THEME["bg"])
            group._ctx = (p, w.label)  # type: ignore[attr-defined]
            group.pack(fill="x")
            line = tk.Frame(group, bg=THEME["bg"])
            line.pack(fill="x", pady=(5, 0))
            self._label(line, w.label, THEME["muted"], 9).pack(side="left")
            right = self._label(line, "", THEME["muted"], 9)
            right.pack(side="right")
            pct = self._label(line, "", THEME["ok"], 9, True)
            pct.pack(side="right")
            c = tk.Canvas(group, width=bar_w, height=6, bg=THEME["bg"], highlightthickness=0)
            c.pack(anchor="w", pady=(2, 0))
            c.create_rectangle(0, 0, bar_w, 6, fill=THEME["track"], width=0)
            fill = c.create_rectangle(0, 0, 0, 6, fill=THEME["ok"], width=0)
            pace = c.create_rectangle(0, 0, 0, 6, fill=THEME["pace"], width=0, state="hidden")
            detail = warn = None
            if w.detail and w.resets_at:
                detail = self._label(group, "", THEME["muted"], 8)
                detail.pack(anchor="w")
            proj = self.projection(p, w, snap.fetched_at)
            if proj and proj.at_risk:
                warn = self._label(group, "", THEME["warn"], 8)
                warn.pack(anchor="w")

            def update(label=w.label, right=right, pct=pct, c=c, fill=fill, pace=pace, detail=detail, warn=warn):
                win = self._win(p, label)
                if win is None:
                    return
                now = time.time()
                if win.resets_at:
                    left = win.resets_at - now
                    self._set(right, text=f"  ↻ {fmt_delta(left)}" if left > 60 else "  resetting")
                else:
                    self._set(right, text=f"  {win.detail}" if win.detail else "")
                color = self._color(win.used_pct)
                if win.used_pct is not None:
                    self._set(pct, text=f"{win.used_pct:.0f}%", fg=color)
                    c.coords(fill, 0, 0, bar_w * max(0.0, min(1.0, win.used_pct / 100)), 6)
                    c.itemconfigure(fill, fill=color)
                else:
                    self._set(pct, text="")
                    c.coords(fill, 0, 0, 0, 6)
                # Pace marker: where you'd be if you used the window evenly until it resets.
                elapsed = 1 - (win.resets_at - now) / win.length_s if win.resets_at and win.length_s else -1
                if 0 < elapsed < 1:
                    x = int(bar_w * elapsed)
                    c.coords(pace, x - 1, 0, x + 1, 6)
                    c.itemconfigure(pace, state="normal")
                else:
                    c.itemconfigure(pace, state="hidden")
                if detail is not None:
                    self._set(detail, text=win.detail)
                if warn is not None:
                    pr = self.projection(p, win, self._snap(p).fetched_at)
                    self._set(warn, text=f"⚠ {pr.summary(now)}" if pr and pr.at_risk else "")
            self._updaters.append(update)
            self.tooltip.bind(self._all_widgets(group), p, w.label)
        self._notes_and_error(p, snap, bar_w)

    def _render_condensed(self, p: Provider) -> None:
        """One tight row per limit: label · mini bar · % · reset (or ⚠ time-to-limit)."""
        snap = self._snap(p)
        head = self._header(p, snap, size=9)
        if snap.error:
            err = self._label(head, "⚠", THEME["err"], 9)
            err.pack(side="right")
        font_t, bold_t = (FONT, fsize(8)), (FONT, fsize(8), "bold")
        font = tkfont.Font(root=self.root, font=font_t)   # for measuring only
        bold = tkfont.Font(root=self.root, font=bold_t)
        W = self.width - 20
        row_h = max(14, font.metrics("linespace") + 2)
        label_w = int(W * 0.36)
        pct_w = bold.measure("100%") + 6
        reset_w = font.measure("↻ 22h 59m") + 2
        x0, x1 = label_w, W - pct_w - reset_w - 6
        mid = row_h // 2
        for w in visible_windows(p, snap):
            c = tk.Canvas(self.body, width=W, height=row_h, bg=THEME["bg"], highlightthickness=0)
            c._ctx = (p, w.label)  # type: ignore[attr-defined]
            c.pack(anchor="w")
            text = w.label
            while text and font.measure(text) > label_w - 6:
                text = text[:-2] + "…" if len(text) > 2 else ""
            c.create_text(0, mid, text=text, anchor="w", fill=THEME["muted"], font=font_t)
            c.create_rectangle(x0, mid - 2, x1, mid + 2, fill=THEME["track"], width=0)
            fill = c.create_rectangle(x0, mid - 2, x0, mid + 2, fill=THEME["ok"], width=0)
            pace = c.create_rectangle(0, 0, 0, 0, fill=THEME["pace"], width=0, state="hidden")
            pct = c.create_text(x1 + 6 + pct_w - 4, mid, text="", anchor="e", fill=THEME["ok"], font=bold_t)
            right = c.create_text(W, mid, text="", anchor="e", fill=THEME["muted"], font=font_t)

            def update(label=w.label, c=c, fill=fill, pace=pace, pct=pct, right=right):
                win = self._win(p, label)
                if win is None:
                    return
                now = time.time()
                color = self._color(win.used_pct)
                if win.used_pct is not None:
                    c.coords(fill, x0, mid - 2, x0 + (x1 - x0) * max(0.0, min(1.0, win.used_pct / 100)), mid + 2)
                    c.itemconfigure(fill, fill=color)
                    c.itemconfigure(pct, text=f"{win.used_pct:.0f}%", fill=color)
                else:
                    c.coords(fill, x0, mid - 2, x0, mid + 2)
                    c.itemconfigure(pct, text="")
                elapsed = 1 - (win.resets_at - now) / win.length_s if win.resets_at and win.length_s else -1
                if 0 < elapsed < 1:
                    x = x0 + int((x1 - x0) * elapsed)
                    c.coords(pace, x, mid - 3, x + 1, mid + 3)
                    c.itemconfigure(pace, state="normal")
                else:
                    c.itemconfigure(pace, state="hidden")
                pr = self.projection(p, win, self._snap(p).fetched_at)
                if pr and pr.at_risk and pr.hits_at:
                    c.itemconfigure(right, text=f"⚠ {fmt_delta(pr.hits_at - now)}", fill=THEME["warn"])
                elif win.resets_at:
                    left = win.resets_at - now
                    c.itemconfigure(right, text=f"↻ {fmt_delta(left)}" if left > 60 else "now", fill=THEME["muted"])
                else:
                    c.itemconfigure(right, text="")
            self._updaters.append(update)
            self.tooltip.bind([c], p, w.label)
        if snap.error:
            lab = self._label(self.body, "", THEME["err"], 7, wraplength=W)
            lab.pack(anchor="w")
            self._updaters.append(lambda: self._set(lab, text=self._snap(p).error))

    def _render_compact(self, p: Provider) -> None:
        """One line: name + the first three percentages. Also used for collapsed subscriptions."""
        snap = self._snap(p)
        compact = view_of(self.cfg) == "compact"
        size = 10 if compact else 9
        row = self._header(p, snap, size=size, plan=False)
        nums = tk.Frame(row, bg=THEME["bg"])
        nums.pack(side="right")
        for w in [w for w in visible_windows(p, snap)[:3] if w.used_pct is not None]:
            lab = self._label(nums, "", THEME["ok"], size, True)
            lab.pack(side="left")
            lab._ctx = (p, w.label)  # type: ignore[attr-defined]

            def update(label=w.label, lab=lab):
                win = self._win(p, label)
                if win is None or win.used_pct is None:
                    return
                pr = self.projection(p, win, self._snap(p).fetched_at)
                self._set(lab, text=f" {win.used_pct:.0f}%{'⚠' if pr and pr.at_risk else ''}",
                          fg=self._color(win.used_pct))
            self._updaters.append(update)
            self.tooltip.bind([lab], p, w.label)
        if snap.error:
            self._label(nums, " ⚠", THEME["err"], size).pack(side="left")

    def _render_parked(self, p: Provider) -> None:
        """One dim line for a subscription whose login expired; it comes back when the login is used."""
        row = tk.Frame(self.body, bg=THEME["bg"])
        row._ctx = (p, None)  # type: ignore[attr-defined]
        row.pack(fill="x")
        dot = tk.Canvas(row, width=8, height=8, bg=THEME["bg"], highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, outline=THEME["muted"], width=1)
        dot.pack(side="left", padx=(0, 6))
        self._label(row, p.name, THEME["muted"], 9, True).pack(side="left")
        self._label(row, "sign-in expired · returns when used", THEME["muted"], 8).pack(side="right")

    @staticmethod
    def _all_widgets(w) -> list:
        out = [w]
        for c in w.winfo_children():
            out += Widget._all_widgets(c)
        return out

    def run(self) -> None:
        self.root.mainloop()
