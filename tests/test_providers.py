"""Every built-in provider against canned responses (no network)."""
import json
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

from subusage.core import HttpError, NotConfigured, ProviderError
from subusage.providers import CATALOG, PROVIDERS, claude, codex, copilot, cursor, gemini, github, keyed, nous
from subusage.providers.command import CommandProvider
from subusage.providers.jetbrains import JetBrainsProvider

from .helpers import FakeHTTP, HomeTestCase, jwt


def labels(snap):
    return [w.label for w in snap.windows]


class RegistryTests(unittest.TestCase):
    def test_metadata(self):
        for t, cls in PROVIDERS.items():
            self.assertEqual(t, cls.type)
            self.assertTrue(cls.default_name and cls.setup and cls.accent, t)
            self.assertIn(cls.auth, ("auto", "key", "cookie", "api", "command"), t)
            for f in cls.fields:
                self.assertIn(f.kind, ("path", "secret", "text", "choice", "command", "device_login"), (t, f))
        names = {c.default_name.lower() for c in PROVIDERS.values()}
        for name, vendor, source in CATALOG:
            self.assertNotIn(name.lower(), names, "catalog entry is already built in")
            self.assertTrue(vendor and source)


class ClaudeTests(HomeTestCase):
    RESP = {"five_hour": {"utilization": 37.0, "resets_at": "2030-01-01T05:00:00.5+00:00"},
            "seven_day": {"utilization": 58.0, "resets_at": "2030-01-03T00:00:00+00:00"},
            "seven_day_opus": None, "seven_day_sonnet": {"utilization": 12.0, "resets_at": None},
            "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250, "utilization": None}}

    def creds(self, expires_in=3600):
        self.write(".claude/.credentials.json", {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-x", "expiresAt": int((self.now + expires_in) * 1000), "subscriptionType": "max"}})

    def test_windows(self):
        self.creds()
        http = FakeHTTP({"/api/oauth/usage": self.RESP})
        with mock.patch.object(claude, "http_json", http):
            s = claude.ClaudeProvider({}).fetch()
        self.assertEqual(s.plan, "Max")
        self.assertEqual(labels(s), ["Session (5h)", "Weekly", "Weekly · Sonnet", "Extra usage (mo)"])
        self.assertAlmostEqual(s.windows[3].used_pct, 25)
        h = http.last("/api/oauth/usage")["headers"]
        self.assertEqual(h["Authorization"], "Bearer sk-ant-oat01-x")
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")

    def test_expired(self):
        self.creds(-10)
        with mock.patch.object(claude, "http_json", FakeHTTP({"/api/oauth/usage": HttpError(401, "")})):
            with self.assertRaisesRegex(ProviderError, "expired"):
                claude.ClaudeProvider({}).fetch()

    def test_custom_path_and_missing(self):
        with self.assertRaises(NotConfigured):
            with mock.patch.object(claude, "IS_MAC", False):
                claude.ClaudeProvider({}).fetch()
        p = self.write("wsl/creds.json", {"claudeAiOauth": {"accessToken": "t"}})
        with mock.patch.object(claude, "http_json", FakeHTTP({"/api/oauth/usage": self.RESP})):
            self.assertTrue(claude.ClaudeProvider({"credentials_path": str(p)}).fetch().windows)

    def test_macos_keychain(self):
        item = json.dumps({"claudeAiOauth": {"accessToken": "kc", "subscriptionType": "pro"}})
        with mock.patch.object(claude, "IS_MAC", True), mock.patch.object(claude, "run_cmd", return_value=item) as rc, \
                mock.patch.object(claude, "http_json", FakeHTTP({"/api/oauth/usage": self.RESP})) as http:
            s = claude.ClaudeProvider({}).fetch()
        self.assertEqual(s.plan, "Pro")
        self.assertIn("Claude Code-credentials", rc.call_args[0][0])
        self.assertEqual(http.last("usage")["headers"]["Authorization"], "Bearer kc")


class CodexTests(HomeTestCase):
    def test_windows(self):
        tok = jwt({"exp": self.now + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}})
        self.write(".codex/auth.json", {"tokens": {"access_token": tok}})
        resp = {"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 91, "limit_window_seconds": 18000, "reset_after_seconds": 2820},
            "secondary_window": {"used_percent": 44, "limit_window_seconds": 604800, "reset_at": int(self.now + 9e4)}},
            "credits": {"has_credits": True, "balance": 12.5}}
        http = FakeHTTP({"wham/usage": resp})
        with mock.patch.object(codex, "http_json", http):
            s = codex.CodexProvider({}).fetch()
        self.assertEqual((s.plan, labels(s)), ("Plus", ["Session (5h)", "Weekly"]))
        self.assertEqual(s.notes, ["Credits: 12.50"])
        self.assertEqual(http.last("wham")["headers"]["ChatGPT-Account-ID"], "acct_1")


class NousTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.write(".hermes/auth.json", {"providers": {"nous": {"access_token": jwt({"exp": self.now + 99}),
                                                                "portal_base_url": "https://portal.example"}}})

    def test_gauges(self):
        resp = {"subscription": {"plan": "plus"}, "paid_service_access": {"purchased_credits_remaining": 4.2},
                "usage_window": {"gauges": [
                    {"id": "day", "used_bp": 2200, "resets_at_ms": int((self.now + 3600) * 1000)},
                    {"id": "week", "used_bp": 7100, "resets_at_ms": int((self.now + 86400) * 1000)},
                    {"id": "top_tier", "used_bp": 10000, "resets_at_ms": int((self.now + 9e5) * 1000)}]}}
        http = FakeHTTP({"portal.example/api/oauth/account": resp})
        with mock.patch.object(nous, "http_json", http):
            s = nous.NousProvider({}).fetch()
        self.assertEqual(labels(s), ["Today", "This week", "Large models (mo)"])
        self.assertEqual([w.used_pct for w in s.windows], [22, 71, 100])
        self.assertIn("Top-up credits: $4.20", s.notes)

    def test_legacy_credits(self):
        resp = {"subscription": {"plan": "pro", "monthly_credits": 20, "credits_remaining": 5,
                                 "current_period_end": "2030-01-01T00:00:00Z"}}
        with mock.patch.object(nous, "http_json", FakeHTTP({"/api/oauth/account": resp})):
            s = nous.NousProvider({}).fetch()
        self.assertEqual((labels(s), s.windows[0].used_pct), (["Monthly credits"], 75))


class CopilotTests(HomeTestCase):
    def test_paid_plan(self):
        resp = {"copilot_plan": "individual_pro", "quota_reset_date": "2030-01-01",
                "quota_snapshots": {"premium_interactions": {"entitlement": 300, "remaining": 108, "percent_remaining": 36},
                                    "chat": {"unlimited": True}, "completions": {"unlimited": True}}}
        http = FakeHTTP({"copilot_internal/user": resp})
        with mock.patch.object(copilot, "http_json", http):
            s = copilot.CopilotProvider({"token": "gho_x"}).fetch()
        self.assertEqual(s.plan, "Individual Pro")
        self.assertEqual((labels(s), s.windows[0].used_pct, s.windows[0].detail), (["Premium requests"], 64, "192 of 300"))
        self.assertEqual(http.last("copilot")["headers"]["Authorization"], "token gho_x")
        self.assertTrue(http.last("copilot")["url"].startswith("https://api.github.com/"))

    def test_free_plan_counts(self):
        s = copilot.CopilotProvider.parse({"access_type_sku": "free_limited_copilot", "limited_user_reset_date": "2030-01-01",
                                           "monthly_quotas": {"chat": 50, "completions": 2000},
                                           "limited_user_quotas": {"chat": 40, "completions": 1500}})
        self.assertEqual([(w.label, w.used_pct) for w in s.windows], [("Chat", 20), ("Completions", 25)])

    def test_gh_fallback_and_enterprise(self):
        with mock.patch.object(copilot, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(copilot, "run_cmd", return_value="gho_from_gh\n") as rc, \
                mock.patch.object(copilot, "http_json", FakeHTTP({"copilot_internal": HttpError(401, "")})):
            with self.assertRaisesRegex(ProviderError, "Sign in with GitHub"):
                copilot.CopilotProvider({"host": "https://octo.ghe.com/login"}).fetch()
        self.assertEqual(rc.call_args[0][0][-2:], ["--hostname", "https://octo.ghe.com/login"])
        self.assertEqual(copilot.CopilotProvider({"host": "octo.ghe.com"})._hosts()[1], "https://api.octo.ghe.com")

    def test_no_login(self):
        with mock.patch.object(copilot, "which", return_value=None):
            with self.assertRaises(NotConfigured):
                copilot.CopilotProvider({}).fetch()

    def test_device_flow(self):
        polls = iter([{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": "gho_new"}])
        http = FakeHTTP({"/login/device/code": {"device_code": "dc", "user_code": "ABCD-1234", "interval": 5,
                                                "verification_uri": "https://github.com/login/device"},
                         "/login/oauth/access_token": lambda *a: next(polls)})
        with mock.patch.object(copilot, "http_json", http):
            st = copilot.CopilotProvider.device_start({})
            self.assertEqual(st["user_code"], "ABCD-1234")
            self.assertIsNone(copilot.CopilotProvider.device_poll(st))
            self.assertIsNone(copilot.CopilotProvider.device_poll(st))
            self.assertEqual(st["interval"], 10)
            self.assertEqual(copilot.CopilotProvider.device_poll(st), "gho_new")
        self.assertEqual(http.last("device/code")["form"]["scope"], "read:user")


class GitHubTests(HomeTestCase):
    NOW = __import__("datetime").datetime(2026, 11, 16, tzinfo=__import__("datetime").timezone.utc)
    ITEMS = [
        {"product": "Actions", "sku": "actions_linux", "unitType": "minutes", "grossQuantity": 1000, "netAmount": 0},
        {"product": "Actions", "sku": "Actions Windows", "unitType": "minutes", "grossQuantity": 100, "netAmount": 0},
        {"product": "Actions", "sku": "actions_macos", "unitType": "minutes", "grossQuantity": 50, "netAmount": 0},
        {"product": "Actions", "sku": "Actions Linux 4-core", "unitType": "minutes", "grossQuantity": 999, "netAmount": 5.0},
        {"product": "Codespaces", "sku": "Codespaces compute 4 core", "unitType": "hours", "grossQuantity": 9, "netAmount": 0},
        {"product": "Codespaces", "sku": "codespaces_storage", "unitType": "gigabyte-hours", "grossQuantity": 7200,
         "netAmount": 0},
        {"product": "Copilot", "sku": "Copilot Premium Request", "unitType": "requests", "grossQuantity": 400,
         "netAmount": 4.0},
    ]
    PREMIUM = [{"product": "Copilot", "model": "GPT-5", "grossQuantity": 400, "discountQuantity": 300,
                "netQuantity": 100, "netAmount": 4.0}]

    def test_parse(self):
        s = github.GitHubProvider.parse(self.ITEMS, self.PREMIUM, "pro", self.NOW)
        self.assertEqual(s.plan, "Pro")
        w = {x.label: x for x in s.windows}
        # 1000 + 100×2 + 50×10 = 1700 of 3000 (the 4-core larger runner doesn't use included minutes)
        self.assertEqual(w["Actions minutes"].detail, "1,700 of 3,000 min")
        self.assertAlmostEqual(w["Actions minutes"].used_pct, 1700 / 3000 * 100)
        self.assertEqual(w["Codespaces compute"].detail, "36.0 of 180 core-h")     # 9 h × 4 cores
        self.assertEqual(w["Codespaces storage"].detail, "10.0 of 20 GB-mo")      # 7200 GB-h / 720 h in Nov
        self.assertEqual(w["Actions minutes"].length_s, 30 * 86400)
        self.assertIn("Copilot requests beyond plan: 100 ($4.00)", s.notes)
        self.assertIn("Billed beyond plan this month: $9.00", s.notes)

    def test_free_plan_no_codespaces(self):
        s = github.GitHubProvider.parse(self.ITEMS[:1], [], "free", self.NOW)
        self.assertEqual([x.label for x in s.windows], ["Actions minutes"])
        self.assertEqual(s.windows[0].detail, "1,000 of 2,000 min")
        self.assertEqual(s.notes, ["Nothing billed beyond your plan this month"])

    def test_fetch_summary_and_plan_detection(self):
        github.GitHubProvider._me.clear()
        http = FakeHTTP({"/users/octo/settings/billing/usage/summary": {"usageItems": self.ITEMS[:1]},
                         "/users/octo/settings/billing/premium_request/usage": {"usageItems": []},
                         "api.github.com/user": {"login": "octo", "plan": {"name": "pro"}}})  # last: prefix of /users
        with mock.patch.object(github, "http_json", http):
            s = github.GitHubProvider({"token": "github_pat_x"}).fetch()
        self.assertEqual((s.plan, s.windows[0].detail), ("Pro", "1,000 of 3,000 min"))
        self.assertEqual(http.last("summary")["headers"]["Authorization"], "Bearer github_pat_x")
        self.assertRegex(http.last("summary")["url"], r"year=\d{4}&month=\d{1,2}$")

    def test_fallback_to_usage_report(self):
        github.GitHubProvider._me.clear()
        http = FakeHTTP({"/usage/summary": HttpError(404, ""),
                         "/settings/billing/usage?": {"usageItems": [
                             {"date": "2026-11-01", "product": "Actions", "sku": "Actions Linux", "quantity": 30,
                              "unitType": "minutes"}]},
                         "/premium_request/usage": HttpError(404, ""),
                         "api.github.com/user": {"login": "octo"}})
        with mock.patch.object(github, "http_json", http):
            s = github.GitHubProvider({"token": "t", "plan": "free"}).fetch()
        self.assertEqual(s.windows[0].detail, "30 of 2,000 min")

    def test_token_errors(self):
        github.GitHubProvider._me.clear()
        http = FakeHTTP({"/usage/summary": HttpError(403, ""), "api.github.com/user": {"login": "octo"}})
        with mock.patch.object(github, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(github, "run_cmd", return_value="gho_cli"), mock.patch.object(github, "http_json", http):
            with self.assertRaisesRegex(ProviderError, "Plan: Read-only.*GitHub CLI"):
                github.GitHubProvider({}).fetch()
        with mock.patch.object(github, "which", return_value=None):
            with self.assertRaises(NotConfigured):
                github.GitHubProvider({}).fetch()
        self.assertFalse(github.GitHubProvider({}).parks_on_auth)


class RenameMigrationTests(HomeTestCase):
    def test_copilot_renamed_with_history(self):
        from subusage import core, history
        from subusage.core import Snapshot, UsageWindow
        history.record("copilot:Copilot", Snapshot(windows=[UsageWindow("Premium requests", 10)], fetched_at=self.now))
        core.save_config({"providers": [{"type": "copilot", "name": "Copilot"}, {"type": "copilot", "name": "Work"}]})
        cfg = core.load_config()
        self.assertEqual([p["name"] for p in cfg["providers"]], ["GitHub Copilot", "Work"])   # custom names kept
        self.assertEqual(json.loads(core.config_path().read_text())["providers"][0]["name"], "GitHub Copilot")
        self.assertEqual(len(history.series("copilot:GitHub Copilot", "Premium requests", 0)), 1)
        self.assertEqual(history.series("copilot:Copilot", "Premium requests", 0), [])


class GeminiTests(HomeTestCase):
    QUOTA = {"buckets": [
        {"modelId": "gemini-2.5-pro", "remainingFraction": 0.78, "resetTime": "2030-01-01T00:00:00Z"},
        {"modelId": "gemini-2.5-pro-preview", "remainingFraction": 0.6, "resetTime": "2030-01-01T00:00:00Z"},
        {"modelId": "gemini-2.5-flash", "remainingFraction": 0.95, "resetTime": "2030-01-01T00:00:00Z"},
        {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 1.0}]}

    def setUp(self):
        super().setUp()
        gemini.GeminiProvider._cache.clear()
        gemini.GeminiProvider._project.clear()

    def test_valid_token(self):
        self.write(".gemini/oauth_creds.json", {"access_token": "ya29.ok", "refresh_token": "1//r",
                                                "expiry_date": int((self.now + 1800) * 1000)})
        http = FakeHTTP({":loadCodeAssist": {"cloudaicompanionProject": "proj-1", "currentTier": {"name": "Gemini Code Assist"}},
                         ":retrieveUserQuota": self.QUOTA})
        with mock.patch.object(gemini, "http_json", http):
            s = gemini.GeminiProvider({}).fetch()
        self.assertEqual(labels(s), ["Pro (daily)", "Flash (daily)", "Flash Lite (daily)"])
        self.assertAlmostEqual(s.windows[0].used_pct, 40)  # worst Pro bucket wins
        self.assertEqual(http.last("retrieveUserQuota")["json"], {"project": "proj-1"})
        self.assertEqual(s.plan, "Code Assist")

    def test_refresh_in_memory(self):
        p = self.write(".gemini/oauth_creds.json", {"access_token": "old", "refresh_token": "1//r",
                                                    "expiry_date": int((self.now - 10) * 1000)})
        before = p.read_text()
        os.environ["GEMINI_OAUTH_CLIENT_ID"], os.environ["GEMINI_OAUTH_CLIENT_SECRET"] = "cid", "sec"
        http = FakeHTTP({"oauth2.googleapis.com/token": {"access_token": "new", "expires_in": 3599},
                         ":loadCodeAssist": {}, ":retrieveUserQuota": self.QUOTA})
        with mock.patch.object(gemini, "http_json", http):
            gemini.GeminiProvider({}).fetch()
            gemini.GeminiProvider({}).fetch()
        self.assertEqual(len([c for c in http.calls if "oauth2" in c["url"]]), 1)  # cached
        self.assertEqual(http.last("oauth2")["form"]["client_id"], "cid")
        self.assertEqual(http.last("retrieveUserQuota")["headers"]["Authorization"], "Bearer new")
        self.assertEqual(p.read_text(), before)  # never written back

    def test_client_from_installed_cli(self):
        root = self.home / "npm"
        f = root / "@google/gemini-cli/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js"
        f.parent.mkdir(parents=True)
        f.write_text("const OAUTH_CLIENT_ID = 'abc.apps.googleusercontent.com';\nconst OAUTH_CLIENT_SECRET = \"GOCSPX-x\";")
        with mock.patch.object(gemini, "_npm_roots", return_value=[root]):
            self.assertEqual(gemini.find_client_credentials(), ("abc.apps.googleusercontent.com", "GOCSPX-x"))


class CursorTests(HomeTestCase):
    RESP = {"membershipType": "pro", "billingCycleStart": "2029-12-02T00:00:00Z", "billingCycleEnd": "2030-01-01T00:00:00Z",
            "individualUsage": {"plan": {"enabled": True, "used": 1240, "limit": 2000, "totalPercentUsed": 62,
                                         "autoPercentUsed": 40, "apiPercentUsed": 22},
                                "onDemand": {"enabled": True, "used": 500, "limit": 2000}}}

    def db(self, token):
        base = {"win32": "AppData", "darwin": "Library/Application Support"}.get(sys.platform, ".config")
        p = self.home / base / "Cursor/User/globalStorage/state.vscdb"
        p.parent.mkdir(parents=True)
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (token,))
        con.commit()
        con.close()
        return p

    def test_app_token(self):
        self.db(jwt({"sub": "auth0|user_123", "exp": self.now + 3600}))
        http = FakeHTTP({"api/usage-summary": self.RESP})
        with mock.patch.object(cursor, "http_json", http):
            s = cursor.CursorProvider({}).fetch()
        self.assertEqual(s.plan, "Pro")
        self.assertEqual(labels(s), ["Included usage", "· Auto", "· API models", "On-demand"])
        self.assertEqual(s.windows[0].detail, "$12.40 of $20.00")
        self.assertTrue(http.last("usage-summary")["headers"]["Cookie"].startswith("WorkosCursorSessionToken=user_123%3A%3A"))

    def test_encrypted_and_manual(self):
        self.db("cursor-safe-storage:v1:abcd")
        with self.assertRaisesRegex(ProviderError, "encrypted"):
            cursor.CursorProvider({}).fetch()
        http = FakeHTTP({"api/usage-summary": self.RESP})
        with mock.patch.object(cursor, "http_json", http):
            cursor.CursorProvider({"session_token": "abc"}).fetch()
        self.assertEqual(http.last("usage")["headers"]["Cookie"], "WorkosCursorSessionToken=abc")

    def test_utf16_blob(self):
        tok = jwt({"sub": "u9", "exp": self.now + 3600})
        self.assertEqual(cursor.read_app_token(self.db(tok.encode("utf-16-le"))), tok)


class JetBrainsTests(HomeTestCase):
    def test_quota_file(self):
        quota = {"type": "Available", "current": "1", "maximum": "2",
                 "tariffQuota": {"current": "250000", "maximum": "1000000", "available": "750000"},
                 "topUpQuota": {"available": "5490000"}, "until": "2030-02-01T00:00:00Z"}
        refill = {"type": "Known", "next": "2030-01-01T00:00:00Z", "tariff": {"amount": "1000000", "duration": "PT720H"}}
        esc = lambda d: json.dumps(d).replace('"', "&quot;")  # noqa: E731
        base = {"win32": "AppData", "darwin": "Library/Application Support"}.get(sys.platform, ".config")
        self.write(f"{base}/JetBrains/PyCharm2026.2/options/AIAssistantQuotaManager2.xml",
                   f'<application><component name="AIAssistantQuotaManager2">'
                   f'<option name="quotaInfo" value="{esc(quota)}" /><option name="nextRefill" value="{esc(refill)}" />'
                   f'</component></application>')
        s = JetBrainsProvider({}).fetch()
        w = s.windows[0]
        self.assertEqual((w.label, w.used_pct, w.detail, w.length_s), ("Monthly credits", 25, "2.50 of 10.00", 720 * 3600))
        self.assertEqual((s.plan, s.notes), ("via PyCharm", ["Top-up credits: 54.90"]))

    def test_missing(self):
        with self.assertRaises(NotConfigured):
            JetBrainsProvider({}).fetch()


class KeyedTests(HomeTestCase):
    def test_zai(self):
        resp = {"success": True, "code": 200, "data": {"planName": "pro", "limits": [
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 3, "usage": 1000, "currentValue": 30},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 20, "nextResetTime": int((self.now + 9e4) * 1000)},
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 45, "nextResetTime": int((self.now + 600) * 1000)}]}}
        http = FakeHTTP({"api.z.ai/api/monitor/usage/quota/limit": resp})
        with mock.patch.object(keyed, "http_json", http):
            s = keyed.ZaiProvider({"api_key": "k"}).fetch()
        self.assertEqual(labels(s), ["Session (5h)", "Weekly", "MCP calls (mo)"])
        self.assertEqual([w.used_pct for w in s.windows], [45, 20, 3])
        with mock.patch.object(keyed, "http_json", FakeHTTP({"open.bigmodel.cn": resp})):
            keyed.ZaiProvider({"api_key": "k", "region": "china"}).fetch()
        with self.assertRaises(NotConfigured):
            keyed.ZaiProvider({}).fetch()

    def test_kimi(self):
        resp = {"user": {"membership": {"level": "LEVEL_INTERMEDIATE"}}, "version": "GOODS_VERSION_V1",
                "usages": {"limit_5h": {"used_ratio": 0.31, "reset_time": "2030-01-01T00:00:00Z"},
                           "limit_7d": {"used_ratio": 0.5}, "limit_month_total": {"used_ratio": 1.2}}}
        http = FakeHTTP({"api.kimi.ai/coding/v1/usages": resp})
        with mock.patch.object(keyed, "http_json", http):
            s = keyed.KimiProvider({"api_key": "k", "region": "global"}).fetch()
        self.assertEqual((s.plan, [round(w.used_pct) for w in s.windows]), ("Allegretto", [31, 50, 100]))

    def test_synthetic(self):
        resp = {"data": {"planWeeklyLimit": {"percentRemaining": 70, "nextRegenAt": "2030-01-01T00:00:00Z"},
                         "search": {"hourly": {"limit": 100, "requests": 25}}, "subscription": {"limit": 1, "requests": 1}}}
        with mock.patch.object(keyed, "http_json", FakeHTTP({"api.synthetic.new/v2/quotas": resp})):
            s = keyed.SyntheticProvider({"api_key": "k"}).fetch()
        self.assertEqual([(w.label, round(w.used_pct)) for w in s.windows], [("Weekly", 30), ("Search (hourly)", 25)])

    def test_clinepass_file_login(self):
        self.write(".cline/data/settings/providers.json",
                   {"providers": {"cline": {"settings": {"auth": {"accessToken": "abc"}}}}})
        resp = {"success": True, "data": {"limits": [{"type": "five_hour", "percentUsed": 12.5, "resetsAt": "2030-01-01T00:00:00Z"},
                                                     {"type": "weekly", "percentUsed": 40}, {"type": "unknown", "percentUsed": 1}]}}
        http = FakeHTTP({"api.cline.bot": resp})
        with mock.patch.object(keyed, "http_json", http):
            s = keyed.ClinePassProvider({}).fetch()
        self.assertEqual(labels(s), ["Session (5h)", "Weekly"])
        self.assertEqual(http.last("cline")["headers"]["Authorization"], "Bearer workos:abc")


class CommandTests(HomeTestCase):
    def test_script(self):
        script = self.write("prov.py", 'import json;print(json.dumps({"plan":"Pro","windows":[{"label":"Daily",'
                                       '"used_pct":42.5,"resets_at":"2030-01-01T00:00:00Z","length_seconds":86400}],"notes":["hi"]}))')
        s = CommandProvider({"command": f'"{sys.executable}" "{script}"'}).fetch()
        self.assertEqual((s.plan, s.windows[0].used_pct, s.notes), ("Pro", 42.5, ["hi"]))

    def test_failure(self):
        script = self.write("bad.py", "import sys; sys.stderr.write('nope'); sys.exit(3)")
        with self.assertRaisesRegex(ProviderError, "nope"):
            CommandProvider({"command": f'"{sys.executable}" "{script}"'}).fetch()

    def test_example_script_runs(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ex = os.path.join(root, "examples", "custom_provider.py")
        s = CommandProvider({"command": f'"{sys.executable}" "{ex}" --demo'}).fetch()
        self.assertTrue(s.windows)


if __name__ == "__main__":
    unittest.main()
