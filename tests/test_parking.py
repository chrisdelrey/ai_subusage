"""Expired logins park a provider (no network) until its login changes."""
import os
import time
import unittest
from unittest import mock

from subusage import core
from subusage.core import AuthExpired, HttpError, Provider, Scheduler, Snapshot, UsageWindow
from subusage.providers import claude, keyed

from .helpers import FakeHTTP, HomeTestCase


class FileLogin(Provider):
    type = "t"
    login_dir, login_file = ".tool", "auth.json"
    calls = 0
    result = None

    def fetch(self):
        type(self).calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return Snapshot(windows=[UsageWindow("Daily", 10)])


class ParkingTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        FileLogin.calls = 0
        self.login = self.write(".tool/auth.json", {"token": "old"})

    def test_park_then_unpark_on_login_change(self):
        p = FileLogin({})
        p.result = AuthExpired("Login expired")
        p.run_fetch()
        self.assertTrue(p.parked)
        self.assertEqual(p.snapshot.error, "Login expired")
        now = time.time()
        self.assertFalse(p.check_unpark(now + 60))           # nothing changed → stay parked
        self.login.write_text('{"token": "refreshed!"}')     # the tool was used and refreshed its login
        os.utime(self.login, (now + 120, now + 120))
        self.assertTrue(p.check_unpark(now + 120))
        p.result = None
        p.run_fetch()
        self.assertFalse(p.parked)
        self.assertEqual(p.snapshot.error, "")

    def test_watch_interval_throttles_checks(self):
        p = FileLogin({})
        p.park("x")
        self.login.write_text("changed")
        self.assertFalse(p.check_unpark(time.time() + 1))    # before PARK_WATCH_S
        self.assertTrue(p.check_unpark(time.time() + p.PARK_WATCH_S + 1))

    def test_http_401_parks_file_login_providers(self):
        p = FileLogin({})
        p.result = HttpError(401, "")
        p.run_fetch()
        self.assertTrue(p.parked)

    def test_scheduler_makes_no_calls_while_parked(self):
        p = FileLogin({})
        p.result = AuthExpired("x")
        p.run_fetch()
        calls = FileLogin.calls
        sched = Scheduler([p])
        with mock.patch.object(core.threading, "Thread") as thread:
            sched.wake.set()
            # one scheduler pass, inline
            for prov in sched.providers:
                if prov.parked and prov.check_unpark(time.time() + 5):
                    prov.run_fetch()
            thread.assert_not_called()
        self.assertEqual(FileLogin.calls, calls)

    def test_refresh_now_retries_parked(self):
        p = FileLogin({})
        p.park("x")
        Scheduler([p]).refresh_all()
        self.assertTrue(p.check_unpark())

    def test_no_watch_path_falls_back_to_slow_retry(self):
        class NoFile(FileLogin):
            def watch_paths(self):
                return []
        p = NoFile({})
        p.park("x")
        now = time.time()
        self.assertFalse(p.check_unpark(now + 600))
        self.assertTrue(p.check_unpark(now + p.PARK_RETRY_S + 1))

    def test_key_providers_do_not_park(self):
        with mock.patch.object(keyed, "http_json", FakeHTTP({"api.z.ai": HttpError(401, "")})):
            p = keyed.ZaiProvider({"api_key": "wrong"})
            p.run_fetch()
        self.assertFalse(p.parked)
        self.assertIn("rejected", p.snapshot.error)
        self.assertTrue(keyed.ClinePassProvider({}).parks_on_auth)
        self.assertFalse(keyed.ClinePassProvider({"api_key": "k"}).parks_on_auth)

    def test_claude_expired_parks_and_wakes_on_new_token(self):
        creds = self.write(".claude/.credentials.json", {"claudeAiOauth": {
            "accessToken": "old", "expiresAt": int((self.now - 60) * 1000)}})
        p = claude.ClaudeProvider({})
        with mock.patch.object(claude, "http_json", FakeHTTP({"/api/oauth/usage": HttpError(401, "")})):
            p.run_fetch()
        self.assertTrue(p.parked)
        self.assertIn("next time you use Claude Code", p.snapshot.error)
        creds.write_text('{"claudeAiOauth": {"accessToken": "new-token", "expiresAt": 1}}')
        os.utime(creds, (self.now + 99, self.now + 99))
        self.assertTrue(p.check_unpark(time.time() + 120))

    def test_claude_keychain_signature(self):
        item = '{"claudeAiOauth": {"accessToken": "abcdefghijklmnop", "expiresAt": 123}}'
        with mock.patch.object(claude, "IS_MAC", True), mock.patch.object(claude, "run_cmd", return_value=item):
            self.assertEqual(claude.ClaudeProvider({}).watch_signature(), ("efghijklmnop", 123))


if __name__ == "__main__":
    unittest.main()
