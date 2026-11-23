"""Settings window and the "Add subscription" picker.

Rows are generated from each provider's `fields`, so new providers need no UI code.
"""
from __future__ import annotations

import json
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Any, Optional

from . import APP
from .core import (IS_LINUX, IS_MAC, HttpError, Provider, Snapshot, bg, config_path, expand, find_login_files,
                   open_path)
from .providers import CATALOG, PROVIDERS
from .providers.command import CUSTOM_HELP
from .ui import EXPIRED, FONT, SORTS, THEME, TRAY_MODES, VIEWS, fsize, view_of

AUTH_LABEL = {"auto": "Uses your existing login", "key": "Paste an API key", "cookie": "Paste a browser session",
              "api": "API usage: paste an API key",
              "command": "Your own command"}

COOKIE_HELP = """Some subscriptions only show usage on the vendor's website. For those, SubUsage uses your browser session.

1. Sign in to the site in your browser and open its usage or settings page.
2. Open the developer tools (F12, or ⌥⌘I on a Mac) and go to the Network tab.
3. Reload the page, right-click any request to that site, and choose Copy → Copy as cURL.
4. Paste it into the provider's "Browser session" box and press Test.

You can also paste just the Cookie header, or only the one cookie the provider names.

SubUsage keeps only the cookies that provider needs, sends them only to that vendor, and stores them in your
config file (like API keys; env:VAR works too). Sessions expire now and then: when one does, the subscription
shows "session expired" and you paste a fresh one. Signing out of the site in your browser also ends it."""


def matches(query: str, *texts: str) -> bool:
    """Every word of the query appears somewhere in the texts (case-insensitive)."""
    hay = " ".join(t for t in texts if t).lower()
    return all(w in hay for w in query.lower().split())


def search_entry(parent, on_change, placeholder: str = "Search…", width: int = 28) -> tk.Entry:
    """An entry with a placeholder that calls on_change(query) as you type. Esc clears it."""
    e = entry(parent, "", width)
    state = {"empty": True}

    def show_placeholder():
        if not e.get():
            state["empty"] = True
            e.insert(0, placeholder)
            e.configure(fg=THEME["muted"])

    def focus_in(_e):
        if state["empty"]:
            e.delete(0, "end")
            e.configure(fg=THEME["fg"])
            state["empty"] = False

    def changed(_e=None):
        if not state["empty"]:
            on_change(e.get().strip())

    def clear(_e):
        e.delete(0, "end")
        on_change("")
        return "break"
    e.bind("<FocusIn>", focus_in)
    e.bind("<FocusOut>", lambda _e: show_placeholder())
    e.bind("<KeyRelease>", changed)
    e.bind("<Escape>", clear)
    e.query = lambda: "" if state["empty"] else e.get().strip()  # type: ignore[attr-defined]
    show_placeholder()
    return e


# --------------------------------------------------------------------------------------------------
# Small themed widgets
# --------------------------------------------------------------------------------------------------

def lbl(parent, text, color=None, size=9, bold=False, **kw):
    return tk.Label(parent, text=text, fg=color or THEME["fg"], bg=kw.pop("bg", None) or parent["bg"],
                    font=(FONT, fsize(size), "bold" if bold else "normal"), justify="left", **kw)


def entry(parent, value, width=44, secret=False):
    e = tk.Entry(parent, width=width, bg=THEME["track"], fg=THEME["fg"], insertbackground=THEME["fg"],
                 relief="flat", highlightthickness=1, highlightbackground=THEME["track"],
                 highlightcolor=THEME["muted"], font=(FONT, fsize(9)), show="•" if secret else "")
    e.insert(0, "" if value is None else str(value))
    return e


def button(parent, text, cmd, primary=False, subtle=False, small=False):
    base = THEME["primary"] if primary else (parent["bg"] if subtle else THEME["button"])
    hover = THEME["primary_hover"] if primary else THEME["button_hover"]
    b = tk.Label(parent, text=text, fg=THEME["muted"] if subtle else THEME["fg"], bg=base,
                 font=(FONT, fsize(8 if small else 9), "bold" if primary else "normal"),
                 padx=8 if small else 10, pady=2 if small else 3, cursor="hand2")
    b.bind("<Button-1>", lambda _e: cmd())
    b.bind("<Enter>", lambda _e: b.configure(bg=hover))
    b.bind("<Leave>", lambda _e: b.configure(bg=base))
    return b


def toggle(parent, text, value, bg=None):
    var = tk.BooleanVar(value=bool(value))
    lab = lbl(parent, "", bg=bg, cursor="hand2")

    def paint(*_a):
        lab.configure(text=("☑ " if var.get() else "☐ ") + text, fg=THEME["fg"] if var.get() else THEME["muted"])
    lab.bind("<Button-1>", lambda _e: var.set(not var.get()))
    var.trace_add("write", paint)
    paint()
    lab.pack(side="left", padx=(0, 14))
    return var


def choice(parent, options, value, bg=None):
    """A small dark dropdown. Returns a StringVar."""
    var = tk.StringVar(value=value)
    names = dict(options)
    mb = tk.Menubutton(parent, text=names.get(value, value), bg=THEME["button"], fg=THEME["fg"],
                       activebackground=THEME["button_hover"], activeforeground=THEME["fg"], relief="flat",
                       font=(FONT, fsize(9)), padx=8, pady=1)
    menu = tk.Menu(mb, tearoff=0)
    for key, label in options:
        menu.add_command(label=label, command=lambda k=key, l=label: (var.set(k), mb.configure(text=l)))
    mb.configure(menu=menu)
    mb.pack(side="left", padx=(0, 14))
    return var


class ScrollFrame(tk.Frame):
    """A vertically scrollable frame; put children in .inner."""

    def __init__(self, parent, max_height: int, width: int):
        super().__init__(parent, bg=parent["bg"])
        self.max_height = max_height
        self.canvas = tk.Canvas(self, bg=parent["bg"], highlightthickness=0, width=width)
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=parent["bg"])
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda _e: self._resize())
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda _e: self._wheel(True))
            w.bind("<Leave>", lambda _e: self._wheel(False))

    def _resize(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        h = self.inner.winfo_reqheight()
        self.canvas.configure(height=min(h, self.max_height), width=self.inner.winfo_reqwidth())
        if h > self.max_height:
            self.bar.pack(side="right", fill="y")
        else:
            self.bar.pack_forget()

    def _wheel(self, on: bool) -> None:
        if IS_LINUX:
            if on:
                self.bind_all("<Button-4>", lambda _e: self.canvas.yview_scroll(-3, "units"))
                self.bind_all("<Button-5>", lambda _e: self.canvas.yview_scroll(3, "units"))
            else:
                self.unbind_all("<Button-4>")
                self.unbind_all("<Button-5>")
        elif on:
            step = 1 if IS_MAC else 120
            self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / step), "units"))
        else:
            self.unbind_all("<MouseWheel>")


def info_dialog(parent, title: str, text: str) -> None:
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(bg=THEME["bg"])
    top.attributes("-topmost", True)
    box = tk.Frame(top, bg=THEME["bg"], padx=16, pady=14)
    box.pack(fill="both", expand=True)
    t = tk.Text(box, width=78, height=text.count("\n") + 2, bg=THEME["panel"], fg=THEME["fg"], relief="flat",
                font=("Consolas" if not (IS_MAC or IS_LINUX) else ("Menlo" if IS_MAC else "DejaVu Sans Mono"),
                      fsize(9)), padx=10, pady=8, wrap="word")
    t.insert("1.0", text)
    t.configure(state="disabled")
    t.pack(fill="both", expand=True)
    button(box, "Close", top.destroy, primary=True).pack(anchor="e", pady=(10, 0))
    top.bind("<Escape>", lambda _e: top.destroy())


def notes_dialog(parent, title: str, text: str, footer: str = "") -> tk.Toplevel:
    """Release notes, lightly formatted from the CHANGELOG's markdown."""
    import re
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(bg=THEME["bg"])
    top.attributes("-topmost", True)
    box = tk.Frame(top, bg=THEME["bg"], padx=16, pady=14)
    box.pack(fill="both", expand=True)
    lbl(box, title, THEME["fg"], 11, True).pack(anchor="w", pady=(0, 8))
    lines = (text or "No release notes for this version.").strip().splitlines()
    t = tk.Text(box, width=72, height=min(24, max(6, len(lines) + 4)), bg=THEME["panel"], fg=THEME["fg"],
                relief="flat", font=(FONT, fsize(9)), padx=12, pady=10, wrap="word", spacing1=2, spacing3=2,
                highlightthickness=0)
    t.tag_configure("h", font=(FONT, fsize(10), "bold"), spacing1=8, foreground=THEME["primary_hover"])
    t.tag_configure("b", font=(FONT, fsize(9), "bold"))
    t.tag_configure("li", lmargin1=6, lmargin2=20)
    for line in lines:
        if line.startswith("#"):
            t.insert("end", "Version " + line.lstrip("# ").strip() + "\n", "h")
            continue
        bullet = line.lstrip().startswith(("- ", "* "))
        body = ("•  " + line.lstrip()[2:]) if bullet else line
        for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", body)):
            t.insert("end", chunk.replace("`", ""), ("li", "b") if i % 2 and bullet else ("b",) if i % 2 else
                     ("li",) if bullet else ())
        t.insert("end", "\n")
    t.configure(state="disabled")
    sb = tk.Scrollbar(box, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="top", fill="both", expand=True)
    bar = tk.Frame(box, bg=THEME["bg"])
    bar.pack(fill="x", pady=(10, 0))
    if footer:
        lbl(bar, footer, THEME["muted"], 8).pack(side="left")
    button(bar, "Close", top.destroy, primary=True).pack(side="right")
    top.bind("<Escape>", lambda _e: top.destroy())
    top.update_idletasks()
    top.geometry(f"+{max(0, parent.winfo_rootx() - 40)}+{max(0, parent.winfo_rooty() + 20)}")
    return top


# --------------------------------------------------------------------------------------------------
# Settings window
# --------------------------------------------------------------------------------------------------

class SettingsDialog:
    def __init__(self, widget):
        self.w = widget
        self.cfg = json.loads(json.dumps(widget.cfg))
        self.rows: list[dict[str, Any]] = []
        self.picker: Optional[tk.Toplevel] = None
        top = self.top = tk.Toplevel(widget.root)
        top.title(f"{APP} settings")
        top.configure(bg=THEME["bg"])
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _e: self.close())
        top.protocol("WM_DELETE_WINDOW", self.close)

        outer = tk.Frame(top, bg=THEME["bg"], padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=THEME["bg"])
        head.pack(fill="x", pady=(0, 6))
        lbl(head, "SUBSCRIPTIONS", THEME["muted"], 8, True).pack(side="left")
        button(head, "+ Add subscription", self.open_picker, small=True).pack(side="right")
        self.row_filter = search_entry(head, self._filter_rows, "Filter subscriptions…", 24)
        self.row_filter.pack(side="right", padx=(0, 8))
        self.filter_note = lbl(head, "", THEME["muted"], 8)
        self.filter_note.pack(side="right", padx=(0, 8))

        self.scroll = ScrollFrame(outer, max_height=int(top.winfo_screenheight() * 0.55), width=640)
        self.scroll.pack(fill="x")
        self.prov_box = self.scroll.inner
        for pc in self.cfg.get("providers", []):
            if pc.get("type") in PROVIDERS:
                self._provider_row(pc)

        lbl(outer, "DISPLAY", THEME["muted"], 8, True).pack(anchor="w", pady=(12, 6))
        disp = tk.Frame(outer, bg=THEME["bg"])
        disp.pack(fill="x")
        self.v_warn = self._num(disp, "Amber at %", self.cfg.get("warn_pct", 70), 0)
        self.v_crit = self._num(disp, "Red at %", self.cfg.get("crit_pct", 90), 1)
        self.v_width = self._num(disp, "Width px", self.cfg.get("width", 300), 2)
        self.v_opacity = self._num(disp, "Opacity %", int(float(self.cfg.get("opacity", 0.95)) * 100), 3)
        flags = tk.Frame(outer, bg=THEME["bg"])
        flags.pack(fill="x", pady=(8, 0))
        lbl(flags, "View", THEME["muted"]).pack(side="left", padx=(0, 6))
        self.v_view = choice(flags, VIEWS, view_of(self.cfg))
        lbl(flags, "Sort", THEME["muted"]).pack(side="left", padx=(0, 6))
        self.v_sort = choice(flags, SORTS, self.cfg.get("sort", "manual"))
        self.v_top = toggle(flags, "Always on top", self.cfg.get("always_on_top", True))
        flags2 = tk.Frame(outer, bg=THEME["bg"])
        flags2.pack(fill="x", pady=(8, 0))
        self.v_hide = toggle(flags2, "Hide subscriptions with no login", self.cfg.get("hide_unconfigured", True))
        self.v_proj = toggle(flags2, "Warn when a limit will run out before it resets", self.cfg.get("projections", True))
        flags4 = tk.Frame(outer, bg=THEME["bg"])
        flags4.pack(fill="x", pady=(8, 0))
        lbl(flags4, "Expired logins", THEME["muted"]).pack(side="left", padx=(0, 6))
        self.v_expired = choice(flags4, EXPIRED, self.cfg.get("expired_display", "hide"))
        lbl(flags4, "e.g. Hermes when you haven't used it lately; it comes back once you do", THEME["muted"], 7).pack(side="left")
        flags5 = tk.Frame(outer, bg=THEME["bg"])
        flags5.pack(fill="x", pady=(8, 0))
        lbl(flags5, "Menu bar icon" if IS_MAC else "System tray", THEME["muted"]).pack(side="left", padx=(0, 6))
        from . import tray
        self.tray_ok = tray.available()
        if self.tray_ok:
            self.v_tray = choice(flags5, TRAY_MODES, self.cfg.get("tray", "both"))
            lbl(flags5, "click the icon for its menu" if IS_MAC else "left-click the icon to show/hide the widget",
                THEME["muted"], 7).pack(side="left")
        else:
            self.v_tray = tk.StringVar(value=self.cfg.get("tray", "off"))
            lbl(flags5, tray.unavailable_reason(), THEME["muted"], 8, wraplength=470).pack(side="left")
        flags6 = tk.Frame(outer, bg=THEME["bg"])
        flags6.pack(fill="x", pady=(8, 0))
        self.v_background = toggle(flags6, "Run in background", self.cfg.get("background", False))
        lbl(flags6, "starts at login and lives in the tray: the widget starts hidden; show/hide, pause or quit "
            "from the icon" + ("" if self.tray_ok else " (needs a tray icon)"), THEME["muted"], 7,
            wraplength=430).pack(side="left")
        flags3 = tk.Frame(outer, bg=THEME["bg"])
        flags3.pack(fill="x", pady=(8, 0))
        self.v_hist = toggle(flags3, "Keep usage history on this computer (30 days)", self.cfg.get("history", True))
        button(flags3, "Clear history", self._clear_history, subtle=True, small=True).pack(side="left")

        self._about(outer)

        bar = tk.Frame(outer, bg=THEME["bg"])
        bar.pack(fill="x", pady=(16, 0))
        button(bar, "Open config file", lambda: open_path(config_path()), subtle=True).pack(side="left")
        button(bar, "Save", self.save, primary=True).pack(side="right")
        button(bar, "Cancel", self.close).pack(side="right", padx=(0, 8))
        self._position()

    def _position(self) -> None:
        top, widget = self.top, self.w
        top.update_idletasks()
        wx, wy = widget.root.winfo_x(), widget.root.winfo_y()
        tw, th = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = wx - tw - 12 if wx - tw - 12 > 0 else min(wx + widget.width + 12, sw - tw)
        top.geometry(f"+{max(0, x)}+{max(0, min(wy, sh - th - 60))}")
        top.focus_force()

    def alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if self.picker is not None and self.picker.winfo_exists():
            self.picker.destroy()
        if self.alive():
            self.top.destroy()
        import gc
        gc.collect()  # free the dialog's Tk variables here, on the UI thread, not later on a fetch thread

    def _num(self, parent, label, value, col):
        lbl(parent, label, THEME["muted"]).grid(row=0, column=col * 2, sticky="w", padx=(0, 4))
        e = entry(parent, value, 5)
        e.grid(row=0, column=col * 2 + 1, sticky="w", padx=(0, 14))
        return e

    def _bg(self, fn, done):
        bg(fn, lambda r: self.alive() and done(r), self.top.after)

    def _clear_history(self) -> None:
        from tkinter import messagebox
        from . import history
        if messagebox.askyesno("Clear history", "Delete all locally stored usage history?", parent=self.top):
            history.clear()

    # ----- about & updates
    def _about(self, outer) -> None:
        from . import update
        lbl(outer, "ABOUT & UPDATES", THEME["muted"], 8, True).pack(anchor="w", pady=(14, 6))
        box = tk.Frame(outer, bg=THEME["panel"], padx=10, pady=8)
        box.pack(fill="x")
        top = tk.Frame(box, bg=THEME["panel"])
        top.pack(fill="x")
        kind = {"git": "git checkout", "pip": "pip/pipx install", "source": "downloaded folder"}[update.install_kind()]
        from . import AUTHOR
        lbl(top, f"{APP} {update.version_string()}", THEME["fg"], 9, True).pack(side="left")
        lbl(top, f"  ·  by {AUTHOR}  ·  {kind}", THEME["muted"], 8).pack(side="left")
        button(top, "Repo", lambda: webbrowser.open(update.REPO_URL), subtle=True, small=True).pack(side="right")
        self.btn_update = button(top, "Update now", self._apply_update, primary=True, small=True)
        self.btn_notes = button(top, "What's new", self._show_new_notes, small=True)
        button(top, "Check for updates", self._check_update, small=True).pack(side="right", padx=(0, 6))
        line = tk.Frame(box, bg=THEME["panel"])
        line.pack(fill="x", pady=(6, 0))
        self.v_updates = toggle(line, "Check automatically (daily)", self.cfg.get("check_updates", True), bg=THEME["panel"])
        button(line, "Notes for this version", self._show_current_notes, subtle=True, small=True).pack(side="right")
        self.upd_status = lbl(box, "", THEME["muted"], 8, wraplength=600)
        self.upd_status.pack(anchor="w", pady=(4, 0))
        self._show_update_state()

    def _show_update_state(self) -> None:
        info, err = self.w.update_info, self.w.update_error
        self.btn_update.pack_forget()
        self.btn_notes.pack_forget()
        if info and info.available:
            first = next((ln.strip("-* ").replace("**", "") for ln in info.notes.splitlines()
                          if ln.strip().startswith(("-", "*"))), "")
            self.upd_status.configure(text=f"Version {info.latest} is available (you have {info.current})."
                                           + (f"  New: {first[:110]}{'…' if len(first) > 110 else ''}" if first else ""),
                                      fg=THEME["warn"])
            self.btn_update.pack(side="right", padx=(0, 6))
            self.btn_notes.pack(side="right", padx=(0, 6))
        elif info:
            ago = int((time.time() - info.checked_at) // 60)
            self.upd_status.configure(text=f"Up to date (latest {info.latest}, checked {ago} min ago).", fg=THEME["ok"])
        elif err:
            self.upd_status.configure(text=f"Couldn't check: {err}", fg=THEME["err"])
        else:
            self.upd_status.configure(text="Not checked yet.", fg=THEME["muted"])

    def _check_update(self) -> None:
        self.upd_status.configure(text="Checking GitHub…", fg=THEME["muted"])
        self.w.check_updates(on_done=lambda: self.alive() and self._show_update_state())

    def _show_new_notes(self) -> None:
        info = self.w.update_info
        if info:
            notes_dialog(self.top, f"What's new in {info.latest}", info.notes or f"See {info.url}",
                         footer=f"You have {info.current}")

    def _show_current_notes(self) -> None:
        from . import __version__, update
        text = update.local_changelog()
        if text:
            notes_dialog(self.top, f"SubUsage {__version__}", update.notes_between(text, "0", __version__)
                         .split("\n## ")[0])
            return
        self._bg(lambda: update.remote_changelog(f"v{__version__}") or update.remote_changelog("main") or "",
                 lambda t: notes_dialog(self.top, f"SubUsage {__version__}",
                                        update.notes_between(t, "0", __version__).split("\n## ")[0]
                                        if isinstance(t, str) else ""))

    def show_about(self) -> None:
        self.top.lift()
        info = self.w.update_info
        if info is None or time.time() - info.checked_at > 3600:
            self._check_update()
        else:
            self._show_update_state()

    def _apply_update(self) -> None:
        from . import update
        info = self.w.update_info
        if not info:
            return
        self.upd_status.configure(text=f"Installing {info.latest}…", fg=THEME["warn"])

        def done(res):
            if isinstance(res, Exception):
                self.upd_status.configure(text=f"✗ Update failed: {res}", fg=THEME["err"])
                return
            self.upd_status.configure(text=f"✓ {res}. Restarting…", fg=THEME["ok"])
            # Shown once after the restart, even offline; also tells the new version what you came from.
            self.w.cfg["pending_whats_new"] = {"from": info.current, "to": info.latest, "notes": info.notes}

            def restart():
                try:
                    update.restart()
                finally:
                    self.w.quit()
            self.top.after(900, restart)
        self._bg(lambda: update.apply(info), done)

    # ----- provider rows
    def _provider_row(self, pc: dict[str, Any]) -> None:
        cls: type[Provider] = PROVIDERS[pc["type"]]
        P = THEME["panel"]
        f = tk.Frame(self.prov_box, bg=P, padx=10, pady=8)
        f.pack(fill="x", pady=(0, 8))
        row: dict[str, Any] = {"cfg": pc, "cls": cls, "frame": f, "inputs": {}}

        head = tk.Frame(f, bg=P)
        head.pack(fill="x")
        dot = tk.Canvas(head, width=8, height=8, bg=P, highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=cls.accent, width=0)
        dot.pack(side="left", padx=(0, 6))
        row["enabled"] = toggle(head, "", pc.get("enabled", True), bg=P)
        row["name"] = entry(head, pc.get("name") or cls.default_name, 16)
        row["name"].pack(side="left")
        lbl(head, f"{cls.vendor} · {cls.plans}" if cls.vendor else cls.plans, THEME["muted"], 8).pack(side="left", padx=8)
        button(head, "Remove", lambda: self._remove(row), subtle=True, small=True).pack(side="right")
        if getattr(cls, "key_url", ""):
            button(head, "Get key ↗", lambda u=cls.key_url: webbrowser.open(u), subtle=True, small=True).pack(side="right")
        row["poll"] = entry(head, pc.get("poll_seconds", cls.default_poll), 5)
        lbl(head, "s", THEME["muted"]).pack(side="right")
        row["poll"].pack(side="right")
        lbl(head, "every ", THEME["muted"]).pack(side="right")
        if cls.setup:
            lbl(f, cls.setup, THEME["muted"], 8, wraplength=600).pack(anchor="w", pady=(4, 0))

        for fld in cls.fields:
            self._field(row, fld)
        if pc.get("hidden"):
            self._hidden_line(row)

        foot = tk.Frame(f, bg=P)
        foot.pack(fill="x", pady=(6, 0))
        button(foot, "Test", lambda: self._test(row), small=True).pack(side="left")
        row["status"] = lbl(foot, "", THEME["muted"], 8, wraplength=520)
        row["status"].pack(side="left", padx=(8, 0))
        self.rows.append(row)

    def _field(self, row, fld) -> None:
        P = THEME["panel"]
        f = row["frame"]
        line = tk.Frame(f, bg=P)
        line.pack(fill="x", pady=(6, 0))
        val = row["cfg"].get(fld.key, "")
        if fld.kind == "device_login":
            button(line, fld.label, lambda: self._device_login(row), small=True).pack(side="left")
            return
        lbl(line, fld.label, THEME["muted"], width=14, anchor="w").pack(side="left")
        if fld.kind == "choice":
            var = tk.StringVar(value=val or fld.default or (fld.options[0][0] if fld.options else ""))
            names = dict(fld.options)
            mb = tk.Menubutton(line, text=names.get(var.get(), var.get()), bg=THEME["button"], fg=THEME["fg"],
                               activebackground=THEME["button_hover"], activeforeground=THEME["fg"], relief="flat",
                               font=(FONT, fsize(9)), padx=8, pady=2)
            menu = tk.Menu(mb, tearoff=0)
            for value, label in fld.options:
                menu.add_command(label=label, command=lambda v=value, l=label: (var.set(v), mb.configure(text=l)))
            mb.configure(menu=menu)
            mb.pack(side="left")
            row["inputs"][fld.key] = var
        else:
            e = entry(line, val, 52 if fld.kind == "command" else 40, secret=fld.kind == "secret")
            e.pack(side="left", fill="x", expand=True)
            row["inputs"][fld.key] = e
            if fld.kind == "path":
                button(line, "Find…", lambda: self._find(row, fld.key), small=True).pack(side="left", padx=(6, 0))
                button(line, "Browse…", lambda: self._browse(row, fld.key), small=True).pack(side="left", padx=(6, 0))
            elif fld.kind == "secret":
                def flip(e=e):
                    e.configure(show="" if e.cget("show") else "•")
                button(line, "Show", flip, small=True).pack(side="left", padx=(6, 0))
            elif fld.kind == "command":
                button(line, "How to", lambda: info_dialog(self.top, "Custom providers", CUSTOM_HELP),
                       small=True).pack(side="left", padx=(6, 0))
        hint = fld.help
        if fld.kind == "path" and not val:
            try:
                d = row["cls"].default_path()
            except Exception:
                d = None
            hint = f"{hint}  Default: {d}" if d else hint
        if hint:
            lbl(f, hint, THEME["muted"], 7, wraplength=600).pack(anchor="w", padx=(0, 0))

    def _hidden_line(self, row) -> None:
        P = THEME["panel"]
        line = tk.Frame(row["frame"], bg=P)
        line.pack(fill="x", pady=(6, 0))
        lbl(line, "Hidden limits", THEME["muted"], width=14, anchor="w").pack(side="left")

        def unhide(label, chip):
            row["cfg"]["hidden"] = [h for h in row["cfg"].get("hidden", []) if h != label]
            if not row["cfg"]["hidden"]:
                row["cfg"].pop("hidden")
            chip.destroy()
        for label in list(row["cfg"].get("hidden", [])):
            chip = button(line, f"{label}  ✕", lambda: None, small=True)
            chip.bind("<Button-1>", lambda _e, l=label, c=chip: unhide(l, c))
            chip.pack(side="left", padx=(0, 6))

    def _status(self, row, text, color=None):
        if row["status"].winfo_exists():
            row["status"].configure(text=text, fg=color or THEME["muted"])

    def _set(self, row, key, value: str) -> None:
        w = row["inputs"][key]
        if isinstance(w, tk.StringVar):
            w.set(value)
        else:
            w.delete(0, "end")
            w.insert(0, value)

    def _browse(self, row, key) -> None:
        cls = row["cls"]
        cur = row["inputs"][key].get().strip()
        d = cls.default_path() if hasattr(cls, "default_path") else None
        start = expand(cur).parent if cur else (d.parent if d else Path.home())
        if not start.exists():
            start = Path.home()
        chosen = filedialog.askopenfilename(parent=self.top, initialdir=str(start),
                                            title=f"Select {cls.login_what}")
        if chosen:
            self._set(row, key, str(Path(chosen)))
            self._test(row)

    def _find(self, row, key) -> None:
        cls = row["cls"]
        self._status(row, "Searching…")

        def search() -> list[Path]:
            custom = getattr(cls, "find_candidates", None)
            if custom:
                return list(custom())
            if cls.login_dir and cls.login_file:
                return find_login_files(cls.login_dir, cls.login_file, cls.default_path())
            d = cls.default_path()
            return [d] if d and d.exists() else []

        def done(res):
            if isinstance(res, Exception):
                self._status(row, f"Search failed: {res}", THEME["err"])
            elif not res:
                self._status(row, "Nothing found. Sign in with the vendor's app/CLI first, or use Browse.", THEME["err"])
            elif len(res) == 1:
                self._pick(row, key, res[0])
            else:
                m = tk.Menu(self.top, tearoff=0)
                for pth in res:
                    m.add_command(label=str(pth), command=lambda pth=pth: self._pick(row, key, pth))
                self._status(row, f"Found {len(res)}; pick one.")
                anchor = row["inputs"][key]
                m.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())
        self._bg(search, done)

    def _pick(self, row, key, pth: Path) -> None:
        d = row["cls"].default_path()
        self._set(row, key, "" if d and Path(pth) == d else str(pth))
        self._test(row)

    def _row_cfg(self, row) -> dict[str, Any]:
        pc = dict(row["cfg"])
        cls = row["cls"]
        pc["enabled"] = bool(row["enabled"].get())
        pc["name"] = row["name"].get().strip() or cls.default_name
        try:
            pc["poll_seconds"] = max(cls.min_poll, int(float(row["poll"].get())))
        except ValueError:
            pc["poll_seconds"] = cls.default_poll
        for key, w in row["inputs"].items():
            v = (w.get() if isinstance(w, tk.StringVar) else w.get()).strip().strip('"')
            if v:
                pc[key] = v
            else:
                pc.pop(key, None)
        return pc

    def _test(self, row) -> None:
        prov = row["cls"](self._row_cfg(row))
        self._status(row, "Testing…")

        def done(res):
            if isinstance(res, Snapshot):
                bits = [res.plan or "OK"] + [f"{w.label} {w.used_pct:.0f}%" for w in res.windows[:3]
                                             if w.used_pct is not None]
                self._status(row, "✓ " + " · ".join(bits), THEME["ok"])
            elif isinstance(res, HttpError):
                msg = ("Rate limited by the vendor; your login works, try again later" if res.status == 429
                       else prov.http_error_message(res))
                self._status(row, f"✗ {msg}", THEME["warn"] if res.status == 429 else THEME["err"])
            else:
                self._status(row, f"✗ {res}", THEME["err"])
        self._bg(prov.fetch, done)

    def _device_login(self, row) -> None:
        cls = row["cls"]
        self._status(row, "Contacting GitHub…")

        def started(state):
            if isinstance(state, Exception):
                self._status(row, f"✗ {state}", THEME["err"])
                return
            code, url = state.get("user_code"), state.get("verification_uri") or "https://github.com/login/device"
            try:
                self.top.clipboard_clear()
                self.top.clipboard_append(code)
            except tk.TclError:
                pass
            webbrowser.open(url)
            self._status(row, f"Enter code {code} at {url} (copied to clipboard). Waiting…", THEME["warn"])
            deadline = time.time() + float(state.get("expires_in") or 900)

            def poll():
                if not self.alive():
                    return
                if time.time() > deadline:
                    self._status(row, "✗ Sign-in timed out; try again", THEME["err"])
                    return

                def got(tok):
                    if isinstance(tok, Exception):
                        self._status(row, f"✗ {tok}", THEME["err"])
                    elif tok:
                        self._set(row, "token", tok)
                        self._status(row, "✓ Signed in. Click Save to keep it.", THEME["ok"])
                        self._test(row)
                    else:
                        self.top.after(int(float(state.get("interval") or 5) * 1000), poll)
                self._bg(lambda: cls.device_poll(state), got)
            self.top.after(int(float(state.get("interval") or 5) * 1000), poll)
        self._bg(lambda: cls.device_start(self._row_cfg(row)), started)

    def _remove(self, row) -> None:
        row["frame"].destroy()
        self.rows.remove(row)
        self._refresh_picker()

    def add_provider(self, ptype: str) -> None:
        cls = PROVIDERS[ptype]
        self._provider_row({"type": ptype, "enabled": True, "name": cls.default_name,
                            "poll_seconds": cls.default_poll})
        self.top.after(50, lambda: self.scroll.canvas.yview_moveto(1.0))
        self._refresh_picker()

    def save(self) -> None:
        def num(e, default, lo, hi):
            try:
                return max(lo, min(hi, float(e.get())))
            except ValueError:
                return default
        cfg = self.cfg
        cfg["providers"] = [self._row_cfg(r) for r in self.rows]
        cfg["warn_pct"] = num(self.v_warn, 70, 1, 100)
        cfg["crit_pct"] = num(self.v_crit, 90, 1, 100)
        cfg["width"] = int(num(self.v_width, 300, 220, 700))
        cfg["opacity"] = round(num(self.v_opacity, 95, 30, 100) / 100, 2)
        cfg["always_on_top"] = bool(self.v_top.get())
        cfg["view"] = self.v_view.get()
        cfg["sort"] = self.v_sort.get()
        cfg["expired_display"] = self.v_expired.get()
        cfg["tray"] = self.v_tray.get()
        was_bg, cfg["background"] = bool(cfg.get("background")), bool(self.v_background.get()) and self.tray_ok
        if cfg["background"] != was_bg:
            from .core import log, set_startup
            try:
                set_startup(cfg["background"])  # background mode = start at login; turning it off undoes that
            except OSError as e:
                log(f"start at login failed: {e}")
        cfg["projections"] = bool(self.v_proj.get())
        cfg["history"] = bool(self.v_hist.get())
        cfg.pop("compact", None)
        cfg["check_updates"] = bool(self.v_updates.get())
        cfg["hide_unconfigured"] = bool(self.v_hide.get())
        self.close()
        self.w.apply_config(cfg)

    # ----- "Add subscription" picker
    def open_picker(self) -> None:
        if self.picker is not None and self.picker.winfo_exists():
            self.picker.lift()
            return
        pk = self.picker = tk.Toplevel(self.top)
        pk.title("Add subscription")
        pk.configure(bg=THEME["bg"])
        pk.attributes("-topmost", True)
        pk.resizable(False, False)
        pk.bind("<Escape>", lambda _e: pk.destroy())
        outer = tk.Frame(pk, bg=THEME["bg"], padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        top_line = tk.Frame(outer, bg=THEME["bg"])
        top_line.pack(fill="x")
        lbl(top_line, "Add or remove subscriptions. Changes apply when you Save in Settings.", THEME["muted"],
            8).pack(side="left")
        self.pick_query = ""
        self.pick_search = search_entry(top_line, self._on_pick_search, "Search providers…", 26)
        self.pick_search.pack(side="right")
        self.pick_search.bind("<Return>", lambda _e: self._pick_enter())
        self.pick_scroll = ScrollFrame(outer, max_height=int(pk.winfo_screenheight() * 0.6), width=600)
        self.pick_scroll.pack(fill="x", pady=(8, 0))
        bar = tk.Frame(outer, bg=THEME["bg"])
        bar.pack(fill="x", pady=(12, 0))
        button(bar, "Custom provider how-to", lambda: info_dialog(pk, "Custom providers", CUSTOM_HELP),
               subtle=True).pack(side="left")
        button(bar, "Browser session how-to", lambda: info_dialog(pk, "Browser sessions", COOKIE_HELP),
               subtle=True).pack(side="left", padx=(8, 0))
        button(bar, "Request / contribute a provider",
               lambda: webbrowser.open("https://github.com/chrisdelrey/ai_subusage/blob/main/CONTRIBUTING.md"),
               subtle=True).pack(side="left", padx=(8, 0))
        button(bar, "Done", pk.destroy, primary=True).pack(side="right")
        self._refresh_picker()
        pk.update_idletasks()
        pk.geometry(f"+{self.top.winfo_x() + 40}+{self.top.winfo_y() + 40}")
        pk.after(50, self.pick_search.focus_set)

    def _refresh_picker(self) -> None:
        if self.picker is None or not self.picker.winfo_exists():
            return
        box = self.pick_scroll.inner
        for c in box.winfo_children():
            c.destroy()
        added = {r["cls"].type for r in self.rows}
        q = getattr(self, "pick_query", "")
        groups = [("auto", "Detected from your existing logins"), ("key", "Paste an API key"),
                  ("cookie", "Paste a browser session (usage only shown on the website)"),
                  ("api", "API usage: pay-as-you-go spend and credit (paste the key)"), ("command", "Anything else")]
        shown = 0
        for auth, title in groups:
            hits = [c for c in PROVIDERS.values() if c.auth == auth
                    and matches(q, c.default_name, c.vendor, c.plans, c.setup, c.type, getattr(c, "old_names", ()) and
                                " ".join(c.old_names) or "")]
            if not hits:
                continue
            lbl(box, title.upper(), THEME["muted"], 8, True).pack(anchor="w", pady=(8, 4))
            for cls in hits:
                self._picker_item(box, cls, cls.type in added)
            shown += len(hits)
        cat = [c for c in CATALOG if matches(q, *c)]
        if cat:
            lbl(box, "NOT BUILT IN YET: USE A CUSTOM PROVIDER OR CONTRIBUTE ONE", THEME["muted"], 8, True).pack(
                anchor="w", pady=(12, 4))
            for name, vendor, source in cat:
                r = tk.Frame(box, bg=THEME["bg"])
                r.pack(fill="x", pady=1)
                lbl(r, f"{name}", THEME["fg"], 9).pack(side="left")
                lbl(r, f"  {vendor} · {source}", THEME["muted"], 8).pack(side="left")
        if not shown and not cat:
            lbl(box, f"Nothing matches “{q}”. You can still track it with a custom provider (Anything else → Custom).",
                THEME["muted"], 9, wraplength=560).pack(anchor="w", pady=(12, 4))
        self.pick_scroll.canvas.yview_moveto(0)
        self._pick_hits = [c for c in PROVIDERS.values() if matches(q, c.default_name, c.vendor, c.plans, c.setup, c.type)]

    def _on_pick_search(self, q: str) -> None:
        if q != self.pick_query:
            self.pick_query = q
            self._refresh_picker()

    def _pick_enter(self) -> None:
        """Enter adds the only match (if it isn't added yet)."""
        added = {r["cls"].type for r in self.rows}
        hits = [c for c in getattr(self, "_pick_hits", []) if c.type not in added or c.multi]
        if self.pick_query and len(hits) == 1:
            self.add_provider(hits[0].type)

    def _filter_rows(self, q: str) -> None:
        shown = 0
        for r in self.rows:
            r["frame"].pack_forget()
        for r in self.rows:
            cls = r["cls"]
            name = r["name"].get() if r.get("name") else cls.default_name
            if matches(q, name, cls.default_name, cls.vendor, cls.plans, cls.type):
                r["frame"].pack(fill="x", pady=(0, 8))
                shown += 1
        self.filter_note.configure(text=f"{shown} of {len(self.rows)}" if q else "")
        self.scroll.canvas.yview_moveto(0)

    def _picker_item(self, box, cls: type[Provider], is_added: bool) -> None:
        P = THEME["panel"]
        r = tk.Frame(box, bg=P, padx=10, pady=6)
        r.pack(fill="x", pady=(0, 4))
        top = tk.Frame(r, bg=P)
        top.pack(fill="x")
        dot = tk.Canvas(top, width=8, height=8, bg=P, highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=cls.accent, width=0)
        dot.pack(side="left", padx=(0, 6))
        lbl(top, cls.default_name, THEME["fg"], 10, True).pack(side="left")
        lbl(top, f"  {cls.vendor} · {cls.plans}", THEME["muted"], 8).pack(side="left")
        if is_added and not cls.multi:
            def rm(t=cls.type):
                for row in [x for x in self.rows if x["cls"].type == t]:
                    self._remove(row)
            button(top, "Remove", rm, subtle=True, small=True).pack(side="right")
            lbl(top, "✓ Added  ", THEME["ok"], 8).pack(side="right")
        else:
            button(top, "Add", lambda t=cls.type: self.add_provider(t), small=True).pack(side="right")
        if getattr(cls, "key_url", ""):
            button(top, "Get key ↗", lambda u=cls.key_url: webbrowser.open(u), subtle=True,
                   small=True).pack(side="right", padx=(0, 6))
        lbl(r, cls.setup, THEME["muted"], 8, wraplength=560).pack(anchor="w", pady=(2, 0))
