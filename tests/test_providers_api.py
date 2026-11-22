"""Pay-as-you-go API usage providers against canned responses (no network)."""
from unittest import mock

from subusage.core import HttpError, NotConfigured, ProviderError
from subusage.providers import PROVIDERS, api

from .helpers import FakeHTTP, HomeTestCase


def labels(snap):
    return [w.label for w in snap.windows]


class ApiBaseTests(HomeTestCase):
    def test_every_api_provider_has_instructions(self):
        for t, cls in PROVIDERS.items():
            if cls.auth != "api":
                continue
            self.assertTrue(cls.key_url.startswith("https://"), t)
            self.assertGreater(len(cls.setup), 40, t)
            self.assertEqual(cls.fields[0].key, "api_key", t)

    def test_budget_kinds(self):
        self.assertEqual(PROVIDERS["openai_api"].fields[-1].label, "Monthly budget")
        self.assertEqual(PROVIDERS["openrouter"].fields[-1].label, "Full balance")

    def test_missing_key(self):
        with self.assertRaises(NotConfigured):
            PROVIDERS["deepseek"]({}).fetch()

    def test_rejected_key_stays_visible(self):
        p = PROVIDERS["openai_api"]({"api_key": "sk-proj-x"})
        with mock.patch.object(api, "http_json", FakeHTTP({"organization/costs": HttpError(403, "")})):
            p.run_fetch()
        self.assertFalse(p.parked)
        self.assertIn("Admin key", p.snapshot.error)


class SpendTests(HomeTestCase):
    COSTS1 = {"data": [{"start_time": 1, "results": [{"amount": {"value": 1.25, "currency": "usd"}}]},
                       {"start_time": 2, "results": [{"amount": {"value": 3.75, "currency": "usd"}}]}],
              "has_more": True, "next_page": "p2"}
    COSTS2 = {"data": [{"start_time": 3, "results": [{"amount": {"value": 5.0, "currency": "usd"}}]}], "has_more": False}

    def test_openai_paginates_and_budget(self):
        http = FakeHTTP({"page=p2": self.COSTS2, "organization/costs": self.COSTS1})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["openai_api"]({"api_key": "sk-admin-1", "budget": "$40"}).fetch()
        self.assertEqual(len(http.calls), 2)
        self.assertIn("bucket_width=1d", http.calls[0]["url"])
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer sk-admin-1")
        self.assertEqual(labels(s), ["Spend (mo)"])
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertEqual(s.windows[0].detail, "$10.00 of $40.00")
        self.assertTrue(s.windows[0].resets_at)

    def test_openai_without_budget(self):
        with mock.patch.object(api, "http_json", FakeHTTP({"organization/costs": self.COSTS2})):
            s = PROVIDERS["openai_api"]({"api_key": "k", "project_id": "proj_1"}).fetch()
        self.assertEqual(s.notes, ["Spent this month: $5.00"])

    def test_anthropic(self):
        j = {"data": [{"results": [{"amount": "1250.5"}, {"amount": "49.5"}]}], "has_more": False}
        http = FakeHTTP({"cost_report": j})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["anthropic_api"]({"api_key": "sk-ant-admin01"}).fetch()
        self.assertEqual(s.notes, ["Spent this month: $13.00"])
        h = http.calls[0]["headers"]
        self.assertEqual((h["x-api-key"], h["anthropic-version"]), ("sk-ant-admin01", "2023-06-01"))
        self.assertIn("starting_at=", http.calls[0]["url"])

    def test_deepinfra(self):
        p = PROVIDERS["deepinfra"]({"api_key": "k", "budget": "20"})
        s = p.parse({"stripe_balance": -15.0, "recent": 2.0, "suspended": False},
                    {"months": [{"period": "2026-10", "total_cost": 900}, {"period": "2026-11", "total_cost": 500}]})
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertIn("Credit left: $13.00", s.notes)


class BalanceTests(HomeTestCase):
    def test_openrouter(self):
        http = FakeHTTP({"/api/v1/key": {"data": {"limit": 20, "limit_remaining": 15, "limit_reset": "monthly",
                                                   "usage_monthly": 4.2, "is_free_tier": False}},
                         "/api/v1/credits": {"data": {"total_credits": 50, "total_usage": 12.5}}})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["openrouter"]({"api_key": "sk-or-1", "budget": "50"}).fetch()
        self.assertEqual(labels(s), ["Key limit (mo)", "Credit used"])
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertAlmostEqual(s.windows[1].used_pct, 25)
        self.assertIn("This key this month: $4.20", s.notes)

    def test_openrouter_without_credits_endpoint(self):
        http = FakeHTTP({"/api/v1/key": {"data": {"limit": None, "usage_monthly": 1}},
                         "/api/v1/credits": HttpError(403, "")})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["openrouter"]({"api_key": "k"}).fetch()
        self.assertEqual(s.notes, ["This key this month: $1.00"])

    def test_deepseek(self):
        p = PROVIDERS["deepseek"]({"api_key": "k"})
        s = p.parse({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "70"},
                                                              {"currency": "USD", "total_balance": "10.50",
                                                               "granted_balance": "1.00"}]})
        self.assertEqual(s.notes, ["Balance: $10.50", "Includes granted: $1.00"])

    def test_moonshot_region(self):
        http = FakeHTTP({"api.moonshot.cn": {"code": 0, "scode": "0x0", "status": True,
                                             "data": {"available_balance": 49.5, "voucher_balance": 0, "cash_balance": 49.5}}})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["moonshot"]({"api_key": "k", "region": "china"}).fetch()
        self.assertEqual(s.notes, ["Balance: ¥49.50"])
        with self.assertRaises(ProviderError):
            PROVIDERS["moonshot"]({}).parse({"code": 1, "scode": "0x1"})

    def test_xai(self):
        http = FakeHTTP({"/prepaid/balance": {"total": {"val": "-2500"}}})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["xai_api"]({"api_key": "m", "team_id": "team 1", "budget": "100"}).fetch()
        self.assertIn("/teams/team%201/prepaid", http.calls[0]["url"])
        self.assertAlmostEqual(s.windows[0].used_pct, 75)
        with self.assertRaises(NotConfigured):
            PROVIDERS["xai_api"]({"api_key": "m"}).fetch()

    def test_simple_balances(self):
        self.assertEqual(PROVIDERS["vercel_ai"]({}).parse({"balance": "12.34", "total_used": "7"}).notes,
                         ["Balance: $12.34", "Used to date: $7.00"])
        self.assertEqual(PROVIDERS["siliconflow"]({}).parse({"data": {"totalBalance": "3.2"}}).notes, ["Balance: $3.20"])
        self.assertEqual(PROVIDERS["fal"]({}).parse({"username": "c", "credits": {"current_balance": 8, "currency": "USD"}}).notes,
                         ["Balance: $8.00"])
        http = FakeHTTP({"api.fal.ai": {"credits": {"current_balance": 1}}})
        with mock.patch.object(api, "http_json", http):
            PROVIDERS["fal"]({"api_key": "id:secret"}).fetch()
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Key id:secret")
        with mock.patch.object(api, "http_json", FakeHTTP({"linkup.so": {"balance": 4}})):
            self.assertEqual(PROVIDERS["linkup"]({"api_key": "k"}).fetch().notes, ["Balance: $4.00"])

    def test_venice(self):
        s = PROVIDERS["venice"]({}).parse({"canConsume": True, "balances": {"diem": 7.5, "usd": 2},
                                           "diemEpochAllocation": 10})
        self.assertEqual(labels(s), ["DIEM (day)"])
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertIn("Balance: $2.00", s.notes)


class QuotaTests(HomeTestCase):
    def test_tavily(self):
        s = api.TavilyProvider.parse({"account": {"current_plan": "researcher", "plan_usage": 250, "plan_limit": 1000},
                                      "key": {"usage": 10, "limit": None}})
        self.assertEqual((s.plan, labels(s)), ("Researcher", ["Credits (mo)"]))
        self.assertAlmostEqual(s.windows[0].used_pct, 25)

    def test_elevenlabs(self):
        http = FakeHTTP({"user/subscription": {"tier": "creator", "status": "active", "character_count": 25000,
                                               "character_limit": 100000, "next_character_count_reset_unix": 1893456000}})
        with mock.patch.object(api, "http_json", http):
            s = PROVIDERS["elevenlabs"]({"api_key": "xi"}).fetch()
        self.assertEqual(http.calls[0]["headers"]["xi-api-key"], "xi")
        self.assertEqual(s.plan, "Creator")
        self.assertAlmostEqual(s.windows[0].used_pct, 25)
        self.assertEqual(s.windows[0].resets_at, 1893456000)
