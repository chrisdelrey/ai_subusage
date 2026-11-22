"""More subscriptions that authenticate with an API key or an existing CLI login.

Keys may be written as env:VAR_NAME. Where a vendor's CLI keeps its login in a file, that file is read
(never written), so pasting a key is optional.
"""
from __future__ import annotations

import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..core import (IS_MAC, IS_WIN, AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot,
                    UsageWindow, deep_find, deep_get, fnum, http_json, normalize_quota, parse_time, pct_of, run_cmd,
                    title_case, which)


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def read_json(p: Optional[Path]) -> Optional[dict[str, Any]]:
    try:
        if p and p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        pass
    return None


class KeyProvider(Provider):
    auth = "key"
    parks_on_auth = False

    def auth_error(self, e: HttpError) -> Exception:
        if e.status in (401, 403):
            return ProviderError(f"{self.default_name} rejected the API key; check it in Settings")
        return e


class KeyOrLoginProvider(KeyProvider):
    """A pasted key wins; otherwise read the vendor CLI's login file (and park it if it's expired)."""

    auth = "auto"

    @property
    def parks_on_auth(self) -> bool:  # type: ignore[override]
        return not self.setting("api_key")

    def file_token(self) -> Optional[str]:  # pragma: no cover - overridden
        return None

    def token(self) -> str:
        key = self.setting("api_key")
        if key:
            return key
        t = self.file_token()
        if not t:
            raise NotConfigured(f"No {self.default_name} login; {self.setup[:1].lower()}{self.setup[1:]}")
        return t

    def auth_error(self, e: HttpError) -> Exception:
        if e.status in (401, 403):
            if self.setting("api_key"):
                return ProviderError(f"{self.default_name} rejected the API key; check it in Settings")
            return AuthExpired(f"{self.default_name} login expired; sign in again with its CLI")
        return e


# --------------------------------------------------------------------------------------------------
# Warp
# --------------------------------------------------------------------------------------------------

class WarpProvider(KeyProvider):
    type = "warp"
    default_name = "Warp"
    vendor = "Warp"
    plans = "Free, Build, Business (AI credits)"
    setup = "Create an API key in Warp (Settings → Platform → API keys) and paste it in Settings."
    usage_url = "https://app.warp.dev/settings/billing"
    accent = "#01a4ff"
    fields = (Field("api_key", "API key", "secret", "env:WARP_API_KEY works too."),)
    QUERY = """query GetRequestLimitInfo($requestContext: RequestContext!) {
  user(requestContext: $requestContext) { __typename ... on UserOutput { user {
    requestLimitInfo { isUnlimited nextRefreshTime requestLimit requestsUsedSinceLastRefresh }
    bonusGrants { requestCreditsGranted requestCreditsRemaining expiration }
    workspaces { bonusGrantsInfo { grants { requestCreditsGranted requestCreditsRemaining expiration } } }
  } } } }"""

    def fetch(self) -> Snapshot:
        key = self.require_key()
        os_name = "macOS" if IS_MAC else "Windows" if IS_WIN else "Linux"
        ver = platform.release() or "0"
        body = {"query": self.QUERY, "operationName": "GetRequestLimitInfo",
                "variables": {"requestContext": {"clientContext": {},
                                                 "osContext": {"category": os_name, "name": os_name, "version": ver}}}}
        try:
            j = http_json("https://app.warp.dev/graphql/v2?op=GetRequestLimitInfo",
                          {"Authorization": f"Bearer {key}", "x-warp-client-id": "warp-app",
                           "x-warp-os-category": os_name, "x-warp-os-name": os_name, "x-warp-os-version": ver},
                          json_body=body)
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        if j.get("errors"):
            msg = "; ".join(str(e.get("message")) for e in j["errors"] if isinstance(e, dict))[:160]
            raise ProviderError(f"Warp: {msg or 'GraphQL error'}")
        user = (((j.get("data") or {}).get("user") or {}).get("user") or {})
        info = user.get("requestLimitInfo")
        if not isinstance(info, dict):
            raise ProviderError("Warp returned no credit info")
        snap = Snapshot()
        reset = parse_time(info.get("nextRefreshTime"))
        if info.get("isUnlimited"):
            snap.notes.append("Unlimited AI credits")
        else:
            used, limit = fnum(info.get("requestsUsedSinceLastRefresh")), fnum(info.get("requestLimit"))
            if pct_of(used, limit) is not None:
                snap.windows.append(UsageWindow("Credits (mo)", pct_of(used, limit), reset,
                                                30 * 86400 if reset else None, f"{used:,.0f} of {limit:,.0f}"))
        grants = list(user.get("bonusGrants") or [])
        for ws in user.get("workspaces") or []:
            grants += (((ws or {}).get("bonusGrantsInfo") or {}).get("grants") or [])
        total = sum(fnum(g.get("requestCreditsGranted")) or 0 for g in grants if isinstance(g, dict))
        left = sum(fnum(g.get("requestCreditsRemaining")) or 0 for g in grants if isinstance(g, dict))
        if total > 0:
            snap.windows.append(UsageWindow("Bonus credits", pct_of(total - left, total), None, None,
                                            f"{left:,.0f} of {total:,.0f} left"))
        if not snap.windows and not snap.notes:
            raise ProviderError("Warp returned no credit info")
        return snap


# --------------------------------------------------------------------------------------------------
# Poe
# --------------------------------------------------------------------------------------------------

class PoeProvider(KeyProvider):
    type = "poe"
    default_name = "Poe"
    vendor = "Quora"
    plans = "Subscriber (compute points)"
    setup = "Create an API key at poe.com/api_key and paste it. Optionally enter your monthly point allowance."
    usage_url = "https://poe.com/settings"
    accent = "#5d5cde"
    fields = (
        Field("api_key", "API key", "secret", "From poe.com/api_key. env:POE_API_KEY works too."),
        Field("allowance", "Monthly points", "text", "Optional, e.g. 1000000. Turns the balance into a usage bar."),
    )

    def fetch(self) -> Snapshot:
        key = self.require_key()
        try:
            j = http_json("https://api.poe.com/usage/current_balance", {"Authorization": f"Bearer {key}"})
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j, fnum(self.setting("allowance").replace(",", "")))

    @staticmethod
    def parse(j: dict[str, Any], allowance: Optional[float] = None) -> Snapshot:
        bal = fnum(j.get("current_point_balance"))
        if bal is None:
            raise ProviderError("Poe returned no point balance")
        snap = Snapshot()
        if allowance and allowance > 0:
            snap.windows.append(UsageWindow("Points (mo)", pct_of(allowance - bal, allowance), None, None,
                                            f"{bal:,.0f} of {allowance:,.0f} left"))
        else:
            snap.notes.append(f"{bal:,.0f} points left")
        return snap


# --------------------------------------------------------------------------------------------------
# Chutes
# --------------------------------------------------------------------------------------------------

class ChutesProvider(KeyProvider):
    type = "chutes"
    default_name = "Chutes"
    vendor = "Chutes"
    plans = "Base, Plus, Pro"
    setup = "Create an API key at chutes.ai (Account → API keys) and paste it in Settings."
    usage_url = "https://chutes.ai/app/account"
    accent = "#14b8a6"
    fields = (Field("api_key", "API key", "secret", "env:CHUTES_API_KEY works too."),)

    def fetch(self) -> Snapshot:
        key = self.require_key()
        h = {"Authorization": f"Bearer {key}"}
        try:
            snap = self.parse(http_json("https://api.chutes.ai/users/me/subscription_usage", h))
        except HttpError as e:
            raise self.auth_error(e) from None
        except ProviderError:
            try:
                snap = self.parse(http_json("https://api.chutes.ai/users/me/quotas", h))
            except HttpError as e:
                raise self.auth_error(e) from None
        return snap

    @staticmethod
    def parse(j: Any) -> Snapshot:
        snap = Snapshot(plan=title_case(deep_get(j, "plan_name", "planName", "tier", "plan") or "")
                        if isinstance(deep_get(j, "plan_name", "planName", "tier", "plan"), str) else "")
        seen: set[str] = set()

        def walk(o: Any, key: str = "", depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(o, dict):
                q = normalize_quota(o)
                k = key.lower().replace("_", "")
                if q and k:
                    if any(t in k for t in ("roll", "4h", "four", "hour")):
                        label, length = "Rolling (4h)", 4 * 3600
                    elif any(t in k for t in ("month", "subscription", "billing")):
                        label, length = "Monthly", 30 * 86400
                    elif "day" in k or "daily" in k:
                        label, length = "Daily", 86400
                    else:
                        label, length = title_case(key), None
                    if label not in seen:
                        seen.add(label)
                        snap.windows.append(UsageWindow(label, q[0], q[1], length))
                    return
                for ck, cv in o.items():
                    walk(cv, str(ck), depth + 1)
            elif isinstance(o, list):
                for i, cv in enumerate(o):
                    name = (cv.get("name") or cv.get("label") or cv.get("period") or cv.get("window")) \
                        if isinstance(cv, dict) else None
                    walk(cv, str(name or key or f"quota {i + 1}"), depth + 1)

        walk(j, "")
        if not snap.windows:
            q = normalize_quota(j) if isinstance(j, dict) else None
            if q:
                snap.windows.append(UsageWindow("Quota", q[0], q[1]))
        if not snap.windows:
            raise ProviderError("Chutes returned no subscription usage")
        return snap


# --------------------------------------------------------------------------------------------------
# GitKraken AI
# --------------------------------------------------------------------------------------------------

class GitKrakenProvider(KeyProvider):
    type = "gitkraken"
    default_name = "GitKraken AI"
    vendor = "GitKraken"
    plans = "Pro, Advanced, Business (weekly AI credits)"
    setup = "Create a personal access token at gitkraken.dev (Settings → Personal access tokens) and paste it."
    usage_url = "https://gitkraken.dev/settings/ai"
    accent = "#179287"
    fields = (
        Field("api_key", "Access token", "secret", "env:GITKRAKEN_API_TOKEN works too."),
        Field("org_id", "Organization ID", "text", "Optional: show the organization's shared credit pool."),
    )

    def fetch(self) -> Snapshot:
        key = self.require_key(what="access token")
        h = {"Authorization": f"Bearer {key}", "Client-Name": "SubUsage", "Client-Version": "1"}
        if self.setting("org_id"):
            h["gk-org-id"] = self.setting("org_id")
        try:
            j = http_json("https://api.gitkraken.dev/v1/ai-tasks/usage", h)
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        d = j.get("data") if isinstance(j.get("data"), dict) else None
        if not d or fnum(d.get("used")) is None:
            raise ProviderError("GitKraken returned no AI usage")
        reset = parse_time(d.get("resetsOn"))
        snap = Snapshot()
        for label, q in (("Weekly", d), ("Org pool (wk)", d.get("organization"))):
            if not isinstance(q, dict):
                continue
            used, limit = fnum(q.get("used")), fnum(q.get("limit"))
            if limit == -1:
                snap.notes.append(f"{label}: unlimited")
            elif pct_of(used, limit) is not None:
                snap.windows.append(UsageWindow(label, pct_of(used, limit), reset, 7 * 86400,
                                                f"{used:,.0f} of {limit:,.0f} credits"))
        if not snap.windows and not snap.notes:
            snap.notes.append(f"{fnum(d.get('used')):,.0f} credits used this week")
        return snap


# --------------------------------------------------------------------------------------------------
# MiniMax Coding Plan
# --------------------------------------------------------------------------------------------------

class MiniMaxProvider(KeyProvider):
    type = "minimax"
    default_name = "MiniMax"
    vendor = "MiniMax"
    plans = "Coding Plan Starter, Plus, Max"
    setup = "Create a Coding Plan API key at platform.minimax.io and paste it in Settings."
    usage_url = "https://platform.minimax.io/user-center/payment/coding-plan"
    accent = "#f43f5e"
    fields = (
        Field("api_key", "API key", "secret", "Coding Plan key. env:MINIMAX_API_KEY works too."),
        Field("region", "Region", "choice", options=(("global", "Global (minimax.io)"),
                                                      ("china", "China (minimaxi.com)")), default="global"),
    )

    def fetch(self) -> Snapshot:
        key = self.require_key()
        host = "https://api.minimaxi.com" if self.setting("region") == "china" else "https://api.minimax.io"
        if self.setting("region") == "china":
            self.usage_url = self.cfg.get("usage_url") or "https://platform.minimaxi.com/user-center/payment/coding-plan"
        last: Optional[Exception] = None
        for path in ("/v1/token_plan/remains", "/v1/api/openplatform/coding_plan/remains"):
            try:
                return self.parse(http_json(host + path, {"Authorization": f"Bearer {key}"}))
            except HttpError as e:
                if e.status in (401, 403):
                    raise self.auth_error(e) from None
                last = e
            except ProviderError as e:
                last = e
        raise last or ProviderError("MiniMax returned no Coding Plan data")

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        base = j.get("base_resp") if isinstance(j.get("base_resp"), dict) else {}
        if base.get("status_code") not in (None, 0):
            raise ProviderError(f"MiniMax: {base.get('status_msg') or base.get('status_code')}")
        models = [m for m in (j.get("model_remains") or []) if isinstance(m, dict)]
        if not models:
            raise ProviderError("MiniMax returned no Coding Plan data for this key")
        m = models[0]
        snap = Snapshot(plan="Coding Plan")

        def lane(prefix: str, label: str, length: int, end_key: str, remains_key: str) -> None:
            total = fnum(m.get(f"{prefix}_total_count"))
            left = fnum(m.get(f"{prefix}_usage_count"))  # MiniMax names the *remaining* count "usage_count"
            left_pct = fnum(m.get(f"{prefix}_remaining_percent"))
            used_pct = fnum(m.get(f"{prefix}_used_percent"))
            if used_pct is None and left_pct is not None:
                used_pct = 100 - left_pct
            if used_pct is None and total:
                used_pct = pct_of(total - (left or 0), total)
            if used_pct is None:
                return
            reset = parse_time(m.get(end_key))
            if not reset and fnum(m.get(remains_key)):
                r = float(m[remains_key])
                reset = time.time() + (r / 1000 if r > 1e6 else r)
            detail = f"{left:,.0f} of {total:,.0f} left" if total and left is not None else ""
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, used_pct)), reset, length, detail))

        lane("current_interval", "Session (5h)", 5 * 3600, "end_time", "remains_time")
        lane("current_weekly", "Weekly", 7 * 86400, "weekly_end_time", "weekly_remains_time")
        if not snap.windows:
            raise ProviderError("MiniMax returned no Coding Plan limits")
        return snap


# --------------------------------------------------------------------------------------------------
# Alibaba Cloud Model Studio Coding Plan (Qwen Code)
# --------------------------------------------------------------------------------------------------

class AlibabaProvider(KeyProvider):
    type = "alibaba"
    default_name = "Alibaba Coding Plan"
    vendor = "Alibaba Cloud"
    plans = "Qwen Coding Plan Lite, Pro"
    setup = "Paste your Coding Plan API key (sk-sp-…) from Model Studio (Bailian)."
    usage_url = "https://modelstudio.console.alibabacloud.com/ap-southeast-1/?tab=coding-plan#/efm/coding_plan"
    accent = "#ff6a00"
    fields = (
        Field("api_key", "API key", "secret", "Coding Plan key. env:ALIBABA_CODING_PLAN_API_KEY works too."),
        Field("region", "Region", "choice", options=(("intl", "International (Singapore)"),
                                                      ("cn", "China mainland (Beijing)")), default="intl"),
    )
    REGIONS = {
        "intl": ("https://modelstudio.console.alibabacloud.com", "ap-southeast-1", "sfm_codingplan_public_intl"),
        "cn": ("https://bailian.console.aliyun.com", "cn-beijing", "sfm_codingplan_public_cn"),
    }

    def fetch(self) -> Snapshot:
        key = self.require_key()
        base, region, commodity = self.REGIONS.get(self.setting("region"), self.REGIONS["intl"])
        url = (f"{base}/data/api.json?action=zeldaEasy.broadscope-bailian.codingPlan.queryCodingPlanInstanceInfoV2"
               f"&product=broadscope-bailian&api=queryCodingPlanInstanceInfoV2&currentRegionId={region}")
        try:
            j = http_json(url, {"Authorization": f"Bearer {key}", "x-api-key": key, "X-DashScope-API-Key": key,
                                "Origin": base, "Referer": base + "/"},
                          json_body={"queryCodingPlanInstanceInfoRequest": {"commodityCode": commodity}})
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: Any) -> Snapshot:
        q = deep_find(j, lambda d: any(k in d for k in ("per5HourTotalQuota", "perWeekTotalQuota",
                                                         "perBillMonthTotalQuota", "perFiveHourTotalQuota")))
        if not q:
            msg = deep_get(j, "message", "Message", "errorMsg")
            raise ProviderError(f"Alibaba: {msg}" if msg else "Alibaba returned no Coding Plan quota for this key")
        plan = deep_get(j, "planName", "plan_name", "packageName")
        snap = Snapshot(plan=str(plan) if isinstance(plan, str) else "")
        for keys, label, length in (
                (("per5HourUsedQuota", "per5HourTotalQuota", "per5HourQuotaNextRefreshTime"), "Session (5h)", 5 * 3600),
                (("perFiveHourUsedQuota", "perFiveHourTotalQuota", "perFiveHourQuotaNextRefreshTime"), "Session (5h)",
                 5 * 3600),
                (("perWeekUsedQuota", "perWeekTotalQuota", "perWeekQuotaNextRefreshTime"), "Weekly", 7 * 86400),
                (("perBillMonthUsedQuota", "perBillMonthTotalQuota", "perBillMonthQuotaNextRefreshTime"), "Monthly",
                 30 * 86400)):
            used, total = fnum(q.get(keys[0])), fnum(q.get(keys[1]))
            if pct_of(used, total) is None or label in [w.label for w in snap.windows]:
                continue
            snap.windows.append(UsageWindow(label, pct_of(used, total), parse_time(q.get(keys[2])), length,
                                            f"{used:,.0f} of {total:,.0f}"))
        if not snap.windows:
            raise ProviderError("Alibaba returned no Coding Plan limits")
        return snap


# --------------------------------------------------------------------------------------------------
# Factory (Droid)
# --------------------------------------------------------------------------------------------------

class FactoryProvider(KeyOrLoginProvider):
    type = "factory"
    default_name = "Factory Droid"
    vendor = "Factory"
    plans = "Pro, Max, Teams"
    setup = "Paste a Factory API key (app.factory.ai → Settings → API keys), or put FACTORY_API_KEY in ~/.factory/.env."
    usage_url = "https://app.factory.ai/settings/billing"
    accent = "#f59e0b"
    login_dir, login_file, login_what = ".factory", ".env", "Factory .env"
    fields = (Field("api_key", "API key", "secret", "Blank = FACTORY_API_KEY from ~/.factory/.env. env:VAR works."),)

    def file_token(self) -> Optional[str]:
        if os.environ.get("FACTORY_API_KEY"):
            return os.environ["FACTORY_API_KEY"].strip()
        p = self.default_path()
        try:
            for line in p.read_text(encoding="utf-8").splitlines() if p and p.exists() else []:
                m = re.match(r"\s*(?:export\s+)?FACTORY_API_KEY\s*=\s*['\"]?([^'\"\s#]+)", line)
                if m:
                    return m.group(1)
        except OSError:
            pass
        return None

    def fetch(self) -> Snapshot:
        h = {"Authorization": f"Bearer {self.token()}", "x-factory-client": "web-app",
             "Origin": "https://app.factory.ai", "Referer": "https://app.factory.ai/"}
        try:
            limits = http_json("https://api.factory.ai/api/billing/limits", h)
        except HttpError as e:
            if e.status in (401, 403):
                raise self.auth_error(e) from None
            limits = {}
        snap = self.parse_limits(limits)
        if snap.windows:
            return snap
        try:
            return self.parse_usage(http_json("https://api.factory.ai/api/organization/subscription/usage", h))
        except HttpError as e:
            raise self.auth_error(e) from None

    @staticmethod
    def parse_limits(j: dict[str, Any]) -> Snapshot:
        snap = Snapshot()
        std = ((j.get("limits") or {}).get("standard") if isinstance(j.get("limits"), dict) else None) or {}
        for key, label, length in (("fiveHour", "Session (5h)", 5 * 3600), ("weekly", "Weekly", 7 * 86400),
                                   ("monthly", "Monthly", 30 * 86400)):
            w = std.get(key)
            if not isinstance(w, dict) or fnum(w.get("usedPercent")) is None:
                continue
            secs = fnum(w.get("secondsRemaining"))
            reset = time.time() + secs if secs and secs > 0 else parse_time(w.get("windowEnd"))
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, float(w["usedPercent"]))), reset, length))
        if snap.windows and not any(w.used_pct or w.resets_at for w in snap.windows):
            snap.windows = []  # all-zero placeholder pools: fall back to token allowances
        bal = fnum(j.get("extraUsageBalanceCents"))
        if bal:
            snap.notes.append(f"Extra usage balance: ${bal / 100:,.2f}")
        return snap

    @staticmethod
    def parse_usage(j: dict[str, Any]) -> Snapshot:
        u = j.get("usage") if isinstance(j.get("usage"), dict) else None
        if not u:
            raise ProviderError("Factory returned no usage")
        end = parse_time(u.get("endDate"))
        start = parse_time(u.get("startDate"))
        snap = Snapshot()
        for key, label in (("standard", "Standard tokens"), ("premium", "Premium tokens")):
            t = u.get(key)
            if not isinstance(t, dict):
                continue
            pct = fnum(t.get("usedRatio"))
            pct = pct * 100 if pct is not None else pct_of(fnum(t.get("orgTotalTokensUsed") or t.get("userTokens")),
                                                            fnum(t.get("totalAllowance")))
            if pct is None or not fnum(t.get("totalAllowance")):
                continue
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, pct)), end,
                                            (end - start) if start and end else None))
        if not snap.windows:
            raise ProviderError("Factory returned no usage")
        return snap


# --------------------------------------------------------------------------------------------------
# OpenCode Go
# --------------------------------------------------------------------------------------------------

def _meter(d: Any) -> Optional[tuple[float, Optional[float]]]:
    if not isinstance(d, dict):
        return None
    pct = next((fnum(d.get(k)) for k in ("usagePercent", "usedPercent", "percentUsed", "percent", "usage_percent",
                                          "used_percent", "utilization") if fnum(d.get(k)) is not None), None)
    if pct is None:
        pct = pct_of(next((fnum(d.get(k)) for k in ("used", "usage", "consumed") if fnum(d.get(k)) is not None), None),
                     next((fnum(d.get(k)) for k in ("limit", "total", "quota", "max") if fnum(d.get(k))), None))
    if pct is None:
        return None
    secs = next((fnum(d.get(k)) for k in ("resetInSec", "resetInSeconds", "resetsInSec", "reset_in_sec")
                 if fnum(d.get(k)) is not None), None)
    reset = time.time() + secs if secs and secs > 0 else next(
        (parse_time(d.get(k)) for k in ("resetAt", "resetsAt", "reset_at", "resets_at") if parse_time(d.get(k))), None)
    return max(0.0, min(100.0, pct)), reset


class OpenCodeGoProvider(KeyOrLoginProvider):
    type = "opencodego"
    default_name = "OpenCode Go"
    vendor = "SST"
    plans = "OpenCode Go"
    setup = "Run `opencode auth login` and pick OpenCode Go, or paste your OpenCode API key."
    usage_url = "https://opencode.ai/auth"
    accent = "#94a3b8"
    login_what = "OpenCode login"
    fields = (Field("api_key", "API key", "secret", "Blank = the `opencode auth login` key. env:OPENCODE_API_KEY works."),
              Field("auth_path", "OpenCode auth file", "path", "Blank = ~/.local/share/opencode/auth.json"))

    @classmethod
    def default_path(cls) -> Optional[Path]:
        return xdg_data_home() / "opencode" / "auth.json"

    def file_token(self) -> Optional[str]:
        if os.environ.get("OPENCODE_API_KEY"):
            return os.environ["OPENCODE_API_KEY"].strip()
        d = read_json(self.login_path()) or {}
        for name in ("opencode-go", "opencode"):
            e = d.get(name)
            if isinstance(e, dict) and e.get("type") == "api" and e.get("key"):
                return str(e["key"])
        return None

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://opencode.ai/zen/go/v1/usage", {"Authorization": f"Bearer {self.token()}"})
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        u = j.get("usage") if isinstance(j.get("usage"), dict) else None
        if not u:
            raise ProviderError("OpenCode Go returned no usage (is this key on a Go subscription?)")
        snap = Snapshot(plan="Go")
        for key, label, length in (("rolling", "Session (5h)", 5 * 3600), ("weekly", "Weekly", 7 * 86400),
                                   ("monthly", "Monthly", 30 * 86400)):
            m = _meter(u.get(key))
            if m:
                snap.windows.append(UsageWindow(label, m[0], m[1], length))
        if not snap.windows:
            raise ProviderError("OpenCode Go returned no usage")
        return snap


# --------------------------------------------------------------------------------------------------
# Kilo Pass
# --------------------------------------------------------------------------------------------------

class KiloProvider(KeyOrLoginProvider):
    type = "kilo"
    default_name = "Kilo Pass"
    vendor = "Kilo Code"
    plans = "Kilo Pass Starter, Pro, Expert"
    setup = "Run `kilo auth login`, or paste a Kilo API key from app.kilo.ai."
    usage_url = "https://app.kilo.ai/profile"
    accent = "#facc15"
    login_what = "Kilo login"
    fields = (Field("api_key", "API key", "secret", "Blank = the `kilo auth login` token. env:KILO_API_KEY works."),
              Field("auth_path", "Kilo auth file", "path", "Blank = ~/.local/share/kilo/auth.json"))

    @classmethod
    def default_path(cls) -> Optional[Path]:
        return xdg_data_home() / "kilo" / "auth.json"

    def file_token(self) -> Optional[str]:
        if os.environ.get("KILO_API_KEY"):
            return os.environ["KILO_API_KEY"].strip()
        d = read_json(self.login_path()) or {}
        k = d.get("kilo")
        return str(k.get("access")) if isinstance(k, dict) and k.get("access") else None

    def fetch(self) -> Snapshot:
        import urllib.parse
        inp = urllib.parse.quote(json.dumps({"0": {"json": None}, "1": {"json": None}}), safe="")
        try:
            j = http_json(f"https://app.kilo.ai/api/trpc/kiloPass.getState,user.getCreditBlocks?batch=1&input={inp}",
                          {"Authorization": f"Bearer {self.token()}"})
        except HttpError as e:
            raise self.auth_error(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: Any) -> Snapshot:
        entries = j if isinstance(j, list) else [j]
        payloads = [deep_get(e, "json") for e in entries]
        sub = deep_find(payloads, lambda d: "currentPeriodUsageUsd" in d or "currentPeriodBaseCreditsUsd" in d)
        snap = Snapshot()
        if sub:
            snap.plan = title_case(sub.get("tier") or "Kilo Pass")
            used = fnum(sub.get("currentPeriodUsageUsd"))
            cap = (fnum(sub.get("currentPeriodBaseCreditsUsd")) or 0) + (fnum(sub.get("currentPeriodBonusCreditsUsd")) or 0)
            reset = next((parse_time(sub.get(k)) for k in ("nextBillingAt", "nextRenewalAt", "renewsAt", "renewAt")
                          if parse_time(sub.get(k))), None)
            if pct_of(used, cap) is not None:
                snap.windows.append(UsageWindow("Pass credits (mo)", pct_of(used, cap), reset,
                                                30 * 86400 if reset else None, f"${used:,.2f} of ${cap:,.2f}"))
        bal = deep_get(payloads, "totalBalance_mUsd")
        if fnum(bal) is not None:
            snap.notes.append(f"Credit balance: ${fnum(bal) / 1000:,.2f}")
        if not snap.windows and not snap.notes:
            raise ProviderError("Kilo returned no Kilo Pass usage")
        return snap


# --------------------------------------------------------------------------------------------------
# Codebuff
# --------------------------------------------------------------------------------------------------

class CodebuffProvider(KeyOrLoginProvider):
    type = "codebuff"
    default_name = "Codebuff"
    vendor = "Codebuff"
    plans = "Pro, Max (credits + weekly limit)"
    setup = "Run `codebuff login`, or paste a Codebuff API key."
    usage_url = "https://www.codebuff.com/usage"
    accent = "#22d3ee"
    login_dir, login_file, login_what = ".config/manicode", "credentials.json", "Codebuff login"
    fields = (Field("api_key", "API key", "secret", "Blank = the `codebuff login` token. env:CODEBUFF_API_KEY works."),
              Field("auth_path", "Codebuff credentials", "path", "Blank = ~/.config/manicode/credentials.json"))

    def file_token(self) -> Optional[str]:
        if os.environ.get("CODEBUFF_API_KEY"):
            return os.environ["CODEBUFF_API_KEY"].strip()
        d = read_json(self.login_path()) or {}
        t = ((d.get("default") or {}) if isinstance(d.get("default"), dict) else {}).get("authToken") or d.get("authToken")
        return str(t) if t else None

    def fetch(self) -> Snapshot:
        h = {"Authorization": f"Bearer {self.token()}"}
        try:
            usage = http_json("https://www.codebuff.com/api/v1/usage", h, json_body={"fingerprintId": "subusage-usage"})
        except HttpError as e:
            raise self.auth_error(e) from None
        try:
            sub = http_json("https://www.codebuff.com/api/user/subscription", h)
        except (HttpError, ProviderError):
            sub = {}
        return self.parse(usage, sub)

    @staticmethod
    def parse(u: dict[str, Any], sub: Optional[dict[str, Any]] = None) -> Snapshot:
        sub = sub or {}
        s = sub.get("subscription") if isinstance(sub.get("subscription"), dict) else {}
        snap = Snapshot(plan=str(s.get("displayName") or sub.get("displayName") or title_case(s.get("tier") or "")))
        rl = sub.get("rateLimit") if isinstance(sub.get("rateLimit"), dict) else {}
        wu, wl = fnum(rl.get("weeklyUsed", rl.get("used"))), fnum(rl.get("weeklyLimit", rl.get("limit")))
        if pct_of(wu, wl) is not None:
            snap.windows.append(UsageWindow("Weekly", pct_of(wu, wl), parse_time(rl.get("weeklyResetsAt")), 7 * 86400))
        used, quota = fnum(u.get("usage", u.get("used"))), fnum(u.get("quota", u.get("limit")))
        left = fnum(u.get("remainingBalance", u.get("remaining")))
        if pct_of(used, quota) is not None:
            reset = parse_time(u.get("next_quota_reset"))
            snap.windows.append(UsageWindow("Credits", pct_of(used, quota), reset, 30 * 86400 if reset else None,
                                            f"{(quota or 0) - (used or 0):,.0f} of {quota:,.0f} left"))
        elif left is not None:
            snap.notes.append(f"{left:,.0f} credits left")
        if not snap.windows and not snap.notes:
            raise ProviderError("Codebuff returned no usage")
        return snap


# --------------------------------------------------------------------------------------------------
# Amp
# --------------------------------------------------------------------------------------------------

class AmpProvider(KeyOrLoginProvider):
    type = "amp"
    default_name = "Amp"
    vendor = "Amp (Sourcegraph)"
    plans = "Free, paid tiers, workspace credits"
    setup = "Install the Amp CLI and sign in (`amp login`), or paste an Amp access token."
    usage_url = "https://ampcode.com/settings"
    accent = "#f34e3f"
    fields = (Field("api_key", "Access token", "secret", "Blank = run `amp usage`. env:AMP_API_KEY works."),)

    def watch_paths(self) -> list[Path]:
        return [xdg_data_home() / "amp" / "secrets.json"]

    def fetch(self) -> Snapshot:
        key = self.setting("api_key") or os.environ.get("AMP_API_KEY", "").strip()
        if key:
            try:
                j = http_json("https://ampcode.com/api/internal?userDisplayBalanceInfo",
                              {"Authorization": f"Bearer {key}"},
                              json_body={"method": "userDisplayBalanceInfo", "params": {}})
            except HttpError as e:
                raise self.auth_error(e) from None
            if not j.get("ok"):
                err = j.get("error") or {}
                if err.get("code") == "auth-required":
                    raise ProviderError("Amp rejected the access token; check it in Settings")
                raise ProviderError(f"Amp: {err.get('message') or 'error'}")
            return self.parse(str((j.get("result") or {}).get("displayText") or ""))
        exe = which(["amp", "amp.cmd"] if IS_WIN else ["amp"])
        if not exe:
            raise NotConfigured("Amp CLI not found; install it or paste an access token")
        out = run_cmd([exe, "usage"], timeout=30)
        if out is None:
            raise AuthExpired("`amp usage` failed; run `amp login`")
        return self.parse(out)

    @staticmethod
    def parse(text: str) -> Snapshot:
        t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("**", "")
        num = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
        f = lambda s: float(s.replace(",", ""))  # noqa: E731
        snap = Snapshot()
        m = re.search(rf"(?im)^\s*Amp Free:\s*\$?{num}\s*/\s*\$?{num}\s+remaining", t)
        if m:
            left, cap = f(m.group(1)), f(m.group(2))
            snap.windows.append(UsageWindow("Amp Free", pct_of(cap - left, cap), None, None,
                                            f"${left:,.2f} of ${cap:,.2f} left"))
        m = re.search(rf"(?im)^\s*Amp Free:\s*{num}\s*%\s+remaining", t)
        if m and not snap.windows:
            snap.windows.append(UsageWindow("Amp Free (day)", 100 - f(m.group(1)), None, 86400))
        renew = lambda n, unit: time.time() + int(n.replace(",", "")) * (30 if unit.startswith("month") else 1) * 86400  # noqa: E731
        m = re.search(rf"(?im)^\s*(?:Subscription\s+(.+?)|Amp\s+(.+?)\s+Subscription):\s*{num}\s*%\s+other\s+usage\s+and\s+"
                      rf"{num}\s*%\s+orb\s+usage\s+remaining\s*-\s*resets\s+upon\s+renewal\s+in\s+([0-9,]+)\s+(days?|months?)",
                      t)
        if m:
            snap.plan = (m.group(1) or m.group(2) or "").strip()
            reset = renew(m.group(5), m.group(6))
            snap.windows.append(UsageWindow("Usage", 100 - f(m.group(3)), reset, None))
            snap.windows.append(UsageWindow("Orb usage", 100 - f(m.group(4)), reset, None))
        m = re.search(rf"(?im)^\s*Amp\s+([^\r\n]+?)\s+Tier:\s*agent\s+usage\s+\${num}\s+of\s+\${num}\s+remaining"
                      rf"[^\r\n]*?resets\s+upon\s+renewal\s+in\s+([0-9,]+)\s+(days?|months?)", t)
        if m:
            snap.plan = snap.plan or m.group(1).strip()
            left, cap = f(m.group(2)), f(m.group(3))
            snap.windows.append(UsageWindow("Agent usage", pct_of(cap - left, cap), renew(m.group(4), m.group(5)), None,
                                            f"${left:,.2f} of ${cap:,.2f} left"))
        m = re.search(rf"(?im)^\s*Individual credits:\s*\$?{num}\s+remaining", t)
        if m:
            snap.notes.append(f"Credits: ${f(m.group(1)):,.2f}")
        for m in re.finditer(rf"(?im)^\s*Workspace\s+(.+?):\s*\$?{num}\s+remaining", t):
            snap.notes.append(f"{m.group(1).strip()}: ${f(m.group(2)):,.2f}")
        if not snap.windows and not snap.notes:
            if re.search(r"not (?:logged|signed) in|amp login", t, re.I):
                raise AuthExpired("Amp isn't signed in; run `amp login`")
            raise ProviderError("Couldn't read Amp usage")
        return snap
