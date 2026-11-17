"""GitHub plan allowances (Free / Pro personal accounts) from GitHub's official billing usage API.

Shows this month's Actions minutes and Codespaces compute/storage against what your plan includes,
what's been billed beyond the plan, and Copilot premium requests beyond your allowance. (Copilot's
own monthly allowance is the separate "GitHub Copilot" provider.)

The API reports usage but not the plan's included amounts, so those come from GitHub's published
plan table below. Needs a fine-grained token with Account permissions → Plan: Read-only.
Usage billed to an organization or enterprise isn't included.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from typing import Any, Optional

from ..core import (Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow, fnum,
                    http_json, run_cmd, which)

API = "https://api.github.com"

# Included monthly usage for personal accounts (docs.github.com: "Product usage included with each plan").
INCLUDED = {
    "free": {"actions_minutes": 2000, "codespaces_core_hours": 120, "codespaces_gb_months": 15},
    "pro": {"actions_minutes": 3000, "codespaces_core_hours": 180, "codespaces_gb_months": 20},
}
# Standard runners count against included minutes with these multipliers; larger runners don't count.
MINUTE_MULTIPLIER = (("macos", 10), ("windows", 2))
_CORES = re.compile(r"(\d+)\s*-?\s*core")


def _qty(item: dict[str, Any]) -> float:
    for k in ("grossQuantity", "quantity", "netQuantity"):
        v = fnum(item.get(k))
        if v is not None:
            return v
    return 0.0


def month_bounds(now: dt.datetime) -> tuple[float, float]:
    start = dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    days = calendar.monthrange(now.year, now.month)[1]
    return start.timestamp(), start.timestamp() + days * 86400


class GitHubProvider(Provider):
    type = "github"
    default_name = "GitHub"
    vendor = "GitHub"
    plans = "Free, Pro: Actions, Codespaces, overage"
    auth = "key"
    parks_on_auth = False
    setup = ("Create a fine-grained token (github.com → Settings → Developer settings → Fine-grained tokens) "
             "with Account permissions → Plan: Read-only, and paste it in Settings.")
    usage_url = "https://github.com/settings/billing/usage"
    accent = "#c9d1d9"
    default_poll = 1800  # GitHub's billing data updates a few times an hour at most
    min_poll = 600
    fields = (
        Field("token", "GitHub token", "secret",
              "Fine-grained token with Plan (read). env:GITHUB_BILLING_TOKEN works. Blank = try `gh auth token`."),
        Field("plan", "Plan", "choice", options=(("auto", "Detect from account"), ("free", "GitHub Free"),
                                                  ("pro", "GitHub Pro")), default="auto"),
    )

    _me: dict[str, dict[str, Any]] = {}  # token suffix -> /user response

    def _token(self) -> tuple[str, str]:
        tok = self.setting("token")
        if tok:
            return tok, "settings"
        gh = which(["gh", "gh.exe"])
        out = run_cmd([gh, "auth", "token"], timeout=10) if gh else None
        if out and out.strip():
            return out.strip(), "gh"
        raise NotConfigured("No GitHub token; paste a fine-grained token with Plan (read) in Settings")

    def fetch(self) -> Snapshot:
        token, source = self._token()
        h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        me = self._me.get(token[-12:])
        if me is None:
            me = http_json(f"{API}/user", h)
            self._me[token[-12:]] = me
        login = me.get("login")
        if not login:
            raise ProviderError("Couldn't identify the GitHub account for this token")
        plan = self.setting("plan") or "auto"
        if plan == "auto":
            plan = str((me.get("plan") or {}).get("name") or "free").lower()
        now = dt.datetime.now(dt.timezone.utc)
        q = f"year={now.year}&month={now.month}"
        base = f"{API}/users/{login}/settings/billing"
        try:
            try:
                items = http_json(f"{base}/usage/summary?{q}", h).get("usageItems") or []
            except HttpError as e:
                if e.status not in (404, 410, 422):
                    raise
                items = http_json(f"{base}/usage?{q}", h).get("usageItems") or []  # older, per-day report
        except HttpError as e:
            if e.status in (401, 403):
                hint = " (the GitHub CLI's token usually can't read billing)" if source == "gh" else ""
                raise ProviderError("GitHub needs a fine-grained token with Account → Plan: Read-only" + hint) from None
            if e.status == 404:
                raise ProviderError("This account isn't on GitHub's enhanced billing platform, so its usage "
                                    "isn't available from the API") from None
            raise
        try:
            premium = http_json(f"{base}/premium_request/usage?{q}", h).get("usageItems") or []
        except (HttpError, ProviderError):
            premium = []
        return self.parse(items, premium, plan, now)

    @staticmethod
    def parse(items: list[dict[str, Any]], premium: list[dict[str, Any]], plan: str,
              now: Optional[dt.datetime] = None) -> Snapshot:
        now = now or dt.datetime.now(dt.timezone.utc)
        start, end = month_bounds(now)
        hours_in_month = (end - start) / 3600
        inc = INCLUDED.get(plan, INCLUDED["free"])
        minutes = core_hours = gb_months = billed = 0.0
        saw_codespaces = False
        for it in items:
            product = str(it.get("product") or "").lower()
            sku = str(it.get("sku") or "").lower().replace("_", " ")
            unit = str(it.get("unitType") or "").lower()
            q = _qty(it)
            billed += fnum(it.get("netAmount")) or 0.0
            if product == "actions" and "minute" in unit:
                if _CORES.search(sku):
                    continue  # larger runners never use included minutes
                mult = next((m for key, m in MINUTE_MULTIPLIER if key in sku), 1)
                minutes += q * mult
            elif product == "codespaces":
                saw_codespaces = True
                if "storage" in sku or "gigabyte" in unit or "gb" in unit:
                    gb_months += q if "month" in unit else q / hours_in_month
                elif "hour" in unit:
                    m = _CORES.search(sku)
                    core_hours += q * (1 if "core" in unit else int(m.group(1)) if m else 2)
        snap = Snapshot(plan={"free": "Free", "pro": "Pro"}.get(plan, plan.title()))
        length = end - start
        snap.windows.append(UsageWindow("Actions minutes", min(100.0, minutes / inc["actions_minutes"] * 100), end,
                                        length, f"{minutes:,.0f} of {inc['actions_minutes']:,} min"))
        if saw_codespaces:
            snap.windows.append(UsageWindow("Codespaces compute",
                                            min(100.0, core_hours / inc["codespaces_core_hours"] * 100), end, length,
                                            f"{core_hours:,.1f} of {inc['codespaces_core_hours']} core-h"))
            snap.windows.append(UsageWindow("Codespaces storage",
                                            min(100.0, gb_months / inc["codespaces_gb_months"] * 100), end, length,
                                            f"{gb_months:,.1f} of {inc['codespaces_gb_months']} GB-mo"))
        over_q = sum(fnum(p.get("netQuantity")) or 0.0 for p in premium)
        over_amt = sum(fnum(p.get("netAmount")) or 0.0 for p in premium)
        if over_q > 0:
            snap.notes.append(f"Copilot requests beyond plan: {over_q:,.0f} (${over_amt:,.2f})")
        snap.notes.append(f"Billed beyond plan this month: ${billed:,.2f}" if billed >= 0.005
                          else "Nothing billed beyond your plan this month")
        return snap
