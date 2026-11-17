"""Nous Portal plans, via the account endpoint Hermes Agent's /usage command uses.

Works with the Hermes CLI and Hermes Desktop when Nous Portal is the model provider (both keep the
login in ~/.hermes/auth.json). Hermes Desktop's "Hermes Cloud" sign-in is stored encrypted and can't be read.
"""
from __future__ import annotations

import json
import time

from ..core import (Field, HttpError, Provider, ProviderError, Snapshot, UsageWindow, fnum, http_json,
                    jwt_claims, parse_time, read_json_file, title_case)


class NousProvider(Provider):
    type = "nous"
    default_name = "Nous"
    vendor = "Nous Research"
    plans = "Plus, Super, Ultra"
    setup = "Sign in to Nous Portal from Hermes (`hermes portal`, or pick Nous Portal as the provider in Hermes Desktop)."
    usage_url = "https://portal.nousresearch.com/billing"
    accent = "#8b7cf6"

    login_dir, login_file, env_var, login_what = ".hermes", "auth.json", "HERMES_HOME", "Hermes login"
    fields = (Field("auth_path", "Login file", "path", "Blank = default (~/.hermes/auth.json)."),)

    LABELS = {"day": ("Today", 86400), "week": ("This week", 7 * 86400),
              "month": ("This month", None), "top_tier": ("Large models (mo)", None)}

    def fetch(self) -> Snapshot:
        data = read_json_file(self.login_path(), self.login_what)
        state = (data.get("providers") or {}).get("nous") or {}
        token = state.get("access_token")
        if not token:
            raise ProviderError("No Nous Portal login in Hermes; run `hermes portal` or choose Nous Portal in Hermes")
        base = (state.get("portal_base_url") or "https://portal.nousresearch.com").rstrip("/")
        self.usage_url = self.cfg.get("usage_url") or f"{base}/billing"
        try:
            j = http_json(f"{base}/api/oauth/account", {"Authorization": f"Bearer {token}"})
        except HttpError as e:
            exp = fnum(jwt_claims(token).get("exp"))
            if e.status == 401 and exp and exp < time.time():
                raise ProviderError("Token expired; run any `hermes` command (or open Hermes) to refresh it") from None
            raise
        if isinstance(j.get("error"), str):
            raise ProviderError(f"Portal: {j['error']}")
        sub = j.get("subscription") or {}
        access = j.get("paid_service_access") or {}
        snap = Snapshot(plan=title_case(sub.get("plan") or j.get("account_tier") or ""))

        # Usage-window plans: daily / weekly / monthly gauges in basis points.
        uw = j.get("usage_window") or {}
        gauges = uw.get("gauges") if isinstance(uw, dict) else None
        if isinstance(gauges, str):
            try:
                gauges = json.loads(gauges)
            except ValueError:
                gauges = None
        for g in gauges if isinstance(gauges, list) else []:
            if not isinstance(g, dict):
                continue
            bp = g.get("used_bp")
            if isinstance(bp, bool) or not isinstance(bp, int):
                continue
            gid = str(g.get("id") or "")
            label, length = self.LABELS.get(gid, (str(g.get("label") or gid or "Allowance"), None))
            reset = parse_time(g.get("resets_at_ms"))
            if length is None and gid in ("month", "top_tier") and reset:
                length = 30 * 86400
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, bp / 100.0)), reset, length))
        if isinstance(uw, dict) and uw.get("paused_reason"):
            snap.notes.append("Included usage paused (see Billing)")

        # Older credit-based plans: monthly credits as a gauge.
        if not snap.windows:
            cap, left = fnum(sub.get("monthly_credits")), fnum(sub.get("credits_remaining"))
            if cap and cap > 0 and left is not None and left <= cap:
                end = parse_time(sub.get("current_period_end"))
                snap.windows.append(UsageWindow("Monthly credits", max(0.0, min(100.0, (cap - left) / cap * 100)),
                                                end, 30 * 86400 if end else None, f"${left:,.2f} of ${cap:,.2f} left"))
        topup = fnum(access.get("purchased_credits_remaining"))
        if topup:
            snap.notes.append(f"Top-up credits: ${topup:,.2f}")
        if not snap.windows and not snap.notes:
            raise ProviderError("Portal returned no usage data for this account")
        return snap
