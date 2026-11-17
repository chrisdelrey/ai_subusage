"""Subscriptions that authenticate with an API key you paste in Settings.

Each one is small; they live together to keep the tree tidy. Keys may be written as env:VAR_NAME
to read them from the environment instead of storing them in the config file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..core import (AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow, fnum,
                    http_json, normalize_quota, parse_time, title_case)


# --------------------------------------------------------------------------------------------------
# z.ai / Zhipu GLM Coding Plan
# --------------------------------------------------------------------------------------------------

class ZaiProvider(Provider):
    parks_on_auth = False  # a wrong key should stay visible
    type = "zai"
    default_name = "GLM (z.ai)"
    vendor = "Z.ai / Zhipu"
    plans = "GLM Coding Lite, Pro, Max"
    auth = "key"
    setup = "Create an API key at z.ai (or open.bigmodel.cn) and paste it in Settings."
    usage_url = "https://z.ai/manage-apikey/coding-plan/personal/my-plan"
    accent = "#3b82f6"
    fields = (
        Field("api_key", "API key", "secret", "From z.ai → API keys. env:ZAI_API_KEY works too."),
        Field("region", "Region", "choice", options=(("global", "Global (api.z.ai)"),
                                                      ("china", "China (open.bigmodel.cn)")), default="global"),
    )
    UNIT_MIN = {1: 1440, 3: 60, 5: 1, 6: 10080}

    def fetch(self) -> Snapshot:
        key = self.require_key()
        base = "https://open.bigmodel.cn" if self.setting("region") == "china" else "https://api.z.ai"
        j = http_json(f"{base}/api/monitor/usage/quota/limit", {"Authorization": f"Bearer {key}"})
        return self.parse(j)

    @classmethod
    def parse(cls, j: dict[str, Any]) -> Snapshot:
        if j.get("success") is not True:
            raise ProviderError(f"z.ai: {j.get('msg') or 'no Coding Plan on this key'}")
        data = j.get("data") or {}
        plan = next((data.get(k) for k in ("planName", "plan", "plan_type", "packageName", "level") if data.get(k)), "")
        snap = Snapshot(plan=title_case(plan))
        rows = []
        for lim in data.get("limits") or []:
            if not isinstance(lim, dict):
                continue
            kind = lim.get("type")
            pct = fnum(lim.get("percentage"))
            usage, rem, cur = fnum(lim.get("usage")), fnum(lim.get("remaining")), fnum(lim.get("currentValue"))
            if usage and usage > 0 and (rem is not None or cur is not None):
                used = cur if cur is not None else usage - (rem or 0)
                pct = used / usage * 100
            if pct is None:
                continue
            unit, num = lim.get("unit"), fnum(lim.get("number")) or 0
            minutes = num * cls.UNIT_MIN[unit] if unit in cls.UNIT_MIN and num > 0 else None
            if kind == "TIME_LIMIT":
                label, minutes = "MCP calls (mo)", 30 * 1440
            elif minutes == 300:
                label = "Session (5h)"
            elif minutes == 10080:
                label = "Weekly"
            elif minutes:
                label = f"{int(minutes // 60)}h window" if minutes < 1440 else f"{int(minutes // 1440)}d window"
            else:
                label = "Quota"
            rows.append((minutes or 10 ** 9, kind == "TIME_LIMIT",
                         UsageWindow(label, max(0.0, min(100.0, pct)), parse_time(lim.get("nextResetTime")),
                                     minutes * 60 if minutes else None)))
        for _, _, w in sorted(rows, key=lambda r: (r[1], r[0])):
            snap.windows.append(w)
        if not snap.windows:
            raise ProviderError("z.ai returned no Coding Plan limits for this key")
        return snap


# --------------------------------------------------------------------------------------------------
# Kimi Code (Moonshot)
# --------------------------------------------------------------------------------------------------

class KimiProvider(Provider):
    parks_on_auth = False  # a wrong key should stay visible
    type = "kimi"
    default_name = "Kimi Code"
    vendor = "Moonshot AI"
    plans = "Moderato, Allegretto, Allegro"
    auth = "key"
    setup = "Create an API key in the Kimi Code console and paste it in Settings."
    usage_url = "https://www.kimi.com/code/console"
    accent = "#111827"
    fields = (
        Field("api_key", "API key", "secret", "From kimi.com/code/console. env:KIMI_API_KEY works too."),
        Field("region", "Region", "choice", options=(("china", "kimi.com"), ("global", "kimi.ai")), default="china"),
    )
    LEVELS = {"LEVEL_FREE": "Adagio", "LEVEL_TRIAL": "Andante", "LEVEL_BASIC": "Moderato",
              "LEVEL_INTERMEDIATE": "Allegretto", "LEVEL_ADVANCED": "Allegro"}

    def fetch(self) -> Snapshot:
        key = self.require_key()
        host = "api.kimi.ai" if self.setting("region") == "global" else "api.kimi.com"
        self.usage_url = self.cfg.get("usage_url") or (
            "https://www.kimi.ai/code/console" if host == "api.kimi.ai" else type(self).usage_url)
        return self.parse(http_json(f"https://{host}/coding/v1/usages", {"Authorization": f"Bearer {key}"}))

    @classmethod
    def parse(cls, j: dict[str, Any]) -> Snapshot:
        level = ((j.get("user") or {}).get("membership") or {}).get("level") or ""
        snap = Snapshot(plan=cls.LEVELS.get(level, title_case(level.replace("LEVEL_", ""))))
        pools = j.get("usages") if isinstance(j.get("usages"), dict) else {}
        for key, label, length in (("limit_5h", "Session (5h)", 5 * 3600), ("limit_7d", "Weekly", 7 * 86400),
                                   ("limit_month_total", "Monthly", 30 * 86400)):
            p = pools.get(key)
            ratio = fnum(p.get("used_ratio")) if isinstance(p, dict) else None
            if ratio is not None:
                snap.windows.append(UsageWindow(label, max(0.0, min(100.0, ratio * 100)),
                                                parse_time(p.get("reset_time")), length))
        if not snap.windows:  # older count-based response
            q = normalize_quota(j.get("usage"))
            if q:
                snap.windows.append(UsageWindow("Weekly", q[0], q[1], 7 * 86400))
        if not snap.windows:
            raise ProviderError("Kimi returned no usage for this key")
        return snap


# --------------------------------------------------------------------------------------------------
# Synthetic
# --------------------------------------------------------------------------------------------------

class SyntheticProvider(Provider):
    parks_on_auth = False  # a wrong key should stay visible
    type = "synthetic"
    default_name = "Synthetic"
    vendor = "Synthetic"
    plans = "Standard, Pro"
    auth = "key"
    setup = "Create an API key at synthetic.new and paste it in Settings."
    usage_url = "https://synthetic.new/billing"
    accent = "#f97316"
    fields = (Field("api_key", "API key", "secret", "env:SYNTHETIC_API_KEY works too."),)

    def fetch(self) -> Snapshot:
        key = self.require_key()
        return self.parse(http_json("https://api.synthetic.new/v2/quotas", {"Authorization": f"Bearer {key}"}))

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        root = j.get("data") if isinstance(j.get("data"), dict) else j
        snap = Snapshot(plan=title_case(root.get("plan") or root.get("planName") or ""))
        lanes = [("planWeeklyLimit", "Weekly", 7 * 86400), ("weeklyTokenLimit", "Weekly", 7 * 86400),
                 ("rollingFiveHourLimit", "Session (5h)", 5 * 3600)]
        seen = set()
        for key, label, length in lanes:
            q = normalize_quota(root.get(key))
            if q and label not in seen:
                seen.add(label)
                snap.windows.append(UsageWindow(label, q[0], q[1], length))
        q = normalize_quota((root.get("search") or {}).get("hourly") if isinstance(root.get("search"), dict) else None)
        if q:
            snap.windows.append(UsageWindow("Search (hourly)", q[0], q[1], 3600))
        if not snap.windows:
            q = normalize_quota(root.get("subscription"))
            if q:
                snap.windows.append(UsageWindow("Subscription", q[0], q[1]))
        if not snap.windows:
            raise ProviderError("Synthetic returned no quota data")
        return snap


# --------------------------------------------------------------------------------------------------
# ClinePass
# --------------------------------------------------------------------------------------------------

class ClinePassProvider(Provider):
    type = "clinepass"
    default_name = "ClinePass"
    vendor = "Cline"
    plans = "ClinePass"
    auth = "auto"
    setup = "Run `cline auth` (browser sign-in), or paste a Cline API key in Settings."
    usage_url = "https://app.cline.bot/dashboard"
    accent = "#22c55e"
    login_dir, login_file, login_what = ".cline", "data/settings/providers.json", "Cline login"
    fields = (
        Field("api_key", "API key", "secret", "Optional; blank = use the `cline auth` login."),
        Field("auth_path", "Cline settings file", "path", "Blank = ~/.cline/data/settings/providers.json"),
    )

    @property
    def parks_on_auth(self) -> bool:  # type: ignore[override]
        return not self.setting("api_key")  # only park the `cline auth` login, not a pasted key

    def _token(self) -> str:
        key = self.setting("api_key")
        if key:
            return key
        p: Optional[Path] = self.login_path()
        if not p or not p.exists():
            raise NotConfigured("No Cline login; run `cline auth` or paste an API key")
        try:
            settings = (((json.loads(p.read_text(encoding="utf-8")).get("providers") or {})
                         .get("cline") or {}).get("settings") or {})
        except (OSError, ValueError):
            raise ProviderError("Couldn't read Cline's providers.json") from None
        auth = settings.get("auth") or {}
        if auth.get("accessToken"):
            t = str(auth["accessToken"])
            return t if t.startswith("workos:") else f"workos:{t}"
        key = settings.get("apiKey") or auth.get("apiKey")
        if not key:
            raise NotConfigured("No Cline login; run `cline auth` or paste an API key")
        return str(key)

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.cline.bot/api/v1/users/me/plan/usage-limits",
                          {"Authorization": f"Bearer {self._token()}"})
        except HttpError as e:
            if e.status in (401, 403):
                raise AuthExpired("Cline login expired or was rejected; run `cline auth` again") from None
            raise
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        if j.get("success") is not True:
            raise ProviderError("ClinePass: unexpected response")
        snap = Snapshot(plan="ClinePass")
        names = {"five_hour": ("Session (5h)", 5 * 3600), "weekly": ("Weekly", 7 * 86400),
                 "monthly": ("Monthly", 30 * 86400)}
        for lim in (j.get("data") or {}).get("limits") or []:
            if isinstance(lim, dict) and lim.get("type") in names and fnum(lim.get("percentUsed")) is not None:
                label, length = names[lim["type"]]
                snap.windows.append(UsageWindow(label, max(0.0, min(100.0, float(lim["percentUsed"]))),
                                                parse_time(lim.get("resetsAt")), length))
        if not snap.windows:
            raise ProviderError("ClinePass returned no limits")
        return snap
