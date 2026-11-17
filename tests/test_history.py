import time
import unittest

from subusage import history
from subusage.core import FETCH_HOOKS, Provider, Snapshot, UsageWindow
from subusage.history import project

from .helpers import HomeTestCase

H = 3600


class StoreTests(HomeTestCase):
    def test_record_series_prune_clear(self):
        key = history.provider_key("claude", "Claude")
        old = Snapshot(windows=[UsageWindow("Weekly", 10)], fetched_at=self.now - 40 * 86400)
        history.record(key, old)
        history.record(key, Snapshot(windows=[UsageWindow("Weekly", 20, self.now + H), UsageWindow("X", None)],
                                     fetched_at=self.now))
        rows = history.series(key, "Weekly", 0)
        self.assertEqual([r[1] for r in rows], [10, 20])
        self.assertEqual(history.series(key, "X", 0), [])         # None values aren't stored
        history.prune()
        self.assertEqual([r[1] for r in history.series(key, "Weekly", 0)], [20])
        history.clear()
        self.assertEqual(history.series(key, "Weekly", 0), [])
        self.assertTrue(str(history.db_path()).startswith(str(self.home)))

    def test_fetch_hook_records(self):
        class P(Provider):
            type = "t"

            def fetch(self):
                return Snapshot(windows=[UsageWindow("Daily", 42.0, time.time() + H)])
        hook = lambda p, s: history.record(history.provider_key(p.type, p.name), s)  # noqa: E731
        FETCH_HOOKS.append(hook)
        try:
            P({"name": "Mine"}).run_fetch()
        finally:
            FETCH_HOOKS.remove(hook)
        self.assertEqual(history.series("t:Mine", "Daily", 0)[0][1], 42.0)

    def test_broken_hook_is_contained(self):
        class P(Provider):
            type = "t"

            def fetch(self):
                return Snapshot(windows=[UsageWindow("D", 1)])

        def boom(p, s):
            raise RuntimeError("nope")
        FETCH_HOOKS.append(boom)
        try:
            p = P({})
            p.run_fetch()
        finally:
            FETCH_HOOKS.remove(boom)
        self.assertEqual(p.snapshot.error, "")


class ProjectionTests(unittest.TestCase):
    now = 1_800_000_000.0

    def win(self, used, reset_in=2 * H, length=5 * H):
        return UsageWindow("Session (5h)", used, self.now + reset_in, length)

    def samples(self, points, reset_in=2 * H):
        return [(self.now - ago, used, self.now + reset_in) for ago, used in points]

    def test_recent_rate_hits_before_reset(self):
        # 30% → 60% over the last 30 min = 60 %/h → 40% left takes 40 min (< 2 h to reset)
        s = self.samples([(1800, 30), (1200, 40), (600, 50), (0, 60)])
        p = project(self.win(60), s, self.now)
        self.assertEqual(p.source, "recent")
        self.assertAlmostEqual(p.rate, 60, places=3)
        self.assertTrue(p.at_risk)
        self.assertAlmostEqual(p.hits_at - self.now, 40 * 60, delta=1)
        self.assertIn("limit in ~40m", p.summary(self.now))

    def test_recent_rate_safe(self):
        s = self.samples([(1800, 30), (0, 31)])           # 2 %/h
        p = project(self.win(31), s, self.now)
        self.assertFalse(p.at_risk)
        self.assertAlmostEqual(p.at_reset, 35, places=3)
        self.assertIn("~35% by reset", p.summary(self.now))

    def test_flat_and_falling(self):
        p = project(self.win(50), self.samples([(1800, 50), (0, 50)]), self.now)
        self.assertEqual((p.rate, p.at_risk), (0.0, False))
        self.assertEqual(p.summary(self.now), "Not increasing lately")

    def test_other_period_ignored_falls_back_to_average(self):
        # samples from a previous window (different reset) are ignored → average since window start
        s = [(self.now - 1200, 90, self.now - 600), (self.now - 900, 95, self.now - 600)]
        p = project(self.win(10, reset_in=4 * H), s, self.now)   # 1 h elapsed, 10% used → 10 %/h
        self.assertEqual(p.source, "average")
        self.assertAlmostEqual(p.rate, 10, places=3)
        self.assertFalse(p.at_risk)                                 # ends at ~50%

    def test_average_matches_pace_marker(self):
        # used% ahead of elapsed% ⇔ at risk
        ahead = project(self.win(80, reset_in=2.5 * H), [], self.now)    # 50% elapsed, 80% used
        behind = project(self.win(30, reset_in=2.5 * H), [], self.now)
        self.assertTrue(ahead.at_risk)
        self.assertFalse(behind.at_risk)

    def test_borderline_is_not_flagged(self):
        # 44% used, 3 of 7 days elapsed: on pace to run out ~5 h before reset → too close to call
        w = UsageWindow("Weekly", 44, self.now + 4 * 86400, 7 * 86400)
        self.assertFalse(project(w, [], self.now).at_risk)

    def test_not_enough_data(self):
        self.assertIsNone(project(UsageWindow("Weekly", 40), [], self.now))                 # no reset/length
        self.assertIsNone(project(self.win(5, reset_in=5 * H - 60), [], self.now))          # window just began
        self.assertIsNone(project(UsageWindow("X", None, self.now + H, H), [], self.now))
        self.assertEqual(project(self.win(100), [], self.now).summary(self.now), "At the limit")

    def test_current_period_without_reset(self):
        w = UsageWindow("Monthly", 12)
        s = [(1, 80, None), (2, 95, None), (3, 5, None), (4, 9, None), (5, 12, None)]
        self.assertEqual([x[1] for x in history.current_period(s, w)], [5, 9, 12])


if __name__ == "__main__":
    unittest.main()
