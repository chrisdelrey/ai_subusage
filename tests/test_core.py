import datetime as dt
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from subusage import core
from subusage.core import (HttpError, NotConfigured, Provider, Snapshot, UsageWindow, fmt_delta, normalize_quota,
                           parse_time)

from .helpers import HomeTestCase


class ParseTimeTests(unittest.TestCase):
    def test_formats(self):
        ref = 1795000000.0
        self.assertEqual(parse_time(ref), ref)
        self.assertEqual(parse_time(ref * 1000), ref)
        self.assertEqual(parse_time(str(int(ref))), ref)
        utc = dt.timezone.utc
        t18 = dt.datetime(2026, 11, 16, 18, tzinfo=utc).timestamp()
        self.assertAlmostEqual(parse_time("2026-11-16T18:00:00Z"), t18)
        self.assertAlmostEqual(parse_time("2026-11-16T13:00:00-05:00"), t18)
        self.assertAlmostEqual(parse_time("2026-11-16T18:00:00.123456789+00:00"), t18 + 0.123456, places=4)
        self.assertAlmostEqual(parse_time("2026-11-16T13:00:00.5-05:00"), t18 + 0.5)
        self.assertAlmostEqual(parse_time("2026-12-01"), dt.datetime(2026, 12, 1, tzinfo=utc).timestamp())
        for bad in (None, "", "soon", True, 0, -5):
            self.assertIsNone(parse_time(bad))

    def test_fmt_delta(self):
        self.assertEqual(fmt_delta(59), "0m")
        self.assertEqual(fmt_delta(3 * 3600 + 5 * 60), "3h 05m")
        self.assertEqual(fmt_delta(2 * 86400 + 4 * 3600), "2d 4h")

    def test_normalize_quota(self):
        self.assertEqual(normalize_quota({"percentUsed": 40})[0], 40)
        self.assertEqual(normalize_quota({"percentRemaining": 25})[0], 75)
        self.assertEqual(normalize_quota({"limit": 200, "used": 50})[0], 25)
        self.assertEqual(normalize_quota({"limit": 200, "remaining": 150})[0], 25)
        self.assertEqual(normalize_quota({"used": 30, "remaining": 70})[0], 30)
        pct, reset = normalize_quota({"usedPercent": 150, "renewsAt": "2030-01-01T00:00:00Z"})
        self.assertEqual(pct, 100)
        self.assertIsNotNone(reset)
        self.assertIsNone(normalize_quota({"foo": 1}))
        self.assertIsNone(normalize_quota("nope"))


class _Handler(BaseHTTPRequestHandler):
    seen: dict = {}

    def log_message(self, *a):
        pass

    def _reply(self):
        _Handler.seen = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()}
        if self.path == "/429":
            self.send_response(429)
            self.send_header("Retry-After", "900")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    do_GET = do_POST = _reply


class HttpJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls._proxy = {k: os.environ.pop(k) for k in list(os.environ) if k.lower() in ("http_proxy", "https_proxy")}
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.update(cls._proxy)

    def test_get_with_headers(self):
        self.assertEqual(core.http_json(self.base + "/x", {"Authorization": "Bearer t"}), {"ok": True})
        self.assertEqual(_Handler.seen["headers"]["authorization"], "Bearer t")

    def test_post_json_and_form(self):
        core.http_json(self.base + "/j", json_body={"a": 1})
        self.assertEqual(json.loads(_Handler.seen["body"]), {"a": 1})
        core.http_json(self.base + "/f", form={"grant_type": "refresh_token", "x": "a+b"})
        self.assertIn("x=a%2Bb", _Handler.seen["body"])

    def test_429_retry_after(self):
        with self.assertRaises(HttpError) as cm:
            core.http_json(self.base + "/429")
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 900))


class _P(Provider):
    type = "t"
    result = None

    def fetch(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RunFetchTests(HomeTestCase):
    def test_success_then_429_keeps_data_and_backs_off(self):
        p = _P({"poll_seconds": 300})
        p.result = Snapshot(plan="Pro", windows=[UsageWindow("Weekly", 10)])
        p.run_fetch()
        self.assertEqual(p.snapshot.plan, "Pro")
        p.result = HttpError(429, "", retry_after=1200)
        p.run_fetch()
        self.assertEqual(p.snapshot.windows[0].used_pct, 10)
        self.assertIn("Rate limited", p.snapshot.error)
        self.assertGreater(p.next_at - time.time(), 1100)

    def test_not_configured_hides(self):
        p = _P({})
        p.result = NotConfigured("nope")
        p.run_fetch()
        self.assertFalse(p.configured)

    def test_crash_is_contained(self):
        p = _P({})
        p.result = ZeroDivisionError("boom")
        p.run_fetch()
        self.assertIn("boom", p.snapshot.error)
        self.assertFalse(p.busy)

    def test_env_secret(self):
        os.environ["TEST_KEY"] = "s3cret"
        self.assertEqual(_P({"api_key": "env:TEST_KEY"}).setting("api_key"), "s3cret")
        self.assertEqual(_P({"api_key": " plain "}).setting("api_key"), "plain")


class ConfigTests(HomeTestCase):
    def test_default_and_roundtrip(self):
        cfg = core.load_config()
        types = [p["type"] for p in cfg["providers"]]
        self.assertEqual(types[:3], ["claude", "codex", "copilot"])
        self.assertTrue(core.config_path().exists())
        cfg["width"] = 333
        core.save_config(cfg)
        self.assertEqual(core.load_config()["width"], 333)
        self.assertEqual(len(core.build_providers(cfg)), len(types))

    def test_unknown_provider_skipped(self):
        self.assertEqual(core.build_providers({"providers": [{"type": "nope"}]}), [])

    @unittest.skipUnless(core.IS_LINUX, "Linux autostart")
    def test_linux_autostart(self):
        core.set_startup(True)
        self.assertTrue(core.startup_enabled())
        text = core.linux_autostart().read_text()
        self.assertIn("Exec=", text)
        self.assertIn("subusage", text)
        core.set_startup(False)
        self.assertFalse(core.startup_enabled())

    def test_mac_plist(self):
        plist = self.home / "Library/LaunchAgents/com.subusage.widget.plist"
        with mock.patch.object(core, "IS_WIN", False), mock.patch.object(core, "IS_MAC", True), \
                mock.patch.object(core, "MAC_PLIST", plist):
            core.set_startup(True)
            self.assertIn("<key>RunAtLoad</key><true/>", plist.read_text())
            self.assertTrue(core.startup_enabled())
            core.set_startup(False)
            self.assertFalse(plist.exists())

    def test_wsl_distro_parsing(self):
        fake = mock.Mock(stdout="Ubuntu\r\ndocker-desktop\r\nDebian\r\n".encode("utf-16-le"))
        with mock.patch.object(core, "IS_WIN", True), mock.patch.object(core.subprocess, "run", return_value=fake):
            self.assertEqual(core.wsl_distros(), ["Ubuntu", "Debian"])


if __name__ == "__main__":
    unittest.main()
