"""Entry point: python -m subusage [--demo | --once [--json] | --settings]"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import APP, __version__  # noqa: F401
from .core import IS_WIN, acquire_single_instance, build_providers, fmt_delta, load_config


def print_once(providers) -> int:
    rc = 0
    for p in providers:
        p.run_fetch()
        s = p.snapshot
        status = "not configured" if not p.configured else (s.plan or "")
        print(f"\n{p.name} [{status}]")
        for w in s.windows:
            reset = f"  resets in {fmt_delta(w.resets_at - time.time())}" if w.resets_at else ""
            pct = f"{w.used_pct:5.1f}%" if w.used_pct is not None else "   n/a"
            print(f"  {w.label:<20} {pct}{reset}  {w.detail}")
        for n in s.notes:
            print(f"  {n}")
        if s.error:
            print(f"  ! {s.error}")
            rc = rc or (1 if p.configured else 0)
    return rc


def set_dpi_aware() -> None:
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="subusage", description=f"{APP} {__version__}: AI subscription usage widget")
    ap.add_argument("--demo", action="store_true", help="show sample data, no logins needed")
    ap.add_argument("--once", action="store_true", help="fetch once, print to the console, exit")
    ap.add_argument("--json", action="store_true", help="with --once: print JSON")
    ap.add_argument("--settings", action="store_true", help="open the Settings window on launch")
    ap.add_argument("--check-update", action="store_true", help="check GitHub for a newer version, then exit")
    ap.add_argument("--update", action="store_true", help="install the latest version from GitHub, then exit")
    ap.add_argument("--version", action="store_true", help="print version and build, then exit")
    args = ap.parse_args(argv)

    if args.version or args.check_update or args.update:
        from . import update
        print(f"{APP} {update.version_string()}  [{update.install_kind()}]")
        if not (args.check_update or args.update):
            return 0
        try:
            info = update.check()
        except Exception as e:
            print(f"Couldn't check for updates: {e}", file=sys.stderr)
            return 1
        if not info.available:
            print(f"Up to date (latest: {info.latest}).")
            return 0
        print(f"Update available: {info.latest}  {info.url}")
        if args.update:
            try:
                print(update.apply(info) + ". Restart the widget to use it.")
            except Exception as e:
                print(f"Update failed: {e}", file=sys.stderr)
                return 1
        return 0

    cfg = load_config()
    if args.demo:
        from .providers.command import demo_providers
        providers = demo_providers()
    else:
        providers = build_providers(cfg)

    if args.once:
        if args.json:
            for p in providers:
                p.run_fetch()
            print(json.dumps({p.name: {"type": p.type, "configured": p.configured, "plan": p.snapshot.plan,
                                       "error": p.snapshot.error, "notes": p.snapshot.notes,
                                       "windows": [w.__dict__ for w in p.snapshot.windows]}
                              for p in providers}, indent=2))
            return 0
        return print_once(providers)

    if not args.demo and not acquire_single_instance():
        print(f"{APP} is already running.", file=sys.stderr)
        return 0
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is missing. On Ubuntu: sudo apt install python3-tk  ·  "
              "Homebrew Python: brew install python-tk", file=sys.stderr)
        return 1
    set_dpi_aware()
    from .ui import Widget
    w = Widget(providers, cfg, demo=args.demo)
    if args.settings:
        w.root.after(300, w.open_settings)
    w.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
