"""JetBrains AI (AI Pro / AI Ultimate, AI Assistant and Junie), read from the IDE's local quota file.

No network: JetBrains IDEs persist the quota state in options/AIAssistantQuotaManager2.xml. It's only
as fresh as the IDE's last save, so open an IDE now and then.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from ..core import (IS_MAC, IS_WIN, Field, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow,
                    app_data_dirs, expand, fnum, parse_time)

QUOTA_FILE = "AIAssistantQuotaManager2.xml"
UNITS_PER_CREDIT = 100_000


def config_roots() -> list[Path]:
    roots = []
    for base in app_data_dirs():
        roots += [base / "JetBrains", base / "Google"]  # Google = Android Studio
    if not IS_WIN and not IS_MAC:
        roots += [Path.home() / ".config" / "JetBrains", Path.home() / ".config" / "Google"]
    return [r for r in dict.fromkeys(roots) if r.is_dir()]


def find_quota_files() -> list[Path]:
    files = []
    for root in config_roots():
        files += root.glob(f"*/options/{QUOTA_FILE}")
    return sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)


def _duration_seconds(iso: Any) -> Optional[float]:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", str(iso or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60


class JetBrainsProvider(Provider):
    type = "jetbrains"
    default_name = "JetBrains AI"
    vendor = "JetBrains"
    plans = "AI Free, AI Pro, AI Ultimate"
    setup = "Sign in to AI Assistant or Junie in any JetBrains IDE. Reads the IDE's local quota file (no network)."
    usage_url = "https://account.jetbrains.com/licenses"
    accent = "#fe2857"
    default_poll = 600
    path_key = "quota_path"
    login_what = "JetBrains AI quota file"
    fields = (Field("quota_path", "Quota file", "path", f"Blank = newest {QUOTA_FILE} from any installed IDE."),)

    @classmethod
    def default_path(cls) -> Optional[Path]:
        files = find_quota_files()
        return files[0] if files else None

    @classmethod
    def find_candidates(cls) -> list[Path]:
        return find_quota_files()

    def fetch(self) -> Snapshot:
        custom = self.setting(self.path_key)
        path = expand(custom) if custom else self.default_path()
        if not path or not path.exists():
            raise NotConfigured("No JetBrains AI quota file found; sign in to AI Assistant in a JetBrains IDE")
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as e:
            raise ProviderError(f"Couldn't read {path.name}: {e}") from None
        opts = {o.get("name"): o.get("value") for o in root.iter("option")}
        try:
            quota = json.loads(opts.get("quotaInfo") or "{}")
            refill = json.loads(opts.get("nextRefill") or "{}")
        except ValueError:
            raise ProviderError("Unexpected quota format in the JetBrains file") from None
        snap = self.parse(quota, refill)
        ide = path.parent.parent.name.rstrip("0123456789.")
        snap.plan = f"via {ide}" if ide else ""
        return snap

    @staticmethod
    def parse(quota: dict[str, Any], refill: dict[str, Any]) -> Snapshot:
        tariff = quota.get("tariffQuota") or {}
        cur, mx = fnum(tariff.get("current")), fnum(tariff.get("maximum"))
        if cur is None or not mx:
            cur, mx = fnum(quota.get("current")), fnum(quota.get("maximum"))
        if cur is None or not mx:
            raise ProviderError("JetBrains quota file has no monthly credits")
        reset = parse_time(refill.get("next")) or parse_time(quota.get("until"))
        length = _duration_seconds((refill.get("tariff") or {}).get("duration")) or (30 * 86400 if reset else None)
        snap = Snapshot()
        snap.windows.append(UsageWindow("Monthly credits", max(0.0, min(100.0, cur / mx * 100)), reset, length,
                                        f"{cur / UNITS_PER_CREDIT:,.2f} of {mx / UNITS_PER_CREDIT:,.2f}"))
        topup = fnum((quota.get("topUpQuota") or {}).get("available"))
        if topup:
            snap.notes.append(f"Top-up credits: {topup / UNITS_PER_CREDIT:,.2f}")
        return snap
