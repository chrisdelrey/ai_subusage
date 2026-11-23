"""Pay-as-you-go API usage: month-to-date spend or remaining credit, read with just an API key.

Every endpoint is pre-set; paste the key (env:VAR works) and, optionally, a budget:
- spend providers (OpenAI, Anthropic, DeepInfra…): "Monthly budget" turns month-to-date spend into a bar that
  resets on the 1st (UTC);
- balance providers (OpenRouter, DeepSeek, fal…): "Full balance" (what you top up to) turns the remaining
  credit into a bar of how much of it you've used.
Without a budget the numbers show as a line under the provider.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any, Optional

from ..core import (Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow, fnum, http_json,
                    parse_time, pct_of, title_case)

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥"}


def money(v: float, currency: str = "USD") -> str:
    sym = SYMBOLS.get(currency.upper(), "")
    return f"{sym}{v:,.2f}" if sym else f"{v:,.2f} {currency.upper()}"


def month_bounds(now: Optional[dt.datetime] = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt


def api_fields(key_help: str, kind: str = "spend", extra: tuple[Field, ...] = ()) -> tuple[Field, ...]:
    budget = (Field("budget", "Monthly budget", "text", "Optional, e.g. 50. Shows this month's spend as a bar against it.")
              if kind == "spend" else
              Field("budget", "Full balance", "text",
                    "Optional: what you top up to, e.g. 25. Shows how much of it you've used as a bar."))
    return (Field("api_key", "API key", "secret", key_help),) + extra + (budget,)


class ApiProvider(Provider):
    """Base for API-usage providers. Subclasses implement fetch() using spend()/balance() helpers."""

    auth = "api"
    parks_on_auth = False
    min_poll = 300
    default_poll = 900
    key_url = ""                  # opened by the "Get key" button in Settings
    key_help = ""                 # help text under the key field
    budget_kind = "spend"         # "spend" (Monthly budget) or "balance" (Full balance)
    currency = "USD"

    def key(self) -> str:
        return self.require_key()

    def budget(self) -> Optional[float]:
        b = fnum(self.setting("budget").replace("$", "").replace(",", "").strip() or None)
        return b if b and b > 0 else None

    def rejected(self, e: HttpError) -> Exception:
        if e.status in (401, 403):
            return ProviderError(f"{self.default_name} rejected the key. {self.key_hint()}")
        return e

    def key_hint(self) -> str:
        return "Check it in Settings."

    def spend(self, snap: Snapshot, amount: float, currency: str = "", label: str = "Spend (mo)") -> Snapshot:
        currency = currency or self.currency
        start, end = month_bounds()
        b = self.budget()
        if b:
            snap.windows.append(UsageWindow(label, pct_of(amount, b), end.timestamp(), (end - start).total_seconds(),
                                            f"{money(amount, currency)} of {money(b, currency)}"))
        else:
            snap.notes.append(f"Spent this month: {money(amount, currency)}")
        return snap

    def balance(self, snap: Snapshot, amount: float, currency: str = "", label: str = "Credit used") -> Snapshot:
        currency = currency or self.currency
        b = self.budget()
        if b:
            snap.windows.append(UsageWindow(label, pct_of(b - amount, b), None, None,
                                            f"{money(max(0.0, amount), currency)} of {money(b, currency)} left"))
        else:
            snap.notes.append(f"Balance: {money(amount, currency)}")
        return snap


# --------------------------------------------------------------------------------------------------
# OpenAI API (admin key → organization costs)
# --------------------------------------------------------------------------------------------------

class OpenAIApiProvider(ApiProvider):
    type = "openai_api"
    default_name = "OpenAI API"
    vendor = "OpenAI"
    plans = "Pay as you go (month-to-date spend)"
    setup = ("Needs an Admin key (sk-admin-…), not a normal project key: platform.openai.com → Settings → "
             "Organization → Admin keys → Create new admin key (you must be an organization Owner). "
             "Read-only is enough.")
    key_url = "https://platform.openai.com/settings/organization/admin-keys"
    key_help = "Admin key (sk-admin-…). env:OPENAI_ADMIN_KEY works too."
    usage_url = "https://platform.openai.com/usage"
    accent = "#10a37f"
    fields = api_fields("Admin key (sk-admin-…). env:OPENAI_ADMIN_KEY works too.", "spend", (Field("project_id", "Project ID", "text",
                                            "Optional: only count one project (proj_…)."),))

    def key_hint(self) -> str:
        return "It must be an Admin key (sk-admin-…) from Organization → Admin keys."

    def fetch(self) -> Snapshot:
        key = self.key()
        start, _end = month_bounds()
        q = {"start_time": str(int(start.timestamp())), "bucket_width": "1d", "limit": "31"}
        if self.setting("project_id"):
            q["project_ids"] = self.setting("project_id")
        total, page, currency = 0.0, None, "USD"
        for _ in range(10):
            params = dict(q, **({"page": page} if page else {}))
            try:
                j = http_json("https://api.openai.com/v1/organization/costs?" + urllib.parse.urlencode(params),
                              {"Authorization": f"Bearer {key}"})
            except HttpError as e:
                raise self.rejected(e) from None
            t, currency = self.sum_costs(j)
            total += t
            page = j.get("next_page") if j.get("has_more") else None
            if not page:
                break
        return self.spend(Snapshot(), total, currency)

    @staticmethod
    def sum_costs(j: dict[str, Any]) -> tuple[float, str]:
        if not isinstance(j.get("data"), list):
            raise ProviderError("OpenAI returned no cost data")
        total, currency = 0.0, "USD"
        for bucket in j["data"]:
            for r in (bucket or {}).get("results") or []:
                amt = (r or {}).get("amount") or {}
                total += fnum(amt.get("value")) or 0.0
                currency = str(amt.get("currency") or currency).upper()
        return total, currency


# --------------------------------------------------------------------------------------------------
# Anthropic API (admin key → cost report)
# --------------------------------------------------------------------------------------------------

class AnthropicApiProvider(ApiProvider):
    type = "anthropic_api"
    default_name = "Anthropic API"
    vendor = "Anthropic"
    plans = "Pay as you go (month-to-date spend)"
    setup = ("Needs an Admin API key (sk-ant-admin01-…); a normal API key can't read costs. Individual Console accounts "
             "can't create one: first set up an organization at platform.claude.com/settings/organization. Then, as an "
             "organization admin, open platform.claude.com/settings/admin-keys → Create admin key → name it, pick an "
             "expiration → Add, and copy the key (it's shown once). Claude Enterprise (claude.ai) orgs aren't supported.")
    key_url = "https://platform.claude.com/settings/admin-keys"
    key_help = "Admin key (sk-ant-admin01-…). env:ANTHROPIC_ADMIN_KEY works too."
    usage_url = "https://platform.claude.com/usage"
    accent = "#d97757"
    fields = api_fields("Admin key (sk-ant-admin01-…). env:ANTHROPIC_ADMIN_KEY works too.", "spend")

    def key_hint(self) -> str:
        return ("It must be an Admin key (sk-ant-admin01-…) from platform.claude.com/settings/admin-keys, "
                "which needs an organization (not an individual account).")

    def fetch(self) -> Snapshot:
        key = self.key()
        start, end = month_bounds()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        q = {"starting_at": start.strftime(fmt), "ending_at": end.strftime(fmt), "bucket_width": "1d", "limit": "31"}
        total, page = 0.0, None
        for _ in range(10):
            params = dict(q, **({"page": page} if page else {}))
            try:
                j = http_json("https://api.anthropic.com/v1/organizations/cost_report?" + urllib.parse.urlencode(params),
                              {"x-api-key": key, "anthropic-version": "2023-06-01"})
            except HttpError as e:
                raise self.rejected(e) from None
            total += self.sum_costs(j)
            page = j.get("next_page") if j.get("has_more") else None
            if not page:
                break
        return self.spend(Snapshot(), total)

    @staticmethod
    def sum_costs(j: dict[str, Any]) -> float:
        if not isinstance(j.get("data"), list):
            raise ProviderError("Anthropic returned no cost data")
        cents = 0.0
        for bucket in j["data"]:
            for r in (bucket or {}).get("results") or []:
                cents += fnum((r or {}).get("amount")) or 0.0  # decimal string in cents
        return cents / 100


# --------------------------------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------------------------------

class OpenRouterProvider(ApiProvider):
    type = "openrouter"
    default_name = "OpenRouter"
    vendor = "OpenRouter"
    plans = "Credits, per-key limits"
    setup = "openrouter.ai → Settings → Keys → Create key, then paste it. Shows your credit balance and the key's limit."
    key_url = "https://openrouter.ai/settings/keys"
    key_help = "sk-or-… key. env:OPENROUTER_API_KEY works too."
    usage_url = "https://openrouter.ai/activity"
    accent = "#6467f2"
    budget_kind = "balance"
    fields = api_fields("sk-or-… key. env:OPENROUTER_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        h = {"Authorization": f"Bearer {self.key()}"}
        try:
            key = http_json("https://openrouter.ai/api/v1/key", h)
        except HttpError as e:
            raise self.rejected(e) from None
        try:
            credits = http_json("https://openrouter.ai/api/v1/credits", h)
        except (HttpError, ProviderError):
            credits = {}
        return self.parse(key, credits)

    def parse(self, key: dict[str, Any], credits: dict[str, Any]) -> Snapshot:
        k = key.get("data") if isinstance(key.get("data"), dict) else {}
        c = credits.get("data") if isinstance(credits.get("data"), dict) else {}
        snap = Snapshot(plan="Free tier" if k.get("is_free_tier") else "")
        limit, left = fnum(k.get("limit")), fnum(k.get("limit_remaining"))
        if limit and left is not None:
            reset = str(k.get("limit_reset") or "").lower()
            label = {"daily": "Key limit (day)", "weekly": "Key limit (wk)", "monthly": "Key limit (mo)"}.get(
                reset, "Key limit")
            snap.windows.append(UsageWindow(label, pct_of(limit - left, limit), None, None,
                                            f"{money(left)} of {money(limit)} left"))
        total, used = fnum(c.get("total_credits")), fnum(c.get("total_usage"))
        if total is not None and used is not None:
            self.balance(snap, total - used)
        month = fnum(k.get("usage_monthly"))
        if month is not None:
            snap.notes.append(f"This key this month: {money(month)}")
        if not snap.windows and not snap.notes:
            raise ProviderError("OpenRouter returned no usage for this key")
        return snap


# --------------------------------------------------------------------------------------------------
# Balance-only vendors
# --------------------------------------------------------------------------------------------------

class DeepSeekProvider(ApiProvider):
    type = "deepseek"
    default_name = "DeepSeek API"
    vendor = "DeepSeek"
    plans = "Prepaid balance"
    setup = "platform.deepseek.com → API keys → Create new API key, then paste it."
    key_url = "https://platform.deepseek.com/api_keys"
    key_help = "sk-… key. env:DEEPSEEK_API_KEY works too."
    usage_url = "https://platform.deepseek.com/usage"
    accent = "#4d6bfe"
    budget_kind = "balance"
    fields = api_fields("sk-… key. env:DEEPSEEK_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        infos = [i for i in (j.get("balance_infos") or []) if isinstance(i, dict)]
        if not infos:
            raise ProviderError("DeepSeek returned no balance")
        info = next((i for i in infos if str(i.get("currency")).upper() == "USD"), infos[0])
        snap = self.balance(Snapshot(), fnum(info.get("total_balance")) or 0.0, str(info.get("currency") or "USD"))
        granted = fnum(info.get("granted_balance"))
        if granted:
            snap.notes.append(f"Includes granted: {money(granted, str(info.get('currency') or 'USD'))}")
        if j.get("is_available") is False:
            snap.notes.append("Balance too low to make calls")
        return snap


class MoonshotProvider(ApiProvider):
    type = "moonshot"
    default_name = "Moonshot API"
    vendor = "Moonshot AI"
    plans = "Kimi API prepaid balance"
    setup = "platform.moonshot.ai (or .cn) → API Keys → Create, then paste it. For Kimi Code plans use Kimi Code."
    key_url = "https://platform.moonshot.ai/console/api-keys"
    key_help = "sk-… key. env:MOONSHOT_API_KEY works too."
    usage_url = "https://platform.moonshot.ai/console/account"
    accent = "#111827"
    budget_kind = "balance"
    fields = api_fields("sk-… key. env:MOONSHOT_API_KEY works too.", "balance", (Field("region", "Region", "choice", options=(
        ("global", "Global (api.moonshot.ai, USD)"), ("china", "China (api.moonshot.cn, CNY)")), default="global"),))

    def fetch(self) -> Snapshot:
        china = self.setting("region") == "china"
        try:
            j = http_json(f"https://api.moonshot.{'cn' if china else 'ai'}/v1/users/me/balance",
                          {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j, "CNY" if china else "USD")

    def parse(self, j: dict[str, Any], currency: str = "USD") -> Snapshot:
        d = j.get("data") if isinstance(j.get("data"), dict) else None
        if j.get("code") not in (None, 0) or not d or fnum(d.get("available_balance")) is None:
            raise ProviderError(f"Moonshot: {j.get('scode') or 'no balance returned'}")
        snap = self.balance(Snapshot(), float(d["available_balance"]), currency)
        voucher = fnum(d.get("voucher_balance"))
        if voucher:
            snap.notes.append(f"Vouchers: {money(voucher, currency)}")
        return snap


class XaiApiProvider(ApiProvider):
    type = "xai_api"
    default_name = "xAI API"
    vendor = "xAI"
    plans = "Prepaid API credits"
    setup = ("Needs a Management key and your Team ID: console.x.ai → Settings → Management keys → Create "
             "(give it billing read access). The Team ID is on console.x.ai → Settings → Team.")
    key_url = "https://console.x.ai/team/default/management-keys"
    key_help = "Management key (not an inference key). env:XAI_MANAGEMENT_API_KEY works too."
    usage_url = "https://console.x.ai"
    accent = "#e5e7eb"
    budget_kind = "balance"
    fields = api_fields("Management key (not an inference key). env:XAI_MANAGEMENT_API_KEY works too.", "balance", (Field("team_id", "Team ID", "text", "From console.x.ai → Settings → Team."),))

    def key_hint(self) -> str:
        return "It must be a Management key with billing access, and the Team ID must match."

    def fetch(self) -> Snapshot:
        team = self.setting("team_id")
        if not team:
            raise NotConfigured("xAI needs your Team ID as well as the management key")
        try:
            j = http_json(f"https://management-api.x.ai/v1/billing/teams/{urllib.parse.quote(team, safe='')}"
                          "/prepaid/balance", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            if e.status == 404:
                raise ProviderError("xAI doesn't know that Team ID") from None
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        total = j.get("total") if isinstance(j.get("total"), dict) else {}
        cents = fnum(total.get("val"))
        if cents is None:
            raise ProviderError("xAI returned no balance")
        return self.balance(Snapshot(), -cents / 100)  # credit is reported as a negative amount


class VercelGatewayProvider(ApiProvider):
    type = "vercel_ai"
    default_name = "Vercel AI Gateway"
    vendor = "Vercel"
    plans = "AI Gateway credits"
    setup = "vercel.com → your team → AI Gateway → API Keys → Create key, then paste it."
    key_url = "https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%2Fapi-keys"
    key_help = "AI Gateway key. env:AI_GATEWAY_API_KEY works too."
    usage_url = "https://vercel.com/dashboard/ai"
    accent = "#e5e7eb"
    budget_kind = "balance"
    fields = api_fields("AI Gateway key. env:AI_GATEWAY_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://ai-gateway.vercel.sh/v1/credits", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        bal = fnum(j.get("balance"))
        if bal is None:
            raise ProviderError("Vercel returned no credit balance")
        snap = self.balance(Snapshot(), bal)
        used = fnum(j.get("total_used"))
        if used is not None:
            snap.notes.append(f"Used to date: {money(used)}")
        return snap


class SiliconFlowProvider(ApiProvider):
    type = "siliconflow"
    default_name = "SiliconFlow"
    vendor = "SiliconFlow"
    plans = "Prepaid balance (international)"
    setup = "cloud.siliconflow.com → API Keys → Create new key, then paste it. (The China site's balance API was retired.)"
    key_url = "https://cloud.siliconflow.com/account/ak"
    key_help = "sk-… key. env:SILICONFLOW_API_KEY works too."
    usage_url = "https://cloud.siliconflow.com/bills"
    accent = "#7c3aed"
    budget_kind = "balance"
    fields = api_fields("sk-… key. env:SILICONFLOW_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.siliconflow.com/v1/user/info", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        d = j.get("data") if isinstance(j.get("data"), dict) else {}
        bal = fnum(d.get("totalBalance"))
        if bal is None:
            raise ProviderError("SiliconFlow returned no balance")
        return self.balance(Snapshot(), bal)


class FalProvider(ApiProvider):
    type = "fal"
    default_name = "fal"
    vendor = "fal.ai"
    plans = "Prepaid credits"
    setup = "fal.ai → Dashboard → Keys → Add key (scope: Admin, so it can read billing), then paste it."
    key_url = "https://fal.ai/dashboard/keys"
    key_help = "key_id:key_secret. env:FAL_KEY works too."
    usage_url = "https://fal.ai/dashboard/billing"
    accent = "#a855f7"
    budget_kind = "balance"
    fields = api_fields("key_id:key_secret. env:FAL_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.fal.ai/v1/account/billing?expand=credits", {"Authorization": f"Key {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        c = j.get("credits") if isinstance(j.get("credits"), dict) else None
        bal = fnum(c.get("current_balance")) if c else None
        if bal is None:
            raise ProviderError("fal returned no credit balance (try again in a moment)")
        return self.balance(Snapshot(plan=str(j.get("username") or "")), bal, str(c.get("currency") or "USD"))


class LinkupProvider(ApiProvider):
    type = "linkup"
    default_name = "Linkup"
    vendor = "Linkup"
    plans = "Search API credits"
    setup = "app.linkup.so → API keys → copy your key, then paste it."
    key_url = "https://app.linkup.so/api-keys"
    key_help = "env:LINKUP_API_KEY works too."
    usage_url = "https://app.linkup.so"
    accent = "#0ea5e9"
    budget_kind = "balance"
    fields = api_fields("env:LINKUP_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.linkup.so/v1/credits/balance", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        bal = fnum(j.get("balance"))
        if bal is None:
            raise ProviderError("Linkup returned no balance")
        return self.balance(Snapshot(), bal)


# --------------------------------------------------------------------------------------------------
# Spend + balance / quota vendors
# --------------------------------------------------------------------------------------------------

class DeepInfraProvider(ApiProvider):
    type = "deepinfra"
    default_name = "DeepInfra"
    vendor = "DeepInfra"
    plans = "Pay as you go"
    setup = "deepinfra.com → Dashboard → API keys → New API key, then paste it."
    key_url = "https://deepinfra.com/dash/api_keys"
    key_help = "env:DEEPINFRA_API_KEY works too."
    usage_url = "https://deepinfra.com/dash/usage"
    accent = "#2563eb"
    fields = api_fields("env:DEEPINFRA_API_KEY works too.", "spend")

    def fetch(self) -> Snapshot:
        h = {"Authorization": f"Bearer {self.key()}"}
        try:
            checklist = http_json("https://api.deepinfra.com/payment/checklist?compute_owed=true", h)
            usage = http_json("https://api.deepinfra.com/payment/usage?from=current", h)
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(checklist, usage)

    def parse(self, checklist: dict[str, Any], usage: dict[str, Any]) -> Snapshot:
        months = usage.get("months") if isinstance(usage.get("months"), list) else None
        stripe = fnum(checklist.get("stripe_balance"))
        if months is None or stripe is None:
            raise ProviderError("DeepInfra returned no billing data")
        recent = max(0.0, fnum(checklist.get("recent")) or 0.0)
        month_cost = max(0.0, (fnum((months[-1] or {}).get("total_cost")) or 0.0) / 100) if months else recent
        snap = self.spend(Snapshot(), month_cost)
        owed = stripe + recent  # positive = owed, negative = prepaid credit left
        snap.notes.append(f"Owed: {money(owed)}" if owed > 0 else f"Credit left: {money(-owed)}")
        if checklist.get("suspended"):
            snap.notes.append(f"Suspended: {checklist.get('suspend_reason') or 'see dashboard'}")
        return snap


class VeniceProvider(ApiProvider):
    type = "venice"
    default_name = "Venice API"
    vendor = "Venice.ai"
    plans = "DIEM allocation, USD balance"
    setup = "venice.ai → Settings → API → Generate new API key, then paste it."
    key_url = "https://venice.ai/settings/api"
    key_help = "env:VENICE_API_KEY works too."
    usage_url = "https://venice.ai/settings/api"
    accent = "#dc2626"
    budget_kind = "balance"
    fields = api_fields("env:VENICE_API_KEY works too.", "balance")

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.venice.ai/api/v1/billing/balance", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    def parse(self, j: dict[str, Any]) -> Snapshot:
        b = j.get("balances") if isinstance(j.get("balances"), dict) else None
        if b is None:
            raise ProviderError("Venice returned no balance")
        snap = Snapshot()
        diem, alloc = fnum(b.get("diem")), fnum(j.get("diemEpochAllocation"))
        if diem is not None and alloc:
            tomorrow = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                                                       microsecond=0)
            snap.windows.append(UsageWindow("DIEM (day)", pct_of(alloc - diem, alloc), tomorrow.timestamp(), 86400,
                                            f"{diem:,.2f} of {alloc:,.2f} left"))
        usd = fnum(b.get("usd"))
        if usd is not None:
            self.balance(snap, usd)
        if j.get("canConsume") is False:
            snap.notes.append("Out of credit")
        if not snap.windows and not snap.notes:
            raise ProviderError("Venice returned no balance")
        return snap


class TavilyProvider(ApiProvider):
    type = "tavily"
    default_name = "Tavily"
    vendor = "Tavily"
    plans = "Researcher, Project, Bootstrap… (API credits)"
    setup = "app.tavily.com → API Keys → copy (or create) a key, then paste it."
    key_url = "https://app.tavily.com/home"
    key_help = "tvly-… key. env:TAVILY_API_KEY works too."
    usage_url = "https://app.tavily.com/home"
    accent = "#3b82f6"
    fields = (Field("api_key", "API key", "secret", "tvly-… key. env:TAVILY_API_KEY works too."),)

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.tavily.com/usage", {"Authorization": f"Bearer {self.key()}"})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        acct = j.get("account") if isinstance(j.get("account"), dict) else None
        key = j.get("key") if isinstance(j.get("key"), dict) else {}
        if acct is None:
            raise ProviderError("Tavily returned no usage")
        snap = Snapshot(plan=title_case(acct.get("current_plan") or acct.get("plan") or ""))
        start, end = month_bounds()
        for label, used, limit in (("Credits (mo)", acct.get("plan_usage"), acct.get("plan_limit")),
                                   ("This key", key.get("usage"), key.get("limit")),
                                   ("Pay as you go", acct.get("paygo_usage"), acct.get("paygo_limit"))):
            u, lim = fnum(used), fnum(limit)
            if pct_of(u, lim) is not None:
                snap.windows.append(UsageWindow(label, pct_of(u, lim), end.timestamp() if label == "Credits (mo)" else None,
                                                (end - start).total_seconds() if label == "Credits (mo)" else None,
                                                f"{u:,.0f} of {lim:,.0f}"))
        if not snap.windows:
            raise ProviderError("Tavily returned no usage")
        return snap


class ElevenLabsProvider(ApiProvider):
    type = "elevenlabs"
    default_name = "ElevenLabs"
    vendor = "ElevenLabs"
    plans = "Free, Starter, Creator, Pro… (credits)"
    setup = ("elevenlabs.io → Developers → API Keys → Create key. Give it the User → Read permission "
             "(needed to read your subscription), then paste it.")
    key_url = "https://elevenlabs.io/app/developers/api-keys"
    key_help = "env:ELEVENLABS_API_KEY works too."
    usage_url = "https://elevenlabs.io/app/subscription"
    accent = "#e5e7eb"
    fields = (Field("api_key", "API key", "secret", "Needs User → Read. env:ELEVENLABS_API_KEY works too."),)

    def key_hint(self) -> str:
        return "Make sure it has the User → Read permission."

    def fetch(self) -> Snapshot:
        try:
            j = http_json("https://api.elevenlabs.io/v1/user/subscription", {"xi-api-key": self.key()})
        except HttpError as e:
            raise self.rejected(e) from None
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        used, limit = fnum(j.get("character_count")), fnum(j.get("character_limit"))
        if used is None or limit is None:
            raise ProviderError("ElevenLabs returned no subscription usage")
        snap = Snapshot(plan=title_case(j.get("tier") or ""))
        reset = parse_time(j.get("next_character_count_reset_unix"))
        snap.windows.append(UsageWindow("Credits (mo)", pct_of(used, limit) if limit else 0.0, reset,
                                        30 * 86400 if reset else None, f"{used:,.0f} of {limit:,.0f}"))
        if str(j.get("status") or "active").lower() not in ("active", "trialing"):
            snap.notes.append(f"Status: {j.get('status')}")
        return snap
