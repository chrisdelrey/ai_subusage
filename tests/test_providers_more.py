"""The providers added in 2.6 against canned responses (no network), plus cookie-paste parsing."""
import json
import sqlite3
from unittest import mock

from subusage.core import AuthExpired, HttpError, NotConfigured, ProviderError, parse_cookie_input, pick_cookies
from subusage.providers import PROVIDERS, apikeys, local, web

from .helpers import FakeHTTP, HomeTestCase


def labels(snap):
    return [w.label for w in snap.windows]


class CookieInputTests(HomeTestCase):
    def test_forms(self):
        self.assertEqual(parse_cookie_input("Cookie: a=1; b=2"), ("a=1; b=2", {}))
        self.assertEqual(parse_cookie_input("  a=1  "), ("a=1", {}))
        curl = ("curl 'https://admin.mistral.ai/api/x' \\\n  -H 'accept: */*' \\\n"
                "  -H 'cookie: ory_session_abc=S; csrftoken=C; _ga=G' \\\n  -H 'x-csrftoken: C' --compressed")
        cookie, extra = parse_cookie_input(curl)
        self.assertEqual(cookie, "ory_session_abc=S; csrftoken=C; _ga=G")
        self.assertEqual(extra, {"x-csrftoken": "C"})
        self.assertEqual(parse_cookie_input("curl https://x -b 'k=v'")[0], "k=v")

    def test_pick(self):
        h = "token_v2=T; _ga=G; notion_user_id=U"
        self.assertEqual(pick_cookies(h, ("token_v2", "notion_user_id")), "token_v2=T; notion_user_id=U")
        self.assertEqual(pick_cookies("BARE", ("token_v2",), bare_name="token_v2"), "token_v2=BARE")
        self.assertEqual(pick_cookies("ory_session_x=1; other=2", (), ("ory_session_",)), "ory_session_x=1")

    def test_only_needed_cookies_are_sent(self):
        http = FakeHTTP({"current_user/ai_credits": {"remaining_balance_credits": 10, "total_balance_credits": 100}})
        p = PROVIDERS["raycast"]({"cookie": "Cookie: __raycast_session=S; _ga=G; csrf_token=C"})
        with mock.patch.object(web, "http_json", http):
            p.fetch()
        self.assertEqual(http.calls[0]["headers"]["Cookie"], "__raycast_session=S; csrf_token=C")

    def test_missing_cookie(self):
        with self.assertRaises(NotConfigured):
            PROVIDERS["notion"]({}).fetch()
        with self.assertRaisesRegex(NotConfigured, "session-token"):
            PROVIDERS["perplexity"]({"cookie": "_ga=1; other=2"}).fetch()

    def test_expired_session_is_shown_not_parked(self):
        p = PROVIDERS["raycast"]({"cookie": "__raycast_session=S"})
        with mock.patch.object(web, "http_json", FakeHTTP({"ai_credits": HttpError(401, "")})):
            p.run_fetch()
        self.assertFalse(p.parked)
        self.assertIn("session expired", p.snapshot.error)


class NotionTests(HomeTestCase):
    SPACES = {"u1": {"notion_user": {"u1": {"value": {"value": {"id": "u1", "email": "c@x"}}}},
                     "space": {"s-personal": {"value": {"value": {"id": "s-personal", "name": "Me",
                                                                  "subscription_tier": "plus"}}},
                               "s-biz": {"value": {"value": {"id": "s-biz", "name": "Delrey",
                                                             "subscription_tier": "business"}}}}}}
    USAGE = {"status": "not_applicable", "allowances": [
        {"bucketId": "auto", "cadence": "billing_period", "state": "resolved", "used": 25, "limit": 100,
         "credits": {"used": 250, "limit": 1000}, "periodStartMs": 1764547200000, "periodEndMs": 1767225600000},
        {"bucketId": "flex", "cadence": "billing_period", "state": "resolved", "used": 10, "limit": 100}]}

    def test_fetch(self):
        http = FakeHTTP({"getSpaces": self.SPACES, "getCreditRateLimitStatus": self.USAGE})
        p = PROVIDERS["notion"]({"cookie": "token_v2=T; _ga=G"})
        with mock.patch.object(web, "http_json", http):
            s = p.fetch()
        self.assertEqual(http.last("getCreditRateLimitStatus")["json"], {"spaceId": "s-biz"})
        self.assertEqual(http.calls[0]["headers"]["Cookie"], "token_v2=T")
        self.assertEqual(s.plan, "Business")
        self.assertEqual(labels(s), ["Standard credits", "Select credits"])
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertEqual(s.windows[0].detail, "750 of 1,000 left")

    def test_legacy_and_not_applicable(self):
        s = web.NotionProvider.parse({"window": {"used": 3, "limit": 10, "window": "6h"}, "resetsInSeconds": 600,
                                      "billingPeriodWindow": {"used": 50, "limit": 200, "periodEndMs": 1767225600000}})
        self.assertEqual(labels(s), ["Rolling (6h)", "Monthly"])
        self.assertEqual(s.windows[0].length_s, 6 * 3600)
        with self.assertRaisesRegex(ProviderError, "Business"):
            web.NotionProvider.parse({"status": "not_applicable"})

    def test_falls_back_to_notion_so(self):
        http = FakeHTTP({"app.notion.com": HttpError(404, ""), "www.notion.so/api/v3/getSpaces": self.SPACES,
                         "www.notion.so/api/v3/getCreditRateLimitStatus": self.USAGE})
        with mock.patch.object(web, "http_json", http):
            s = PROVIDERS["notion"]({"cookie": "T"}).fetch()
        self.assertEqual(len(s.windows), 2)


class MistralTests(HomeTestCase):
    PAGE = ('<script>self.__next_f.push([1,"5:{\\"vibe_budget\\":{\\"usage_percentage\\":42.5,'
            '\\"initial_budget\\":20,\\"currency\\":\\"EUR\\",\\"reset_at\\":\\"2030-01-01T00:00:00Z\\"},'
            '\\"api_budget\\":{\\"usage_percentage\\":10,\\"initial_budget\\":5,\\"currency\\":\\"EUR\\",'
            '\\"reset_at\\":null}}"])</script>')

    def test_budgets_from_page(self):
        http = FakeHTTP({"admin.mistral.ai/subscription": self.PAGE})
        p = PROVIDERS["mistral"]({"cookie": "ory_session_x=S; csrftoken=C; _ga=G"})
        with mock.patch.object(web, "http_text", http), mock.patch.object(web, "http_json", FakeHTTP({})):
            s = p.fetch()
        self.assertEqual(labels(s), ["Vibe (mo)", "API budget (mo)"])
        self.assertAlmostEqual(s.windows[0].used_pct, 42.5)
        self.assertEqual(s.windows[0].detail, "€8.50 of €20.00")
        self.assertEqual(http.calls[0]["kw"].get("follow_redirects"), False)

    def test_vibe_fallback(self):
        vibe = [{"result": {"data": {"json": {"usage_percentage": 12, "reset_at": "2030-01-01T00:00:00Z"}}}}]
        jh = FakeHTTP({"billing.vibeUsage": vibe})
        p = PROVIDERS["mistral"]({"cookie": "ory_session_x=S; csrftoken=C"})
        with mock.patch.object(web, "http_text", FakeHTTP({"subscription": "<html></html>"})), \
                mock.patch.object(web, "http_json", jh):
            s = p.fetch()
        self.assertEqual(labels(s), ["Vibe (mo)"])
        self.assertEqual(jh.calls[0]["headers"]["X-CSRFToken"], "C")
        self.assertEqual(jh.calls[0]["headers"]["Cookie"], "ory_session_x=S; csrftoken=C")

    def test_login_redirect_is_expired(self):
        p = PROVIDERS["mistral"]({"cookie": "ory_session_x=S; csrftoken=C"})
        with mock.patch.object(web, "http_text", FakeHTTP({"subscription": HttpError(302, "")})):
            with self.assertRaises(AuthExpired):
                p.fetch()


class WebParseTests(HomeTestCase):
    def test_perplexity(self):
        s = web.PerplexityProvider.parse({"balance_cents": 1500, "renewal_date_ts": 1893456000, "total_usage_cents": 250,
                                          "credit_grants": [{"type": "recurring", "amount_cents": 1000},
                                                            {"type": "promotional", "amount_cents": 500}]})
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertIn("Balance: $15.00", s.notes)

    def test_t3(self):
        body = '{"json":{"0":[[0],[null,0,0]]}}\n' + json.dumps({"json": [2, 0, [[{
            "usageFourHourPercentage": 30, "usageMonthPercentage": 5, "usageFourHourNextResetAt": 1893456000000,
            "usageBand": "normal", "subscription": {"productName": "pro", "currentPeriodEnd": 1893456000}}]]]})
        s = web.T3ChatProvider.parse(body)
        self.assertEqual(s.plan, "Pro")
        self.assertEqual(labels(s), ["Base (4h)", "Overage (mo)"])
        self.assertEqual(s.windows[0].resets_at, 1893456000)

    def test_manus(self):
        s = web.ManusProvider.parse({"totalCredits": 1500, "proMonthlyCredits": 1900, "periodicCredits": 950,
                                     "refreshCredits": 100, "maxRefreshCredits": 300, "refreshInterval": "DAILY",
                                     "nextRefreshTime": "2030-01-01T00:00:00Z"})
        self.assertEqual(labels(s), ["Monthly credits", "Daily credits"])
        self.assertAlmostEqual(s.windows[0].used_pct, 50)

    def test_ollama(self):
        html = ('<span>Cloud Usage</span><span class="x">pro</span>'
                '<div><span>Session usage</span><div>12.5% used</div><time data-time="2030-01-01T00:00:00Z"></time></div>'
                '<div><span>Weekly usage</span><div style="width: 40%"></div></div>')
        s = web.OllamaProvider.parse(html)
        self.assertEqual(s.plan, "Pro")
        self.assertEqual(labels(s), ["Session", "Weekly"])
        self.assertEqual([w.used_pct for w in s.windows], [12.5, 40.0])
        with self.assertRaises(AuthExpired):
            web.OllamaProvider.parse('<a href="/signin">Sign in</a>')

    def test_augment(self):
        s = web.AugmentProvider.parse({"usageUnitsRemaining": 300, "usageUnitsConsumedThisBillingCycle": 100},
                                      {"planName": "Developer", "billingPeriodEnd": "2030-01-01T00:00:00Z"})
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertEqual(s.plan, "Developer")

    def test_zed(self):
        web_j = {"plan": "zed_pro", "current_usage": {"edit_predictions": {"used": 50, "limit": 2000},
                                                      "token_spend": {"spend_in_cents": 500, "limit_in_cents": 2000}}}
        s = web.ZedProvider.parse(web_j, web=True)
        self.assertEqual(s.plan, "Zed Pro")
        self.assertEqual(labels(s), ["Edit predictions", "Token spend"])
        ed = {"user": {"id": 1, "github_login": "c"}, "plan": {"plan_v3": "zed_free", "has_overdue_invoices": False,
              "usage": {"edit_predictions": {"used": 10, "limit": {"limited": 2000}}},
              "subscription_period": {"started_at": "2029-12-01T00:00:00Z", "ended_at": "2030-01-01T00:00:00Z"}}}
        s = web.ZedProvider.parse(ed, web=False)
        self.assertAlmostEqual(s.windows[0].used_pct, 0.5)
        self.assertEqual(s.windows[0].length_s, 31 * 86400)

    def test_devin(self):
        curl = ("curl 'https://app.devin.ai/api/org-abc/billing/quota/usage' -H 'authorization: Bearer TOK' "
                "-H 'x-cog-org-id: org-abc'")
        http = FakeHTTP({"/api/org-abc/billing/quota/usage": {"daily_percentage": 0.25, "weekly_percentage": 40,
                                                               "weekly_reset_at": "2030-01-01T00:00:00Z"}})
        with mock.patch.object(web, "http_json", http):
            s = PROVIDERS["devin"]({"cookie": curl}).fetch()
        self.assertEqual([w.used_pct for w in s.windows], [25.0, 40.0])
        h = http.calls[0]["headers"]
        self.assertEqual((h["Authorization"], h["x-cog-org-id"]), ("Bearer TOK", "org-abc"))


class KeyProviderTests(HomeTestCase):
    def test_warp(self):
        j = {"data": {"user": {"__typename": "UserOutput", "user": {
            "requestLimitInfo": {"isUnlimited": False, "nextRefreshTime": "2030-01-01T00:00:00Z", "requestLimit": 1500,
                                 "requestsUsedSinceLastRefresh": 300},
            "bonusGrants": [{"requestCreditsGranted": 100, "requestCreditsRemaining": 50}], "workspaces": []}}}}
        http = FakeHTTP({"graphql/v2": j})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["warp"]({"api_key": "wk"}).fetch()
        self.assertEqual(labels(s), ["Credits (mo)", "Bonus credits"])
        self.assertAlmostEqual(s.windows[0].used_pct, 20)
        self.assertEqual(http.calls[0]["headers"]["x-warp-client-id"], "warp-app")
        self.assertEqual(http.calls[0]["json"]["operationName"], "GetRequestLimitInfo")
        with self.assertRaisesRegex(ProviderError, "bad key"):
            apikeys.WarpProvider.parse({"errors": [{"message": "bad key"}]})

    def test_poe(self):
        self.assertEqual(apikeys.PoeProvider.parse({"current_point_balance": 2500}).notes, ["2,500 points left"])
        s = apikeys.PoeProvider.parse({"current_point_balance": 250000}, 1000000)
        self.assertAlmostEqual(s.windows[0].used_pct, 75)

    def test_chutes(self):
        s = apikeys.ChutesProvider.parse({"plan": "pro", "rolling4h": {"used": 50, "limit": 200},
                                          "monthly": {"used": 1000, "limit": 4000, "reset_at": "2030-01-01T00:00:00Z"}})
        self.assertEqual(labels(s), ["Rolling (4h)", "Monthly"])
        self.assertEqual(s.plan, "Pro")

    def test_gitkraken(self):
        s = apikeys.GitKrakenProvider.parse({"data": {"used": 30, "limit": 100, "resetsOn": "2030-01-01T00:00:00Z",
                                                      "organization": {"used": 400, "limit": 1000}}})
        self.assertEqual(labels(s), ["Weekly", "Org pool (wk)"])

    def test_minimax(self):
        j = {"base_resp": {"status_code": 0}, "model_remains": [{
            "model_name": "MiniMax-M2", "current_interval_total_count": 1500, "current_interval_usage_count": 1200,
            "end_time": 1893456000000, "current_weekly_total_count": 15000, "current_weekly_usage_count": 7500}]}
        http = FakeHTTP({"/v1/token_plan/remains": HttpError(404, ""), "coding_plan/remains": j})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["minimax"]({"api_key": "k"}).fetch()
        self.assertEqual(labels(s), ["Session (5h)", "Weekly"])
        self.assertAlmostEqual(s.windows[0].used_pct, 20)  # usage_count is what's *left*
        self.assertAlmostEqual(s.windows[1].used_pct, 50)
        with self.assertRaisesRegex(ProviderError, "invalid"):
            apikeys.MiniMaxProvider.parse({"base_resp": {"status_code": 1004, "status_msg": "invalid key"}})

    def test_alibaba(self):
        j = {"data": {"DataV2": {"data": {"data": {"codingPlanInstanceInfos": [{"planName": "Pro",
             "codingPlanQuotaInfo": {"per5HourUsedQuota": 100, "per5HourTotalQuota": 1200,
                                     "perWeekUsedQuota": 900, "perWeekTotalQuota": 9000,
                                     "perBillMonthUsedQuota": 0, "perBillMonthTotalQuota": 18000}}]}}}}}
        http = FakeHTTP({"queryCodingPlanInstanceInfoV2": j})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["alibaba"]({"api_key": "sk-sp-1"}).fetch()
        self.assertEqual(labels(s), ["Session (5h)", "Weekly", "Monthly"])
        self.assertEqual(s.plan, "Pro")
        self.assertIn("currentRegionId=ap-southeast-1", http.calls[0]["url"])
        self.assertEqual(http.calls[0]["headers"]["X-DashScope-API-Key"], "sk-sp-1")

    def test_factory_env_file_and_limits(self):
        self.write(".factory/.env", "# droid\nexport FACTORY_API_KEY='fk-123'\n")
        limits = {"usesTokenRateLimitsBilling": True, "limits": {"standard": {
            "fiveHour": {"usedPercent": 12, "secondsRemaining": 3600},
            "weekly": {"usedPercent": 40, "windowEnd": "2030-01-01T00:00:00Z"},
            "monthly": {"usedPercent": 5}}}}
        http = FakeHTTP({"billing/limits": limits})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["factory"]({}).fetch()
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer fk-123")
        self.assertEqual(labels(s), ["Session (5h)", "Weekly", "Monthly"])

    def test_factory_token_fallback(self):
        usage = {"usage": {"startDate": 1767225600000, "endDate": 1769904000000,
                           "standard": {"userTokens": 5, "totalAllowance": 20000000, "usedRatio": 0.3}}}
        http = FakeHTTP({"billing/limits": {"limits": None}, "subscription/usage": usage})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["factory"]({"api_key": "k"}).fetch()
        self.assertAlmostEqual(s.windows[0].used_pct, 30)

    def test_opencode_go_login_file(self):
        self.write(".local/share/opencode/auth.json", {"opencode-go": {"type": "api", "key": "oc-k"}})
        j = {"usage": {"rolling": {"usagePercent": 15, "resetInSec": 600}, "weekly": {"used": 1, "limit": 4},
                       "monthly": {"usagePercent": 2, "resetsAt": "2030-01-01T00:00:00Z"}}}
        http = FakeHTTP({"zen/go/v1/usage": j})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["opencodego"]({}).fetch()
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer oc-k")
        self.assertEqual([w.used_pct for w in s.windows], [15, 25, 2])

    def test_login_providers_park_when_expired(self):
        self.write(".local/share/kilo/auth.json", {"kilo": {"access": "kt"}})
        p = PROVIDERS["kilo"]({})
        with mock.patch.object(apikeys, "http_json", FakeHTTP({"trpc": HttpError(401, "")})):
            p.run_fetch()
        self.assertTrue(p.parked)
        p2 = PROVIDERS["kilo"]({"api_key": "pasted"})
        with mock.patch.object(apikeys, "http_json", FakeHTTP({"trpc": HttpError(401, "")})):
            p2.run_fetch()
        self.assertFalse(p2.parked)
        self.assertIn("rejected", p2.snapshot.error)

    def test_kilo(self):
        j = [{"result": {"data": {"json": {"subscription": {"tier": "pro", "currentPeriodUsageUsd": 12.5,
                                                            "currentPeriodBaseCreditsUsd": 40,
                                                            "currentPeriodBonusCreditsUsd": 10,
                                                            "nextBillingAt": "2030-01-01T00:00:00Z"}}}}},
             {"result": {"data": {"json": {"totalBalance_mUsd": 5000, "creditBlocks": []}}}}]
        s = apikeys.KiloProvider.parse(j)
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertIn("Credit balance: $5.00", s.notes)

    def test_codebuff(self):
        self.write(".config/manicode/credentials.json", {"default": {"authToken": "cb"}})
        http = FakeHTTP({"api/v1/usage": {"usage": 200, "quota": 1000, "next_quota_reset": "2030-01-01T00:00:00Z"},
                         "user/subscription": {"subscription": {"displayName": "Pro"},
                                               "rateLimit": {"weeklyUsed": 3, "weeklyLimit": 10}}})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["codebuff"]({}).fetch()
        self.assertEqual(s.plan, "Pro")
        self.assertEqual(labels(s), ["Weekly", "Credits"])
        self.assertEqual(http.last("api/v1/usage")["headers"]["Authorization"], "Bearer cb")

    def test_amp(self):
        text = ("Signed in as c@x (Chris)\n**Amp Free:** $6.50/$10 remaining (replenishes +$0.42/hour)\n"
                "Individual credits: $12.00 remaining\nWorkspace Delrey: $40 remaining\n")
        s = apikeys.AmpProvider.parse(text)
        self.assertAlmostEqual(s.windows[0].used_pct, 35)
        self.assertEqual(s.notes, ["Credits: $12.00", "Delrey: $40.00"])
        s = apikeys.AmpProvider.parse("Amp Pro Subscription: 80% other usage and 90% orb usage remaining - resets "
                                      "upon renewal in 12 days")
        self.assertEqual(labels(s), ["Usage", "Orb usage"])
        self.assertAlmostEqual(s.windows[0].used_pct, 20)

    def test_amp_api(self):
        http = FakeHTTP({"userDisplayBalanceInfo": {"ok": True, "result": {"displayText": "Amp Free: 70% remaining today"}}})
        with mock.patch.object(apikeys, "http_json", http):
            s = PROVIDERS["amp"]({"api_key": "a"}).fetch()
        self.assertAlmostEqual(s.windows[0].used_pct, 30)


class LocalProviderTests(HomeTestCase):
    def test_windsurf(self):
        db = self.home / "ws.vscdb"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
        plan = {"planName": "Pro", "quotaUsage": {"dailyRemainingPercent": 80, "weeklyRemainingPercent": 55,
                                                  "dailyResetAtUnix": 1893456000}}
        con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("windsurf.settings.cachedPlanInfo", json.dumps(plan).encode()))
        con.commit()
        con.close()
        s = PROVIDERS["windsurf"]({"db_path": str(db)}).fetch()
        self.assertEqual(s.plan, "Pro")
        self.assertEqual([w.used_pct for w in s.windows], [20, 45])
        s = local.WindsurfProvider.parse({"usage": {"messages": 500, "remainingMessages": 400}})
        self.assertAlmostEqual(s.windows[0].used_pct, 20)

    def kiro_db(self, path, key="kirocli:odic:token", expires=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE auth_kv (key TEXT, value TEXT)")
        con.execute("CREATE TABLE state (key TEXT, value TEXT)")
        tok = {"access_token": "at", "expires_at": expires or "2099-01-01T00:00:00Z"}
        con.execute("INSERT INTO auth_kv VALUES (?, ?)", (key, json.dumps(tok)))
        con.execute("INSERT INTO state VALUES (?, ?)", ("api.codewhisperer.profile",
                                                         json.dumps({"arn": "arn:aws:codewhisperer:eu-central-1:1:profile/X"})))
        con.commit()
        con.close()

    RESP = {"nextDateReset": 1893456000, "subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
            "usageBreakdownList": [{"resourceType": "CREDIT", "usageLimitWithPrecision": 1000,
                                    "currentUsageWithPrecision": 250.5, "currentOveragesWithPrecision": 0}]}

    def test_kiro(self):
        db = self.home / "kiro" / "data.sqlite3"
        self.kiro_db(db)
        http = FakeHTTP({"q.eu-central-1": self.RESP})
        with mock.patch.object(local, "http_json", http):
            s = PROVIDERS["kiro"]({"auth_path": str(db)}).fetch()
        c = http.calls[0]
        self.assertEqual(c["headers"]["X-Amz-Target"], "AmazonCodeWhispererService.GetUsageLimits")
        self.assertEqual(c["json"], {"profileArn": "arn:aws:codewhisperer:eu-central-1:1:profile/X"})
        self.assertEqual(s.plan, "Kiro Pro")
        self.assertAlmostEqual(s.windows[0].used_pct, 25.05)

    def test_kiro_ide_token_and_expiry(self):
        self.write(".aws/sso/cache/kiro-auth-token.json", {"accessToken": "ide", "expiresAt": "2099-01-01T00:00:00Z"})
        http = FakeHTTP({"codewhisperer.us-east-1": self.RESP})
        with mock.patch.object(local, "http_json", http):
            PROVIDERS["kiro"]({"auth_path": str(self.home / "none.sqlite3")}).fetch()
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer ide")
        self.write(".aws/sso/cache/kiro-auth-token.json", {"accessToken": "ide", "expiresAt": "2000-01-01T00:00:00Z"})
        with self.assertRaises(AuthExpired):
            PROVIDERS["kiro"]({"auth_path": str(self.home / "none.sqlite3")}).fetch()

    def test_amazon_q(self):
        db = self.home / "q" / "data.sqlite3"
        self.kiro_db(db, key="codewhisperer:odic:token")
        with mock.patch.object(local, "http_json", FakeHTTP({"amazonaws.com": self.RESP})):
            s = PROVIDERS["amazonq"]({"auth_path": str(db)}).fetch()
        self.assertEqual(labels(s), ["Credits (mo)"])

    def test_antigravity_parse(self):
        j = {"userStatus": {"planStatus": {"planInfo": {"planName": "Pro"}}, "cascadeModelConfigData": {
            "clientModelConfigs": [
                {"label": "Gemini 3 Pro", "quotaInfo": {"remainingFraction": 0.6, "resetTime": "2030-01-01T00:00:00Z"}},
                {"label": "Claude Sonnet", "quotaInfo": {"remainingFraction": 1}},
                {"label": "No quota"}]}}}
        s = local.AntigravityProvider.parse(j)
        self.assertEqual(labels(s), ["Gemini 3 Pro", "Claude Sonnet"])
        self.assertAlmostEqual(s.windows[0].used_pct, 40)

    def test_antigravity_probe(self):
        p = PROVIDERS["antigravity"]({})
        cmd = "/App/language_server_macos_arm --csrf_token abc --extension_server_port 5000 --app_data_dir antigravity"
        http = FakeHTTP({"127.0.0.1:6000": {"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "M", "quotaInfo": {"remainingFraction": 0.5}}]}}}})
        with mock.patch.object(p, "processes", lambda: [(42, cmd)]), \
                mock.patch.object(p, "ports", lambda pid, c: [6000]), mock.patch.object(local, "http_json", http):
            s = p.fetch()
        self.assertEqual(http.calls[0]["headers"]["X-Codeium-Csrf-Token"], "abc")
        self.assertTrue(http.calls[0]["kw"]["local_tls"])
        self.assertAlmostEqual(s.windows[0].used_pct, 50)
        with mock.patch.object(p, "processes", lambda: []):
            with self.assertRaises(NotConfigured):
                p.fetch()

    def test_grok_parse(self):
        s = local.GrokProvider.parse({"config": {"creditUsagePercent": 33, "subscriptionTier": "SUPERGROK",
                                                 "currentPeriod": {"start": "2029-12-01T00:00:00Z",
                                                                   "end": "2030-01-01T00:00:00Z"}}})
        self.assertEqual((s.plan, s.windows[0].used_pct), ("Supergrok", 33))
        s = local.GrokProvider.parse({"monthlyLimit": {"val": 3000}, "usage": {"includedUsed": {"val": 750}},
                                      "billingCycle": {"billingPeriodEnd": "2030-01-01T00:00:00Z"}})
        self.assertAlmostEqual(s.windows[0].used_pct, 25)

    def test_jsonrpc_session(self):
        import sys
        script = ("import sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n"
                  " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'ok':m['method']}}),flush=True)\n")
        msg = local.jsonrpc_session([sys.executable, "-c", script],
                                    [{"id": 1, "method": "initialize"}, {"id": 2, "method": "_x.ai/billing"}], 2, 10)
        self.assertEqual(msg["result"], {"ok": "_x.ai/billing"})

    def test_coderabbit(self):
        s = local.CodeRabbitProvider.parse("\x1b[1mOrganization:\x1b[0m delrey\nPlan: Pro\nYour reviews: 14\n"
                                           "Period resets: 2030-01-01\n")
        self.assertEqual(s.plan, "Pro")
        self.assertEqual(s.windows[0].detail, "14 this period")
        self.assertIsNone(s.windows[0].used_pct)
        with self.assertRaises(AuthExpired):
            local.CodeRabbitProvider.parse("Error: not authenticated, run coderabbit auth login")
