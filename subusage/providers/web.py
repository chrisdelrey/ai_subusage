"""Subscriptions whose usage is only shown on the vendor's website, read with your browser session.

You paste the session once in Settings: either the Cookie header, or the whole "Copy as cURL" of a
request from the browser's Network tab (DevTools → Network → right-click a request → Copy → Copy as cURL).
Each provider keeps only the cookies its vendor needs and sends them only to that vendor.
These are the same internal endpoints the vendors' own web apps call, so they can change without notice.
"""
from __future__ import annotations

import html as htmllib
import re
import time
import urllib.parse
from typing import Any, Optional

from ..core import (BROWSER_UA, IS_MAC, AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot,
                    UsageWindow, deep_find, fnum, http_json, http_text, parse_cookie_input, parse_time, pct_of,
                    pick_cookies, run_cmd, title_case)

COOKIE_HELP = ("Paste the Cookie header, or the whole 'Copy as cURL' of a request from the site "
               "(DevTools → Network). env:VAR works too.")


def cookie_field(site: str, extra: str = "") -> Field:
    return Field("cookie", "Browser session", "secret", f"From {site}. {extra}{COOKIE_HELP}".strip())


class CookieProvider(Provider):
    """Base for website-session providers. Subclasses set cookie_names/cookie_prefixes/bare_cookie."""

    auth = "cookie"
    parks_on_auth = False          # an expired cookie should stay visible with a clear message
    cookie_names: tuple[str, ...] = ()
    cookie_prefixes: tuple[str, ...] = ()
    bare_cookie = ""               # cookie name to use when the user pastes just a value
    site = ""
    fields = ()

    def session(self, required: bool = True) -> tuple[str, dict[str, str]]:
        raw = self.setting("cookie")
        header, extra = parse_cookie_input(raw)
        cookie = pick_cookies(header, self.cookie_names, self.cookie_prefixes, self.bare_cookie)
        if not cookie and required:
            if raw:
                want = " / ".join((self.cookie_prefixes and [self.cookie_prefixes[0] + "…"] or [])
                                  + list(self.cookie_names[:2]))
                raise NotConfigured(f"That session doesn't include the cookie {self.default_name} needs ({want})")
            raise NotConfigured(f"No {self.default_name} session; paste one from {self.site} in Settings")
        return cookie, extra

    def browser_headers(self, origin: str, referer: str = "") -> dict[str, str]:
        return {"User-Agent": BROWSER_UA, "Origin": origin, "Referer": referer or origin + "/",
                "Accept-Language": "en-US,en;q=0.9"}

    def http_error_message(self, e: HttpError) -> str:
        if e.status in (401, 403) or 300 <= e.status < 400:
            return f"{self.default_name} session expired; paste a fresh one from {self.site} in Settings"
        return super().http_error_message(e)

    def expired(self, e: HttpError) -> Exception:
        """401/403 or a redirect to a login page = the session expired; anything else keeps its HTTP status
        (so 429 still backs off)."""
        if e.status in (401, 403) or 300 <= e.status < 400:
            return AuthExpired(self.http_error_message(e))
        return e


# --------------------------------------------------------------------------------------------------
# Notion AI
# --------------------------------------------------------------------------------------------------

class NotionProvider(CookieProvider):
    type = "notion"
    default_name = "Notion AI"
    vendor = "Notion"
    plans = "Business, Enterprise (AI credits)"
    setup = ("Paste your notion.so / app.notion.com session (the token_v2 cookie). Notion only meters AI "
             "credits on Business and Enterprise workspaces.")
    usage_url = "https://www.notion.so/settings/notion-ai"
    accent = "#e5e7eb"
    site = "app.notion.com"
    cookie_names = ("token_v2", "notion_user_id", "notion_users", "device_id")
    bare_cookie = "token_v2"
    min_poll = 300
    default_poll = 600
    fields = (
        cookie_field("app.notion.com (or notion.so)", "Needs the token_v2 cookie. "),
        Field("workspace_id", "Workspace ID", "text", "Optional. Blank = your first Business/Enterprise workspace."),
    )
    HOSTS = ("app.notion.com", "www.notion.so")

    def _post(self, host: str, endpoint: str, body: dict[str, Any], cookie: str, extra: dict[str, str]) -> Any:
        h = {**self.browser_headers(f"https://{host}"), "Cookie": cookie, **extra}
        return http_json(f"https://{host}/api/v3/{endpoint}", h, json_body=body)

    def fetch(self) -> Snapshot:
        cookie, extra = self.session()
        last: Optional[Exception] = None
        for host in self.HOSTS:
            try:
                spaces = self._post(host, "getSpaces", {}, cookie, extra)
            except HttpError as e:
                if e.status in (401, 403):
                    raise self.expired(e) from None
                last = e
                continue
            except ProviderError as e:
                last = e
                continue
            ws = self.pick_workspace(spaces, self.setting("workspace_id"))
            if not ws:
                raise ProviderError("No Notion workspace found for this account")
            try:
                usage = self._post(host, "getCreditRateLimitStatus", {"spaceId": ws["id"]}, cookie, extra)
            except HttpError as e:
                raise self.expired(e) from None
            return self.parse(usage, ws)
        if isinstance(last, HttpError):
            raise self.expired(last)
        raise last or ProviderError("Couldn't reach Notion")

    @staticmethod
    def _unwrap(v: Any) -> Optional[dict[str, Any]]:
        for _ in range(3):
            if isinstance(v, dict) and isinstance(v.get("value"), dict):
                v = v["value"]
        return v if isinstance(v, dict) else None

    @classmethod
    def pick_workspace(cls, spaces: Any, preferred: str = "") -> Optional[dict[str, Any]]:
        if not isinstance(spaces, dict):
            return None
        norm = lambda s: str(s or "").replace("-", "").strip().lower()  # noqa: E731
        found: list[dict[str, Any]] = []
        for _uid, container in spaces.items():
            records = (container or {}).get("space") if isinstance(container, dict) else None
            for key, rec in sorted((records or {}).items()):
                r = cls._unwrap(rec)
                if r:
                    found.append({**r, "id": r.get("id") or key})
        if not found:
            return None
        if preferred:
            hit = next((s for s in found if norm(s["id"]) == norm(preferred)), None)
            if hit:
                return hit
        tiered = next((s for s in found if str(s.get("subscription_tier") or "").lower() in ("business", "enterprise")),
                      None)
        return tiered or found[0]

    @staticmethod
    def parse(j: dict[str, Any], ws: Optional[dict[str, Any]] = None) -> Snapshot:
        ws = ws or {}
        snap = Snapshot(plan=title_case(ws.get("subscription_tier") or ""))
        if ws.get("name"):
            snap.notes.append(str(ws["name"]))
        for bucket, label in (("auto", "Standard credits"), ("flex", "Select credits")):
            entries = [a for a in (j.get("allowances") or []) if isinstance(a, dict)
                       and a.get("bucketId") == bucket and a.get("cadence") == "billing_period"
                       and a.get("state") == "resolved"]
            if len(entries) != 1:
                continue
            a = entries[0]
            credits = a.get("credits") if isinstance(a.get("credits"), dict) else {}
            used, limit = fnum(credits.get("used")), fnum(credits.get("limit"))
            detail = ""
            if used is None or not limit:
                used, limit = fnum(a.get("used")), fnum(a.get("limit"))
            else:
                detail = f"{max(0.0, limit - used):,.0f} of {limit:,.0f} left"
            pct = pct_of(used, limit)
            if pct is None:
                continue
            start, end = parse_time(a.get("periodStartMs")), parse_time(a.get("periodEndMs"))
            snap.windows.append(UsageWindow(label, pct, end, (end - start) if start and end and end > start else None,
                                            detail))
        if not snap.windows:  # older shape: rolling + billing-period windows
            w = j.get("window") if isinstance(j.get("window"), dict) else None
            if w and pct_of(fnum(w.get("used")), fnum(w.get("limit"))) is not None:
                secs = fnum(j.get("resetsInSeconds"))
                m = re.match(r"^(\d+)([mhdw])$", str(w.get("window") or "").strip().lower())
                length = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)] if m else None
                snap.windows.append(UsageWindow(f"Rolling ({w.get('window')})" if m else "Rolling",
                                                pct_of(fnum(w.get("used")), fnum(w.get("limit"))),
                                                time.time() + secs if secs and secs > 0 else None, length))
            b = j.get("billingPeriodWindow") if isinstance(j.get("billingPeriodWindow"), dict) else None
            if b and pct_of(fnum(b.get("used")), fnum(b.get("limit"))) is not None:
                end = parse_time(b.get("periodEndMs"))
                snap.windows.append(UsageWindow("Monthly", pct_of(fnum(b.get("used")), fnum(b.get("limit"))), end,
                                                30 * 86400 if end else None))
        if not snap.windows:
            if str(j.get("status") or "").lower() == "not_applicable":
                raise ProviderError("Notion doesn't meter AI usage on this workspace (Business/Enterprise only)")
            raise ProviderError("Notion returned no AI allowance for this workspace")
        if str(j.get("enforcement") or "").lower() == "preview":
            snap.notes.append("Allowance not enforced yet (preview)")
        return snap


# --------------------------------------------------------------------------------------------------
# Mistral (Le Chat Pro / Team: Vibe + API allowances)
# --------------------------------------------------------------------------------------------------

class MistralProvider(CookieProvider):
    type = "mistral"
    default_name = "Mistral"
    vendor = "Mistral AI"
    plans = "Le Chat Pro, Team (Vibe, API budget)"
    setup = ("Paste your admin.mistral.ai session (the ory_session_… and csrftoken cookies), e.g. 'Copy as cURL' "
             "from admin.mistral.ai/subscription.")
    usage_url = "https://admin.mistral.ai/subscription"
    accent = "#fa500f"
    site = "admin.mistral.ai"
    cookie_prefixes = ("ory_session_",)
    cookie_names = ("csrftoken",)
    min_poll = 300
    fields = (cookie_field("admin.mistral.ai", "Needs ory_session_… and csrftoken. "),)
    VIBE = ("https://console.mistral.ai/api-ui/trpc/billing.vibeUsage?batch=1&input="
            + urllib.parse.quote('{"0":{"json":null,"meta":{"values":["undefined"],"v":1}}}', safe=""))

    def fetch(self) -> Snapshot:
        cookie, extra = self.session()
        csrf = extra.get("x-csrftoken") or extra.get("X-CSRFToken") or \
            dict(p.split("=", 1) for p in cookie.split("; ") if "=" in p).get("csrftoken", "")
        budgets: dict[str, dict[str, Any]] = {}
        try:
            page = http_text("https://admin.mistral.ai/subscription",
                             {**self.browser_headers("https://admin.mistral.ai"), "Cookie": cookie,
                              "Accept": "text/html"}, follow_redirects=False)
            budgets = self.parse_budgets(page)
        except HttpError as e:
            if e.status in (401, 403) or 300 <= e.status < 400:
                raise self.expired(e) from None
        snap = Snapshot(plan="Le Chat")
        for key, label in (("vibe", "Vibe (mo)"), ("api", "API budget (mo)")):
            b = budgets.get(key)
            if b:
                snap.windows.append(b_window(label, b))
        if "vibe" not in budgets and csrf:
            try:
                j = http_json(self.VIBE, {**self.browser_headers("https://console.mistral.ai"),
                                          "Cookie": pick_cookies(cookie, ("csrftoken",), ("ory_session_",)),
                                          "X-CSRFToken": csrf})
                w = self.parse_vibe(j)
                if w:
                    snap.windows.insert(0, w)
            except HttpError as e:
                if not snap.windows:
                    raise self.expired(e) from None
        if not snap.windows:
            raise ProviderError("Mistral returned no subscription usage (is this account on Le Chat Pro/Team?)")
        return snap

    @staticmethod
    def parse_budgets(page: str) -> dict[str, dict[str, Any]]:
        """The subscription page embeds its data as Next.js flight chunks; the budgets are plain JSON inside."""
        text = page.replace('\\"', '"').replace("\\\\", "\\")
        out: dict[str, dict[str, Any]] = {}
        for m in re.finditer(r'"(vibe|api)_budget"\s*:\s*\{([^{}]*)\}', text):
            body = m.group(2)
            get = lambda k: (re.search(rf'"{k}"\s*:\s*("?)([^",}}]*)\1', body) or [None, None, None])[2]  # noqa: E731
            pct, init = fnum(get("usage_percentage")), fnum(get("initial_budget"))
            if pct is None:
                continue
            out.setdefault(m.group(1), {"pct": pct, "limit": init, "currency": get("currency") or "",
                                        "reset": parse_time(get("reset_at"))})
        return out

    @staticmethod
    def parse_vibe(j: Any) -> Optional[UsageWindow]:
        first = j[0] if isinstance(j, list) and j else j
        data = deep_find(first, lambda d: "usage_percentage" in d)
        if not data or fnum(data.get("usage_percentage")) is None:
            return None
        return UsageWindow("Vibe (mo)", max(0.0, min(100.0, float(data["usage_percentage"]))),
                           parse_time(data.get("reset_at")), 30 * 86400)


def b_window(label: str, b: dict[str, Any]) -> UsageWindow:
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(str(b.get("currency") or "").upper(), "")
    detail = ""
    if b.get("limit"):
        used = b["limit"] * b["pct"] / 100
        detail = f"{sym}{used:,.2f} of {sym}{b['limit']:,.2f}"
    return UsageWindow(label, max(0.0, min(100.0, b["pct"])), b.get("reset"), 30 * 86400 if b.get("reset") else None,
                       detail)


# --------------------------------------------------------------------------------------------------
# Perplexity
# --------------------------------------------------------------------------------------------------

class PerplexityProvider(CookieProvider):
    type = "perplexity"
    default_name = "Perplexity"
    vendor = "Perplexity"
    plans = "Pro, Max (usage credits)"
    setup = "Paste your perplexity.ai session (the __Secure-next-auth.session-token cookie)."
    usage_url = "https://www.perplexity.ai/account/usage"
    accent = "#20b8cd"
    site = "www.perplexity.ai"
    cookie_names = ("__Secure-authjs.session-token", "authjs.session-token", "__Secure-next-auth.session-token",
                    "next-auth.session-token")
    cookie_prefixes = ("__Secure-next-auth.session-token.", "__Secure-authjs.session-token.")
    bare_cookie = "__Secure-next-auth.session-token"
    fields = (cookie_field("perplexity.ai"),)

    def fetch(self) -> Snapshot:
        cookie, _ = self.session()
        try:
            j = http_json("https://www.perplexity.ai/rest/billing/credits?version=2.18&source=default",
                          {**self.browser_headers("https://www.perplexity.ai", "https://www.perplexity.ai/account/usage"),
                           "Cookie": cookie})
        except HttpError as e:
            raise self.expired(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        g = lambda *ks: next((fnum(j.get(k)) for k in ks if fnum(j.get(k)) is not None), None)  # noqa: E731
        used_total = g("total_usage_cents", "totalUsageCents")
        balance = g("balance_cents", "balanceCents")
        renewal = parse_time(j.get("renewal_date_ts") or j.get("renewalDateTs"))
        grants = j.get("credit_grants") or j.get("creditGrants") or []
        if used_total is None or not isinstance(grants, list):
            raise ProviderError("Perplexity returned no credit data")
        amt = lambda t: sum(max(0.0, fnum(x.get("amount_cents", x.get("amountCents"))) or 0)  # noqa: E731
                            for x in grants if isinstance(x, dict) and x.get("type") == t)
        recurring = amt("recurring")
        snap = Snapshot(plan="")
        if recurring > 0:
            used = min(used_total, recurring)
            snap.windows.append(UsageWindow("Monthly credits", pct_of(used, recurring), renewal,
                                            30 * 86400 if renewal else None,
                                            f"${(recurring - used) / 100:,.2f} of ${recurring / 100:,.2f} left"))
        if balance is not None:
            snap.notes.append(f"Balance: ${balance / 100:,.2f}")
        extra = amt("purchased") + amt("promotional")
        if extra:
            snap.notes.append(f"Bonus/purchased credits: ${extra / 100:,.2f}")
        if not snap.windows and not snap.notes:
            raise ProviderError("Perplexity returned no credit data")
        return snap


# --------------------------------------------------------------------------------------------------
# T3 Chat
# --------------------------------------------------------------------------------------------------

class T3ChatProvider(CookieProvider):
    type = "t3chat"
    default_name = "T3 Chat"
    vendor = "T3"
    plans = "Pro"
    setup = ("Paste the whole 'Copy as cURL' of any t3.chat request (the Cookie header alone can trip Vercel's "
             "bot check).")
    usage_url = "https://t3.chat/settings/customization"
    accent = "#ec4899"
    site = "t3.chat"
    fields = (cookie_field("t3.chat"),)

    def fetch(self) -> Snapshot:
        cookie, extra = self.session()
        inp = urllib.parse.quote('{"0":{"json":{"sessionId":null},"meta":{"values":{"sessionId":["undefined"]}}}}',
                                 safe="")
        try:
            body = http_text(f"https://t3.chat/api/trpc/getCustomerData?batch=1&input={inp}",
                             {**self.browser_headers("https://t3.chat", self.usage_url), "Cookie": cookie,
                              "trpc-accept": "application/jsonl", "x-trpc-source": "web-client", **extra})
        except HttpError as e:
            if e.status == 429:
                raise ProviderError("T3 Chat showed a bot check; paste the full 'Copy as cURL' instead") from None
            raise self.expired(e) from None
        return self.parse(body)

    @staticmethod
    def parse(body: str) -> Snapshot:
        import json
        data = None
        for line in body.splitlines() or [body]:
            try:
                data = deep_find(json.loads(line), lambda d: "usageFourHourPercentage" in d
                                 or "usageMonthPercentage" in d or ("subscription" in d and "usageBand" in d))
            except ValueError:
                continue
            if data:
                break
        if not data:
            raise ProviderError("T3 Chat returned no usage data")
        sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
        plan = title_case(sub.get("productName") or data.get("subTier") or "")
        snap = Snapshot(plan=plan)
        four = fnum(data.get("usageFourHourPercentage"))
        if four is not None:
            snap.windows.append(UsageWindow("Base (4h)", max(0.0, min(100.0, four)),
                                            parse_time(data.get("usageFourHourNextResetAt"))
                                            or parse_time(data.get("usageWindowNextResetAt")), 4 * 3600,
                                            str(data.get("usageBand") or "")))
        month = fnum(data.get("usageMonthPercentage", data.get("usagePeriodPercentage")))
        if month is not None:
            end = parse_time(sub.get("currentPeriodEnd"))
            snap.windows.append(UsageWindow("Overage (mo)", max(0.0, min(100.0, month)), end,
                                            30 * 86400 if end else None))
        if not snap.windows:
            raise ProviderError("T3 Chat returned no usage data")
        return snap


# --------------------------------------------------------------------------------------------------
# Raycast
# --------------------------------------------------------------------------------------------------

class RaycastProvider(CookieProvider):
    type = "raycast"
    default_name = "Raycast AI"
    vendor = "Raycast"
    plans = "Pro, Pro+, Max (AI credits)"
    setup = "Paste your raycast.com session (the __raycast_session cookie, from raycast.com/settings)."
    usage_url = "https://www.raycast.com/settings"
    accent = "#ff6363"
    site = "www.raycast.com"
    cookie_names = ("__raycast_session", "csrf_token")
    bare_cookie = "__raycast_session"
    fields = (cookie_field("raycast.com/settings"),)
    TIERS = {"pro": "Pro", "pro_plus": "Pro+", "max": "Max"}

    def fetch(self) -> Snapshot:
        cookie, _ = self.session()
        try:
            j = http_json("https://www.raycast.com/frontend_api/current_user/ai_credits",
                          {**self.browser_headers("https://www.raycast.com", self.usage_url), "Cookie": cookie})
        except HttpError as e:
            raise self.expired(e) from None
        return self.parse(j)

    @classmethod
    def parse(cls, j: dict[str, Any]) -> Snapshot:
        left, total = fnum(j.get("remaining_balance_credits")), fnum(j.get("total_balance_credits"))
        tier = str(((j.get("funding_subscription") or {}) if isinstance(j.get("funding_subscription"), dict)
                    else {}).get("tier") or "")
        snap = Snapshot(plan=cls.TIERS.get(tier, title_case(tier)))
        reset = parse_time(j.get("next_credits_at"))
        if left is not None and total:
            snap.windows.append(UsageWindow("AI credits (mo)", pct_of(total - left, total), reset,
                                            30 * 86400 if reset else None, f"{left:,.0f} of {total:,.0f} left"))
        elif left is not None:
            snap.notes.append(f"{left:,.0f} AI credits left")
        else:
            raise ProviderError("Raycast returned no credit data")
        return snap


# --------------------------------------------------------------------------------------------------
# Manus
# --------------------------------------------------------------------------------------------------

class ManusProvider(CookieProvider):
    type = "manus"
    default_name = "Manus"
    vendor = "Manus"
    plans = "Starter, Pro (credits)"
    setup = "Paste your manus.im session (the session_id cookie)."
    usage_url = "https://manus.im/app"
    accent = "#111827"
    site = "manus.im"
    cookie_names = ("session_id",)
    bare_cookie = "session_id"
    fields = (cookie_field("manus.im"),)

    def fetch(self) -> Snapshot:
        cookie, _ = self.session()
        token = cookie.split("=", 1)[1]
        try:
            j = http_json("https://api.manus.im/user.v1.UserService/GetAvailableCredits",
                          {**self.browser_headers("https://manus.im"), "Authorization": f"Bearer {token}",
                           "Connect-Protocol-Version": "1"}, json_body={})
        except HttpError as e:
            raise self.expired(e) from None
        return self.parse(j)

    @staticmethod
    def parse(root: dict[str, Any]) -> Snapshot:
        d = next((root[k] for k in ("data", "result", "response", "availableCredits") if isinstance(root.get(k), dict)),
                 root)
        keys = ("totalCredits", "freeCredits", "periodicCredits", "refreshCredits", "maxRefreshCredits",
                "proMonthlyCredits")
        if not any(k in d for k in keys):
            raise ProviderError("Manus returned no credit data")
        n = lambda k: fnum(d.get(k)) or 0.0  # noqa: E731
        snap = Snapshot()
        monthly, periodic = n("proMonthlyCredits"), n("periodicCredits")
        if monthly > 0:
            snap.windows.append(UsageWindow("Monthly credits", pct_of(monthly - periodic, monthly), None, None,
                                            f"{periodic:,.0f} of {monthly:,.0f} left"))
        mx, refresh = n("maxRefreshCredits"), n("refreshCredits")
        if mx > 0:
            r = d.get("nextRefreshTime")
            reset = parse_time(r + 978307200 if isinstance(r, (int, float)) and 0 < r < 1e9 else r)
            daily = str(d.get("refreshInterval") or "").lower().startswith(("day", "daily"))
            snap.windows.append(UsageWindow("Daily credits" if daily else "Refresh credits",
                                            pct_of(mx - refresh, mx), reset, 86400 if daily else None,
                                            f"{refresh:,.0f} of {mx:,.0f} left"))
        snap.notes.append(f"Balance: {n('totalCredits'):,.0f} credits")
        return snap


# --------------------------------------------------------------------------------------------------
# Ollama Cloud
# --------------------------------------------------------------------------------------------------

class OllamaProvider(CookieProvider):
    type = "ollama"
    default_name = "Ollama Cloud"
    vendor = "Ollama"
    plans = "Free, Pro, Max"
    setup = "Paste your ollama.com session (Cookie header from ollama.com/settings)."
    usage_url = "https://ollama.com/settings"
    accent = "#e5e7eb"
    site = "ollama.com/settings"
    fields = (cookie_field("ollama.com/settings"),)
    LABELS = (("Session usage", "Session", 5 * 3600), ("Hourly usage", "Hourly", 3600),
              ("Weekly usage", "Weekly", 7 * 86400), ("Monthly usage", "Monthly", 30 * 86400),
              ("Free usage", "Monthly", 30 * 86400))

    def fetch(self) -> Snapshot:
        cookie, _ = self.session()
        try:
            page = http_text(self.usage_url, {**self.browser_headers("https://ollama.com"), "Cookie": cookie,
                                              "Accept": "text/html"}, follow_redirects=False)
        except HttpError as e:
            raise self.expired(e) from None
        return self.parse(page)

    @classmethod
    def parse(cls, page: str) -> Snapshot:
        starts = []
        for text, label, length in cls.LABELS:
            m = re.search(rf">\s*{re.escape(text)}\s*<", page)
            if m:
                starts.append((m.end(), label, length))
        starts.sort()
        snap = Snapshot()
        plan = re.search(r"(?:Included usage|Cloud Usage)\s*</span>\s*<span[^>]*>([^<]+)</span", page, re.S)
        if plan:
            snap.plan = htmllib.unescape(plan.group(1)).strip().title()
        seen = set()
        for i, (pos, label, length) in enumerate(starts):
            chunk = page[pos: starts[i + 1][0] if i + 1 < len(starts) else pos + 4000][:4000]
            pct = None
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", chunk, re.I)
            if m:
                pct = float(m.group(1))
            else:
                m = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)\s+of\s+\$([0-9,]+(?:\.[0-9]+)?)\s+used", chunk, re.I)
                if m:
                    pct = pct_of(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
                else:
                    m = re.search(r"width:\s*([0-9]+(?:\.[0-9]+)?)%", chunk)
                    pct = float(m.group(1)) if m else None
            if pct is None or label in seen:
                continue
            seen.add(label)
            t = re.search(r'data-time="([^"]+)"', chunk)
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, pct)), parse_time(t.group(1)) if t else None,
                                            length))
        if not snap.windows:
            if re.search(r"/signin|Sign in to Ollama", page, re.I):
                raise AuthExpired("Ollama session expired; paste a fresh one from ollama.com/settings")
            raise ProviderError("Couldn't find usage on ollama.com/settings (page layout may have changed)")
        return snap


# --------------------------------------------------------------------------------------------------
# Augment Code
# --------------------------------------------------------------------------------------------------

class AugmentProvider(CookieProvider):
    type = "augment"
    default_name = "Augment Code"
    vendor = "Augment"
    plans = "Indie, Developer, Pro, Max (credits)"
    setup = "Paste your app.augmentcode.com session (Cookie header from app.augmentcode.com/account/subscription)."
    usage_url = "https://app.augmentcode.com/account/subscription"
    accent = "#a3e635"
    site = "app.augmentcode.com"
    cookie_names = ("session", "_session", "web_rpc_proxy_session", "auth0", "__Secure-next-auth.session-token",
                    "next-auth.session-token", "__Secure-authjs.session-token", "authjs.session-token")
    bare_cookie = "_session"
    fields = (cookie_field("app.augmentcode.com"),)

    def fetch(self) -> Snapshot:
        cookie, _ = self.session()
        h = {**self.browser_headers("https://app.augmentcode.com", self.usage_url), "Cookie": cookie}
        try:
            credits = http_json("https://app.augmentcode.com/api/credits", h)
        except HttpError as e:
            raise self.expired(e) from None
        try:
            sub = http_json("https://app.augmentcode.com/api/subscription", h)
        except (HttpError, ProviderError):
            sub = {}
        return self.parse(credits, sub)

    @staticmethod
    def parse(c: dict[str, Any], sub: Optional[dict[str, Any]] = None) -> Snapshot:
        sub = sub or {}
        left, used = fnum(c.get("usageUnitsRemaining")), fnum(c.get("usageUnitsConsumedThisBillingCycle"))
        avail = fnum(c.get("usageUnitsAvailable"))
        limit = avail if avail and avail > 0 else (left + used if left is not None and used is not None else None)
        if used is None and left is not None and limit:
            used = limit - left
        snap = Snapshot(plan=str(sub.get("planName") or ""))
        end = parse_time(sub.get("billingPeriodEnd"))
        pct = pct_of(used, limit)
        if pct is not None:
            snap.windows.append(UsageWindow("Credits (mo)", pct, end, 30 * 86400 if end else None,
                                            f"{(limit or 0) - (used or 0):,.0f} of {limit:,.0f} left"))
        elif left is not None:
            snap.notes.append(f"{left:,.0f} credits left")
        else:
            raise ProviderError("Augment returned no credit data")
        return snap


# --------------------------------------------------------------------------------------------------
# Zed
# --------------------------------------------------------------------------------------------------

class ZedProvider(CookieProvider):
    type = "zed"
    default_name = "Zed"
    vendor = "Zed Industries"
    plans = "Free, Pro"
    auth = "auto"
    setup = ("macOS: uses the Zed editor's sign-in from the Keychain (you may be asked once to allow it). "
             "Elsewhere: paste your zed.dev session in Settings.")
    usage_url = "https://zed.dev/account"
    accent = "#0751cf"
    site = "zed.dev/account"
    fields = (cookie_field("zed.dev/account", "Optional on macOS. "),)

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self._editor_auth: Optional[str] = None

    def _keychain(self) -> Optional[str]:
        """Zed stores `<user id>` / `<access token>` as an internet password for https://zed.dev."""
        if self._editor_auth is not None:
            return self._editor_auth or None
        self._editor_auth = ""
        if not IS_MAC:
            return None
        out = run_cmd(["security", "find-internet-password", "-s", "zed.dev", "-g"], timeout=20)
        # -g prints the password on stderr, which run_cmd drops, so ask again for it with -w.
        token = run_cmd(["security", "find-internet-password", "-s", "zed.dev", "-w"], timeout=20)
        acct = re.search(r'"acct"<blob>="([^"]+)"', out or "")
        if acct and token and token.strip():
            self._editor_auth = f"{acct.group(1)} {token.strip()}"
        return self._editor_auth or None

    def fetch(self) -> Snapshot:
        cookie, _ = self.session(required=False)
        if cookie:
            try:
                j = http_json("https://cloud.zed.dev/frontend/billing/usage",
                              {**self.browser_headers("https://zed.dev"), "Cookie": cookie})
            except HttpError as e:
                raise self.expired(e) from None
            return self.parse(j, web=True)
        auth = self._keychain()
        if not auth:
            raise NotConfigured("No Zed sign-in found; sign in from the Zed editor (macOS) or paste a zed.dev session")
        try:
            j = http_json("https://cloud.zed.dev/client/users/me", {"Authorization": auth})
        except HttpError as e:
            if e.status in (401, 403):
                self._editor_auth = None
                raise AuthExpired("Zed sign-in expired; sign in again from the editor") from None
            raise
        return self.parse(j, web=False)

    @staticmethod
    def parse(j: dict[str, Any], web: bool) -> Snapshot:
        plan_obj = j.get("plan")
        plan = plan_obj if isinstance(plan_obj, str) else (plan_obj or {}).get("plan_v3") if isinstance(plan_obj, dict) else ""
        usage = (j.get("current_usage") if web else (plan_obj or {}).get("usage")) or {}
        snap = Snapshot(plan=title_case(plan))
        ep = usage.get("edit_predictions") if isinstance(usage, dict) else None
        period = (plan_obj or {}).get("subscription_period") if isinstance(plan_obj, dict) else None
        end = parse_time((period or {}).get("ended_at")) if isinstance(period, dict) else None
        start = parse_time((period or {}).get("started_at")) if isinstance(period, dict) else None
        if isinstance(ep, dict):
            lim = ep.get("limit")
            lim = lim.get("limited") if isinstance(lim, dict) else lim
            used = fnum(ep.get("used"))
            if lim == "unlimited" or lim is None:
                snap.notes.append("Edit predictions: unlimited")
            elif pct_of(used, fnum(lim)) is not None:
                snap.windows.append(UsageWindow("Edit predictions", pct_of(used, fnum(lim)), end,
                                                (end - start) if start and end else None,
                                                f"{used:,.0f} of {fnum(lim):,.0f}"))
        spend = usage.get("token_spend") if isinstance(usage, dict) else None
        if isinstance(spend, dict):
            s, cap = fnum(spend.get("spend_in_cents")), fnum(spend.get("limit_in_cents"))
            if s is not None and cap:
                snap.windows.append(UsageWindow("Token spend", pct_of(s, cap), end, None,
                                                f"${s / 100:,.2f} of ${cap / 100:,.2f}"))
            elif s is not None:
                snap.notes.append(f"Token spend: ${s / 100:,.2f}")
        if isinstance(plan_obj, dict) and plan_obj.get("has_overdue_invoices"):
            snap.notes.append("Overdue invoice")
        if not snap.windows and not snap.notes:
            raise ProviderError("Zed returned no usage data")
        return snap


# --------------------------------------------------------------------------------------------------
# Devin (Cognition)
# --------------------------------------------------------------------------------------------------

class DevinProvider(CookieProvider):
    type = "devin"
    default_name = "Devin"
    vendor = "Cognition"
    plans = "Core, Team (daily/weekly quotas)"
    setup = ("Paste the 'Copy as cURL' of a request from app.devin.ai/settings/usage (it carries the Bearer token and "
             "your organization), or the token and organization separately.")
    usage_url = "https://app.devin.ai/settings/usage"
    accent = "#38bdf8"
    site = "app.devin.ai"
    fields = (
        Field("cookie", "Request (cURL) or token", "secret",
              "From app.devin.ai → DevTools → Network → a billing request → Copy as cURL; or just the Bearer token."),
        Field("org", "Organization", "text", "Org slug, internal org-… ID, or an app.devin.ai/org/… URL. "
              "Blank = read it from the pasted cURL."),
    )

    def _auth(self) -> tuple[str, str, str]:
        raw = self.setting("cookie")
        if not raw:
            raise NotConfigured("No Devin session; paste a request from app.devin.ai in Settings")
        _cookie, extra = parse_cookie_input(raw)
        token = ""
        for k, v in extra.items():
            if k.lower() == "authorization":
                token = v
        if not token and not raw.lower().startswith("curl"):
            token = raw.strip()
        token = re.sub(r"^bearer\s+", "", token, flags=re.I).strip()
        org = self.setting("org")
        internal = next((v for k, v in extra.items() if k.lower() == "x-cog-org-id"), "")
        if not org:
            m = re.search(r"app\.devin\.ai/api/([^'\"\s]+?)/billing/", raw)
            org = m.group(1) if m else internal
        m = re.search(r"devin\.ai/(org|organizations)/([^/?#\s]+)", org)
        if m:
            org = f"{m.group(1)}/{m.group(2)}"
        if not token or not org:
            raise NotConfigured("Devin needs both the Bearer token and your organization")
        if org.startswith(("org-", "org_")):
            internal = internal or org
        return token, org.strip("/"), internal

    def fetch(self) -> Snapshot:
        token, org, internal = self._auth()
        h = {"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA}
        if internal:
            h["x-cog-org-id"] = internal
        paths = [f"{org}/billing/quota/usage"]
        if not org.startswith(("org/", "organizations/", "org-", "org_")):
            paths.append(f"org/{org}/billing/quota/usage")
        last: Optional[HttpError] = None
        for p in paths:
            try:
                return self.parse(http_json(f"https://app.devin.ai/api/{p}", h))
            except HttpError as e:
                if e.status in (401, 403):
                    raise AuthExpired("Devin token expired; paste a fresh request from app.devin.ai") from None
                last = e
        raise ProviderError(f"Devin: HTTP {last.status if last else '?'} for organization {org!r}")

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        snap = Snapshot()
        for key, label, length in (("daily", "Daily", 86400), ("weekly", "Weekly", 7 * 86400)):
            if key == "daily" and j.get("hide_daily_quota"):
                continue
            v = fnum(j.get(f"{key}_percentage"))
            if v is None:
                continue
            v = v * 100 if v < 1 else v
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, v)), parse_time(j.get(f"{key}_reset_at")),
                                            length))
        bal = fnum(j.get("overage_balance"))
        if bal is None and fnum(j.get("overage_balance_cents")) is not None:
            bal = float(j["overage_balance_cents"]) / 100
        if bal:
            snap.notes.append(f"Overage balance: ${bal:,.2f}")
        if not snap.windows:
            raise ProviderError("Devin returned no quota data")
        return snap
