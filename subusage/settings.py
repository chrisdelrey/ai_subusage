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
    """A vertically scrollable frame; put children in .inner.

    With fill=True it takes all the space its parent gives it, the inner frame stretches to its width
    (so rows follow the window when it's resized), and the scrollbar spans the full height.
    Otherwise it sizes itself to its content, up to max_height."""

    def __init__(self, parent, max_height: int = 0, width: int = 600, fill: bool = False):
        super().__init__(parent, bg=parent["bg"])
        self.max_height, self.fill = max_height, fill
        self.canvas = tk.Canvas(self, bg=parent["bg"], highlightthickness=0, width=width)
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=parent["bg"])
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._set_bar)
        if fill:
            self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda _e: self._resize())
        if fill:
            self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda _e: self._wheel(True))
            w.bind("<Leave>", lambda _e: self._wheel(False))

    def _set_bar(self, lo, hi) -> None:
        self.bar.set(lo, hi)
        if self.fill:  # hide the bar when everything fits
            if float(lo) <= 0.0 and float(hi) >= 1.0:
                self.bar.pack_forget()
            elif not self.bar.winfo_ismapped():
                self.bar.pack(side="right", fill="y", before=self.canvas)

    def _resize(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.fill:
            return
        h = self.inner.winfo_reqheight()
        self.canvas.configure(height=min(h, self.max_height), width=self.inner.winfo_reqwidth())
        if h > self.max_height:
            self.bar.pack(side="right", fill="y")
        else:
            self.bar.pack_forget()

    def _scroll(self, units: int) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(units, "units")

    def _wheel(self, on: bool) -> None:
        if IS_LINUX:
            if on:
                self.bind_all("<Button-4>", lambda _e: self._scroll(-3))
                self.bind_all("<Button-5>", lambda _e: self._scroll(3))
            else:
                self.unbind_all("<Button-4>")
                self.unbind_all("<Button-5>")
        elif on:
            step = 1 if IS_MAC else 120
            self.bind_all("<MouseWheel>", lambda e: self._scroll(int(-e.delta / step)))
        else:
            self.unbind_all("<MouseWheel>")

    def scroll_to(self, widget) -> None:
        """Scroll so widget (inside .inner) is near the top."""
        self.update_idletasks()
        total = max(1, self.inner.winfo_height())
        y = widget.winfo_rooty() - self.inner.winfo_rooty()
        self.canvas.yview_moveto(max(0.0, (y - 8) / total))


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

KIND_TAG = {"auto": "Subscription", "key": "Subscription", "cookie": "Subscription", "api": "API usage",
            "command": "Custom"}
KIND_FILTERS = (("all", "All"), ("sub", "Subscriptions"), ("api", "API usage"), ("cookie", "Browser session"))
PAGES = (("providers", "My providers"), ("add", "Add providers"), ("display", "Display"),
         ("tray", "Tray & startup"), ("data", "History & data"), ("about", "About & updates"))


def kind_matches(cls: type[Provider], kind: str) -> bool:
    return (kind == "all" or (kind == "api" and cls.auth == "api") or (kind == "cookie" and cls.auth == "cookie")
            or (kind == "sub" and cls.auth in ("auto", "key", "cookie")))


def vendor_of(cls: type[Provider]) -> str:
    return (cls.vendor or cls.default_name or "Other").strip()


class SettingsDialog:
    """Settings: a resizable window with a navigation menu on the left and one page at a time on the right."""

    def __init__(self, widget):
        self.w = widget
        self.cfg = json.loads(json.dumps(widget.cfg))
        self.rows: list[dict[str, Any]] = []
        self.groups: dict[str, dict[str, Any]] = {}
        self.page = ""
        self.pick_query, self.pick_kind = "", "all"
        top = self.top = tk.Toplevel(widget.root)
        top.title(f"{APP} settings")
        top.configure(bg=THEME["bg"])
        top.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        top.minsize(760, 480)
        top.bind("<Escape>", lambda _e: self.close())
        top.protocol("WM_DELETE_WINDOW", self.close)

        # Save / Cancel always visible at the bottom
        bar = tk.Frame(top, bg=THEME["panel"], padx=14, pady=8)
        bar.pack(side="bottom", fill="x")
        button(bar, "Open config file", lambda: open_path(config_path()), subtle=True).pack(side="left")
        button(bar, "Save", self.save, primary=True).pack(side="right")
        button(bar, "Cancel", self.close).pack(side="right", padx=(0, 8))

        body = tk.Frame(top, bg=THEME["bg"])
        body.pack(side="top", fill="both", expand=True)
        nav = tk.Frame(body, bg=THEME["panel"], width=180, padx=8, pady=12)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        lbl(nav, APP, THEME["fg"], 12, True).pack(anchor="w", padx=8, pady=(0, 12))
        self.nav_items: dict[str, tk.Label] = {}
        for key, title in PAGES:
            item = tk.Label(nav, text=title, anchor="w", padx=10, pady=6, cursor="hand2", bg=THEME["panel"],
                            fg=THEME["muted"], font=(FONT, fsize(10)))
            item.pack(fill="x", pady=1)
            item.bind("<Button-1>", lambda _e, k=key: self.show(k))
            self.nav_items[key] = item
        self.content = tk.Frame(body, bg=THEME["bg"], padx=18, pady=14)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}

        self._build_providers()
        self._build_add()
        self._build_display()
        self._build_tray()
        self._build_data()
        self._build_about()
        self.show("providers")
        self._position()

    # ----- layout helpers
    def _page(self, key: str, title: str, subtitle: str = "") -> tk.Frame:
        f = tk.Frame(self.content, bg=THEME["bg"])
        head = tk.Frame(f, bg=THEME["bg"])
        head.pack(fill="x", pady=(0, 10))
        lbl(head, title, THEME["fg"], 13, True).pack(side="left")
        f.head = head  # type: ignore[attr-defined]
        if subtitle:
            lbl(f, subtitle, THEME["muted"], 8, wraplength=640).pack(anchor="w", pady=(0, 10))
        self.pages[key] = f
        return f

    def _form_row(self, parent, label: str, help_text: str = "") -> tk.Frame:
        """A labelled row: label on the left, controls in the returned frame, help underneath."""
        row = tk.Frame(parent, bg=THEME["bg"])
        row.pack(fill="x", pady=(0, 10))
        lbl(row, label, THEME["muted"], 9, width=18, anchor="w").pack(side="left", anchor="n")
        right = tk.Frame(row, bg=THEME["bg"])
        right.pack(side="left", fill="x", expand=True)
        ctl = tk.Frame(right, bg=THEME["bg"])
        ctl.pack(fill="x")
        if help_text:
            lbl(right, help_text, THEME["muted"], 8, wraplength=520).pack(anchor="w", pady=(2, 0))
        return ctl

    def show(self, key: str) -> None:
        if key == self.page:
            return
        for k, f in self.pages.items():
            f.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for k, item in self.nav_items.items():
            on = k == key
            item.configure(bg=THEME["button"] if on else THEME["panel"], fg=THEME["fg"] if on else THEME["muted"],
                           font=(FONT, fsize(10), "bold" if on else "normal"))
        self.page = key
        if key == "add":
            self._refresh_picker()
            self.top.after(50, self.pick_search.focus_set)

    def _position(self) -> None:
        top, widget = self.top, self.w
        top.update_idletasks()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        geo = str(self.cfg.get("settings_geometry") or "")
        try:
            size, _, _ = geo.partition("+")
            tw, th = (int(v) for v in size.split("x"))
        except ValueError:
            tw, th = 960, 680
        tw, th = min(max(760, tw), sw - 40), min(max(480, th), sh - 80)
        wx, wy = widget.root.winfo_x(), widget.root.winfo_y()
        x = wx - tw - 12 if wx - tw - 12 > 0 else min(wx + widget.width + 12, sw - tw)
        top.geometry(f"{tw}x{th}+{max(0, x)}+{max(0, min(wy, sh - th - 60))}")
        top.focus_force()

    def alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if self.alive():
            try:  # remember the size for next time (written with the next config save)
                self.w.cfg["settings_geometry"] = f"{self.top.winfo_width()}x{self.top.winfo_height()}"
            except tk.TclError:
                pass
            self.top.destroy()
        import gc
        gc.collect()  # free the dialog's Tk variables here, on the UI thread, not later on a fetch thread

    def _num(self, parent, value, width=6):
        e = entry(parent, value, width)
        e.pack(side="left")
        return e

    # ----- pages
    def _build_providers(self) -> None:
        f = self._page("providers", "My providers")
        button(f.head, "+ Add providers", lambda: self.show("add"), small=True).pack(side="right")
        button(f.head, "Collapse all", lambda: self._expand_all(False), subtle=True, small=True).pack(side="right")
        button(f.head, "Expand all", lambda: self._expand_all(True), subtle=True, small=True).pack(side="right")
        self.row_filter = search_entry(f.head, self._filter_rows, "Filter…", 18)
        self.row_filter.pack(side="right", padx=(0, 8))
        self.filter_note = lbl(f.head, "", THEME["muted"], 8)
        self.filter_note.pack(side="right", padx=(0, 6))
        lbl(f, "Grouped by vendor. Click a provider to open its settings; changes apply when you Save.",
            THEME["muted"], 8).pack(anchor="w", pady=(0, 8))
        self.scroll = ScrollFrame(f, fill=True)
        self.scroll.pack(fill="both", expand=True)
        self.scroll.canvas.bind("<Configure>", lambda _e: self._reflow(), add="+")
        self.prov_box = self.scroll.inner
        self.empty_note = lbl(self.prov_box, "No providers yet. Use “+ Add providers” to pick some.", THEME["muted"], 9)
        for pc in self.cfg.get("providers", []):
            if pc.get("type") in PROVIDERS:
                self._provider_row(pc)
        self._update_counts()

    def _build_add(self) -> None:
        f = self._page("add", "Add providers")
        self.pick_search = search_entry(f.head, self._on_pick_search, "Search name, vendor, plan…", 30)
        self.pick_search.pack(side="right")
        self.pick_search.bind("<Return>", lambda _e: self._pick_enter())
        chips = tk.Frame(f, bg=THEME["bg"])
        chips.pack(fill="x", pady=(0, 8))
        self.kind_chips: dict[str, tk.Label] = {}
        for key, title in KIND_FILTERS:
            c = tk.Label(chips, text=title, padx=10, pady=3, cursor="hand2", font=(FONT, fsize(9)))
            c.pack(side="left", padx=(0, 6))
            c.bind("<Button-1>", lambda _e, k=key: self._set_kind(k))
            self.kind_chips[key] = c
        self._paint_chips()
        lbl(chips, "Grouped by vendor. Changes apply when you Save.", THEME["muted"], 8).pack(side="right")
        self.pick_scroll = ScrollFrame(f, fill=True)
        self.pick_scroll.pack(fill="both", expand=True)
        links = tk.Frame(f, bg=THEME["bg"])
        links.pack(fill="x", pady=(8, 0))
        button(links, "Custom provider how-to", lambda: info_dialog(self.top, "Custom providers", CUSTOM_HELP),
               subtle=True).pack(side="left")
        button(links, "Browser session how-to", lambda: info_dialog(self.top, "Browser sessions", COOKIE_HELP),
               subtle=True).pack(side="left", padx=(8, 0))
        button(links, "Request / contribute a provider",
               lambda: webbrowser.open("https://github.com/chrisdelrey/ai_subusage/blob/main/CONTRIBUTING.md"),
               subtle=True).pack(side="left", padx=(8, 0))

    def _build_display(self) -> None:
        f = self._page("display", "Display", "How the widget looks. The thresholds also color the tray icon.")
        r = self._form_row(f, "Colors", "Bars turn amber, then red, at these percentages.")
        lbl(r, "Amber at", THEME["muted"]).pack(side="left", padx=(0, 4))
        self.v_warn = self._num(r, self.cfg.get("warn_pct", 70), 5)
        lbl(r, "%     Red at", THEME["muted"]).pack(side="left", padx=(0, 4))
        self.v_crit = self._num(r, self.cfg.get("crit_pct", 90), 5)
        lbl(r, "%", THEME["muted"]).pack(side="left")
        r = self._form_row(f, "Size", "Width in pixels (220–700) and window opacity (30–100%).")
        lbl(r, "Width", THEME["muted"]).pack(side="left", padx=(0, 4))
        self.v_width = self._num(r, self.cfg.get("width", 300), 5)
        lbl(r, "px     Opacity", THEME["muted"]).pack(side="left", padx=(0, 4))
        self.v_opacity = self._num(r, int(float(self.cfg.get("opacity", 0.95)) * 100), 5)
        lbl(r, "%", THEME["muted"]).pack(side="left")
        r = self._form_row(f, "View", "Full shows every limit; Condensed one line per limit; Compact one line per provider.")
        self.v_view = choice(r, VIEWS, view_of(self.cfg))
        r = self._form_row(f, "Sort")
        self.v_sort = choice(r, SORTS, self.cfg.get("sort", "manual"))
        r = self._form_row(f, "Window")
        self.v_top = toggle(r, "Always on top", self.cfg.get("always_on_top", True))
        r = self._form_row(f, "Providers")
        self.v_hide = toggle(r, "Hide providers with no login", self.cfg.get("hide_unconfigured", True))
        r = self._form_row(f, "Warnings")
        self.v_proj = toggle(r, "Warn when a limit will run out before it resets", self.cfg.get("projections", True))
        r = self._form_row(f, "Expired logins", "e.g. Hermes when you haven't used it lately; it comes back once you do.")
        self.v_expired = choice(r, EXPIRED, self.cfg.get("expired_display", "hide"))

    def _build_tray(self) -> None:
        from . import tray
        from .core import startup_enabled
        f = self._page("tray", "Tray & startup")
        self.tray_ok = tray.available()
        icon = "Menu bar icon" if IS_MAC else "Tray icon"
        if self.tray_ok:
            r = self._form_row(f, icon, "Click the icon for its menu." if IS_MAC else
                               "Left-click the icon to show/hide the widget; right-click for its menu.")
            self.v_tray = choice(r, TRAY_MODES, self.cfg.get("tray", "both"))
        else:
            r = self._form_row(f, icon, tray.unavailable_reason())
            self.v_tray = tk.StringVar(value=self.cfg.get("tray", "off"))
            lbl(r, "Not available here", THEME["warn"]).pack(side="left")
        r = self._form_row(f, "Background mode", "Starts at login and lives in the tray: the widget starts hidden. "
                           "Show/hide, pause or quit from the icon." + ("" if self.tray_ok else " Needs a tray icon."))
        self.v_background = toggle(r, "Run in background", self.cfg.get("background", False))
        self._startup_was = startup_enabled()
        r = self._form_row(f, "Start at login", "Background mode turns this on for you.")
        self.v_startup = toggle(r, "Start SubUsage when I log in", self._startup_was)
        r = self._form_row(f, "Command line", "Control the running widget from a terminal or a shortcut.")
        lbl(r, "python3 -m subusage --show | --hide | --pause | --resume | --quit", THEME["fg"], 8).pack(side="left")

    def _build_data(self) -> None:
        from .core import log_path
        f = self._page("data", "History & data", "Everything stays on this computer.")
        r = self._form_row(f, "Usage history", "Used for the tooltip charts, History window and time-to-limit estimates.")
        self.v_hist = toggle(r, "Keep 30 days of usage history", self.cfg.get("history", True))
        button(r, "Clear history", self._clear_history, subtle=True, small=True).pack(side="left")
        r = self._form_row(f, "Config file", str(config_path()) + "  (holds your keys and sessions: keep it private)")
        button(r, "Open", lambda: open_path(config_path()), small=True).pack(side="left")
        r = self._form_row(f, "Log file", str(log_path()))
        button(r, "Open", lambda: open_path(log_path()), small=True).pack(side="left")

    def _build_about(self) -> None:
        f = self._page("about", "About & updates")
        self._about(f)

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

    # ----- provider rows (grouped by vendor, collapsible)
    def _group(self, vendor: str) -> dict[str, Any]:
        g = self.groups.get(vendor)
        if g is None:
            frame = tk.Frame(self.prov_box, bg=THEME["bg"])
            frame.pack(fill="x", pady=(0, 6))
            head = tk.Frame(frame, bg=THEME["bg"])
            head.pack(fill="x", pady=(4, 4))
            lbl(head, vendor.upper(), THEME["muted"], 8, True).pack(side="left")
            count = lbl(head, "", THEME["muted"], 8)
            count.pack(side="left", padx=(6, 0))
            g = self.groups[vendor] = {"frame": frame, "count": count, "rows": []}
        return g

    def _live_status(self, row) -> tuple[str, str]:
        """One-line status from the running widget's provider with the same type and name."""
        cls, pc = row["cls"], row["cfg"]
        if not pc.get("enabled", True):
            return "disabled", THEME["muted"]
        p = next((p for p in getattr(self.w, "providers", []) if p.type == cls.type
                  and p.name == (pc.get("name") or cls.default_name)), None)
        if p is None:
            return "not saved yet", THEME["muted"]
        snap = p.snapshot
        if getattr(p, "parked", False):
            return "login expired", THEME["warn"]
        if snap.error:
            return "⚠ " + snap.error[:70] + ("…" if len(snap.error) > 70 else ""), THEME["err"]
        wins = [w for w in snap.windows if w.used_pct is not None]
        if wins:
            top = max(wins, key=lambda w: w.used_pct)
            return f"{top.label} {top.used_pct:.0f}%", THEME["ok"] if top.used_pct < 70 else THEME["warn"]
        if snap.notes:
            return snap.notes[0][:60], THEME["fg"]
        return "not checked yet", THEME["muted"]

    def _provider_row(self, pc: dict[str, Any], expanded: bool = False) -> dict[str, Any]:
        cls: type[Provider] = PROVIDERS[pc["type"]]
        P = THEME["panel"]
        vendor = vendor_of(cls)
        g = self._group(vendor)
        f = tk.Frame(g["frame"], bg=P, padx=10, pady=6)
        f.pack(fill="x", pady=(0, 4))
        row: dict[str, Any] = {"cfg": pc, "cls": cls, "frame": f, "inputs": {}, "vendor": vendor, "built": False}

        head = tk.Frame(f, bg=P, cursor="hand2")
        head.pack(fill="x")
        chev = lbl(head, "▸", THEME["muted"], 9, cursor="hand2")
        chev.pack(side="left", padx=(0, 6))
        dot = tk.Canvas(head, width=8, height=8, bg=P, highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=cls.accent, width=0)
        dot.pack(side="left", padx=(0, 6))
        row["enabled"] = toggle(head, "", pc.get("enabled", True), bg=P)
        title = lbl(head, pc.get("name") or cls.default_name, THEME["fg"], 10, True, cursor="hand2")
        title.pack(side="left")
        tag = KIND_TAG.get(cls.auth, "")
        lbl(head, f"  {tag}", THEME["primary_hover"] if cls.auth == "api" else THEME["muted"], 8).pack(side="left")
        button(head, "Remove", lambda: self._remove(row), subtle=True, small=True).pack(side="right")
        text, color = self._live_status(row)
        row["live"] = lbl(head, text, color, 8)
        row["live"].pack(side="right", padx=(0, 10))
        row["chev"], row["title"] = chev, title
        for w in (head, chev, title):
            w.bind("<Button-1>", lambda _e: self._toggle_row(row))

        row["body"] = tk.Frame(f, bg=P)
        g["rows"].append(row)
        self.rows.append(row)
        if expanded:
            self._toggle_row(row, True)
        self._update_counts()
        return row

    def _build_body(self, row) -> None:
        """Fields are built the first time a row is opened (keeps the window fast with many providers)."""
        cls, pc, P = row["cls"], row["cfg"], THEME["panel"]
        b = row["body"]
        line = tk.Frame(b, bg=P)
        line.pack(fill="x", pady=(6, 0))
        lbl(line, "Name", THEME["muted"], width=14, anchor="w").pack(side="left")
        row["name"] = entry(line, pc.get("name") or cls.default_name, 22)
        row["name"].pack(side="left")
        row["name"].bind("<KeyRelease>", lambda _e: row["title"].configure(text=row["name"].get() or cls.default_name))
        lbl(line, "   Check every", THEME["muted"]).pack(side="left")
        row["poll"] = entry(line, pc.get("poll_seconds", cls.default_poll), 6)
        row["poll"].pack(side="left", padx=(4, 2))
        lbl(line, "s", THEME["muted"]).pack(side="left")
        if getattr(cls, "key_url", ""):
            button(line, "Get key ↗", lambda u=cls.key_url: webbrowser.open(u), small=True).pack(side="right")
        if cls.usage_url:
            button(line, "Usage page ↗", lambda u=cls.usage_url: webbrowser.open(u), subtle=True,
                   small=True).pack(side="right", padx=(0, 6))
        lbl(b, f"{cls.vendor} · {cls.plans}" if cls.vendor else cls.plans, THEME["muted"], 8).pack(anchor="w",
                                                                                                  pady=(6, 0))
        if cls.setup:
            row["setup"] = lbl(b, cls.setup, THEME["fg"], 8, wraplength=self._wrap())
            row["setup"].pack(anchor="w", pady=(2, 0))
        for fld in cls.fields:
            self._field(row, fld)
        if pc.get("hidden"):
            self._hidden_line(row)
        foot = tk.Frame(b, bg=P)
        foot.pack(fill="x", pady=(8, 2))
        button(foot, "Test", lambda: self._test(row), small=True).pack(side="left")
        row["status"] = lbl(foot, "", THEME["muted"], 8, wraplength=self._wrap() - 80)
        row["status"].pack(side="left", padx=(8, 0))
        row["built"] = True

    def _wrap(self) -> int:
        try:
            return max(420, self.scroll.canvas.winfo_width() - 60)
        except tk.TclError:
            return 620

    def _reflow(self) -> None:
        """Re-wrap the long help texts when the window is resized."""
        w = self._wrap()
        for r in self.rows:
            for key, width in (("setup", w), ("status", w - 80)):
                if r.get(key) is not None:
                    try:
                        r[key].configure(wraplength=width)
                    except tk.TclError:
                        pass

    def _toggle_row(self, row, open_: Optional[bool] = None) -> None:
        want = (not row["body"].winfo_ismapped()) if open_ is None else open_
        if want:
            if not row["built"]:
                self._build_body(row)
            row["body"].pack(fill="x")
            row["chev"].configure(text="▾")
        else:
            row["body"].pack_forget()
            row["chev"].configure(text="▸")

    def _expand_all(self, open_: bool) -> None:
        for r in self.rows:
            if r["frame"].winfo_ismapped() or not open_:
                self._toggle_row(r, open_)

    def _update_counts(self) -> None:
        for vendor, g in list(self.groups.items()):
            n = len(g["rows"])
            if not n:
                g["frame"].destroy()
                del self.groups[vendor]
                continue
            g["count"].configure(text=f"· {n}" if n > 1 else "")
        self.nav_items["providers"].configure(text=f"My providers ({len(self.rows)})") \
            if hasattr(self, "nav_items") else None
        if self.rows:
            self.empty_note.pack_forget()
        else:
            self.empty_note.pack(anchor="w", pady=20)

    def _field(self, row, fld) -> None:
        P = THEME["panel"]
        f = row["body"]
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
            lbl(f, hint, THEME["muted"], 7, wraplength=self._wrap()).pack(anchor="w", padx=(110, 0))

    def _hidden_line(self, row) -> None:
        P = THEME["panel"]
        line = tk.Frame(row["body"], bg=P)
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
        if not row["built"]:
            self._toggle_row(row, True)
        if row["status"].winfo_exists():
            row["status"].configure(text=text, fg=color or THEME["muted"])

    def _set(self, row, key, value: str) -> None:
        if not row["built"]:
            self._build_body(row)
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
        if not row["built"]:  # never opened: nothing edited except the enabled checkbox
            return pc
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
        g = self.groups.get(row["vendor"])
        if g and row in g["rows"]:
            g["rows"].remove(row)
        self._update_counts()
        self._refresh_picker()

    def add_provider(self, ptype: str) -> None:
        cls = PROVIDERS[ptype]
        row = self._provider_row({"type": ptype, "enabled": True, "name": cls.default_name,
                                  "poll_seconds": cls.default_poll}, expanded=True)
        row["live"].configure(text="new: fill in and Save", fg=THEME["warn"])
        self._refresh_picker()
        if self.page == "providers":
            self.top.after(60, lambda: self.scroll.scroll_to(row["frame"]))

    def save(self) -> None:
        def num(e, default, lo, hi):
            try:
                return max(lo, min(hi, float(e.get())))
            except ValueError:
                return default
        from .core import log, set_startup
        cfg = self.cfg
        # Saved in the order shown: grouped by vendor, so the widget lists them the same way.
        cfg["providers"] = [self._row_cfg(r) for g in self.groups.values() for r in g["rows"]]
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
        want_startup = bool(self.v_startup.get())
        if cfg["background"] != was_bg:
            want_startup = cfg["background"]  # background mode = start at login; turning it off undoes that
        if want_startup != self._startup_was:
            try:
                set_startup(want_startup)
            except OSError as e:
                log(f"start at login failed: {e}")
        cfg["projections"] = bool(self.v_proj.get())
        cfg["history"] = bool(self.v_hist.get())
        cfg.pop("compact", None)
        cfg["check_updates"] = bool(self.v_updates.get())
        cfg["hide_unconfigured"] = bool(self.v_hide.get())
        try:
            cfg["settings_geometry"] = f"{self.top.winfo_width()}x{self.top.winfo_height()}"
        except tk.TclError:
            pass
        self.close()
        self.w.apply_config(cfg)

    # ----- "Add providers" page
    def open_picker(self) -> None:
        self.top.lift()
        self.show("add")

    def show_about(self) -> None:
        self.top.lift()
        self.show("about")
        info = self.w.update_info
        if info is None or time.time() - info.checked_at > 3600:
            self._check_update()
        else:
            self._show_update_state()

    def _set_kind(self, kind: str) -> None:
        self.pick_kind = kind
        self._paint_chips()
        self._refresh_picker()

    def _paint_chips(self) -> None:
        for k, c in self.kind_chips.items():
            on = k == self.pick_kind
            c.configure(bg=THEME["primary"] if on else THEME["button"], fg=THEME["fg"])

    def _pick_matches(self) -> list[type[Provider]]:
        q = self.pick_query
        return [c for c in PROVIDERS.values() if kind_matches(c, self.pick_kind)
                and matches(q, c.default_name, c.vendor, c.plans, c.setup, c.type, KIND_TAG.get(c.auth, ""),
                            " ".join(getattr(c, "old_names", ()) or ()))]

    def _refresh_picker(self) -> None:
        if not hasattr(self, "pick_scroll") or self.page != "add":
            return
        box = self.pick_scroll.inner
        for c in box.winfo_children():
            c.destroy()
        added = {r["cls"].type for r in self.rows}
        hits = self._pick_matches()
        self._pick_hits = hits
        by_vendor: dict[str, list[type[Provider]]] = {}
        for c in hits:
            by_vendor.setdefault(vendor_of(c), []).append(c)
        for vendor in sorted(by_vendor, key=lambda v: (v in ("You", "Other"), v.lower())):
            lbl(box, vendor.upper(), THEME["muted"], 8, True).pack(anchor="w", pady=(8, 4))
            for cls in sorted(by_vendor[vendor], key=lambda c: (c.auth == "api", c.default_name.lower())):
                self._picker_item(box, cls, cls.type in added)
        cat = [c for c in CATALOG if matches(self.pick_query, *c)] if self.pick_kind in ("all", "sub") else []
        if cat:
            lbl(box, "NOT BUILT IN YET: USE A CUSTOM PROVIDER OR CONTRIBUTE ONE", THEME["muted"], 8, True).pack(
                anchor="w", pady=(12, 4))
            for name, vendor, source in cat:
                r = tk.Frame(box, bg=THEME["bg"])
                r.pack(fill="x", pady=1)
                lbl(r, f"{name}", THEME["fg"], 9).pack(side="left")
                lbl(r, f"  {vendor} · {source}", THEME["muted"], 8).pack(side="left")
        if not hits and not cat:
            lbl(box, f"Nothing matches “{self.pick_query}”. You can still track it with a Custom provider.",
                THEME["muted"], 9, wraplength=560).pack(anchor="w", pady=(12, 4))
        self.pick_scroll.canvas.yview_moveto(0)

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
        for g in self.groups.values():
            g["frame"].pack_forget()
            for r in g["rows"]:
                r["frame"].pack_forget()
        for g in self.groups.values():
            vis = []
            for r in g["rows"]:
                cls = r["cls"]
                name = r["name"].get() if r.get("name") else (r["cfg"].get("name") or cls.default_name)
                if matches(q, name, cls.default_name, cls.vendor, cls.plans, cls.type, KIND_TAG.get(cls.auth, "")):
                    vis.append(r)
            if vis:
                g["frame"].pack(fill="x", pady=(0, 6))
                for r in vis:
                    r["frame"].pack(fill="x", pady=(0, 4))
                shown += len(vis)
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
        tag = KIND_TAG.get(cls.auth, "")
        lbl(top, f"  {tag}", THEME["primary_hover"] if cls.auth == "api" else THEME["muted"], 8).pack(side="left")
        lbl(top, f"  ·  {cls.plans}", THEME["muted"], 8).pack(side="left")
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
        lbl(r, cls.setup, THEME["muted"], 8, wraplength=max(420, self.pick_scroll.canvas.winfo_width() - 60)).pack(
            anchor="w", pady=(2, 0))
