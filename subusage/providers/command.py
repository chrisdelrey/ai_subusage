"""Custom provider: run any command that prints usage JSON. See CONTRIBUTING.md for the format."""
from __future__ import annotations

import json
import subprocess
import time

from ..core import (NO_WINDOW, Field, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow, fnum,
                    parse_time)

CUSTOM_HELP = """\
A custom provider runs a command you write and shows what it prints. The command must print JSON like:

{"plan": "Pro",
 "windows": [{"label": "Daily", "used_pct": 42.5,
              "resets_at": "2026-11-17T00:00:00Z", "length_seconds": 86400}],
 "notes": ["optional extra line"]}

• used_pct is 0–100. resets_at can be ISO-8601, epoch seconds or epoch ms.
• length_seconds is optional; it enables the pace marker.
• Exit non-zero (message on stderr) to show an error under the provider.
• Any language works: python my_usage.py, node usage.js, a shell script…

See examples/custom_provider.py in the repo for a starting point."""


class CommandProvider(Provider):
    type = "command"
    default_name = "Custom"
    vendor = "You"
    plans = "Anything with a usage number"
    auth = "command"
    setup = "Runs a command that prints usage JSON. Click “How to” for the format."
    accent = "#5aa9e6"
    multi = True
    parks_on_auth = False
    fields = (
        Field("command", "Command", "command", "e.g. python ~/bin/my_usage.py"),
        Field("usage_url", "Usage page URL", "text", "Optional: opened on double-click."),
    )

    def fetch(self) -> Snapshot:
        cmd = self.setting("command")
        if not cmd:
            raise NotConfigured("No command configured")
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                 timeout=int(fnum(self.cfg.get("timeout_seconds")) or 30), creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ProviderError("Command timed out") from None
        if out.returncode != 0:
            raise ProviderError(f"Command failed ({out.returncode}): {out.stderr.strip()[:160]}")
        try:
            j = json.loads(out.stdout)
        except ValueError:
            raise ProviderError("Command didn't print valid JSON") from None
        snap = Snapshot(plan=str(j.get("plan") or ""), notes=[str(n) for n in j.get("notes") or []])
        for w in j.get("windows") or []:
            pct = fnum(w.get("used_pct"))
            snap.windows.append(UsageWindow(str(w.get("label") or "Usage"),
                                            None if pct is None else max(0.0, min(100.0, pct)),
                                            parse_time(w.get("resets_at")), fnum(w.get("length_seconds")),
                                            str(w.get("detail") or "")))
        return snap


class DemoProvider(Provider):
    """Sample data for --demo and screenshots."""
    type = "demo"

    def __init__(self, cfg, windows, plan, notes=None, accent="#d97757"):
        super().__init__(cfg)
        self.accent = accent
        self._w, self._plan, self._notes = windows, plan, notes or []

    def fetch(self) -> Snapshot:
        now = time.time()
        snap = Snapshot(plan=self._plan, notes=list(self._notes))
        for label, pct, reset_in, length in self._w:
            snap.windows.append(UsageWindow(label, pct, now + reset_in if reset_in else None, length))
        return snap


def demo_providers() -> list[Provider]:
    h, d = 3600, 86400
    return [
        DemoProvider({"name": "Claude"}, [("Session (5h)", 37, 2 * h + 840, 5 * h), ("Weekly", 58, 2 * d + 5 * h, 7 * d),
                                          ("Weekly · Opus", 12, 2 * d + 5 * h, 7 * d)], "Max", accent="#d97757"),
        DemoProvider({"name": "ChatGPT"}, [("Session (5h)", 91, 2820, 5 * h), ("Weekly", 44, 4 * d, 7 * d)], "Plus",
                     accent="#10a37f"),
        DemoProvider({"name": "Copilot"}, [("Premium requests", 64, 14 * d, 30 * d)], "Pro", accent="#8250df"),
        DemoProvider({"name": "Gemini"}, [("Pro (daily)", 22, 9 * h, d), ("Flash (daily)", 5, 9 * h, d)],
                     "Google AI Pro", accent="#4285f4"),
        DemoProvider({"name": "Nous"}, [("Today", 18, 9 * h, d), ("This week", 71, 3 * d, 7 * d)], "Plus",
                     ["Top-up credits: $4.20"], accent="#8b7cf6"),
    ]
