"""Local usage history (SQLite, next to the config file) and time-to-limit projections.

Every successful fetch records one sample per limit. Nothing leaves your machine. Samples older than
RETENTION_DAYS are pruned. Projections use the recent burn rate from history, falling back to the
average pace since the window started.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core import Snapshot, UsageWindow, config_dir, fmt_delta, log

RETENTION_DAYS = 30
SAME_PERIOD_S = 180       # resets_at values within this are the same window period
RECENT_S = 3600           # burn rate looks at the last hour...
MIN_SPAN_S = 600          # ...but needs at least 10 minutes of samples
RISK_MARGIN = 0.9         # warn only if the limit is hit before 90% of the remaining time
_lock = threading.Lock()

Sample = tuple[float, float, Optional[float]]  # (ts, used_pct, resets_at)


def db_path() -> Path:
    return config_dir() / "history.db"


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL, "
                "used REAL NOT NULL, resets REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS samples_key ON samples (key, label, ts)")
    return con


def provider_key(ptype: str, name: str) -> str:
    return f"{ptype}:{name}"


def record(key: str, snap: Snapshot, ts: Optional[float] = None) -> None:
    rows = [(ts or snap.fetched_at or time.time(), key, w.label, float(w.used_pct), w.resets_at)
            for w in snap.windows if w.used_pct is not None]
    if not rows:
        return
    try:
        with _lock, _connect() as con:
            con.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?)", rows)
    except sqlite3.Error as e:
        log(f"history: record failed: {e}")


def series(key: str, label: str, since: float) -> list[Sample]:
    try:
        with _lock, _connect() as con:
            cur = con.execute("SELECT ts, used, resets FROM samples WHERE key = ? AND label = ? AND ts >= ? "
                              "ORDER BY ts", (key, label, since))
            return [(float(t), float(u), r) for t, u, r in cur.fetchall()]
    except sqlite3.Error as e:
        log(f"history: read failed: {e}")
        return []


def prune(days: int = RETENTION_DAYS) -> None:
    try:
        with _lock, _connect() as con:
            con.execute("DELETE FROM samples WHERE ts < ?", (time.time() - days * 86400,))
    except sqlite3.Error as e:
        log(f"history: prune failed: {e}")


def clear() -> None:
    try:
        with _lock, _connect() as con:
            con.execute("DELETE FROM samples")
        with _lock, sqlite3.connect(db_path()) as con:
            con.execute("VACUUM")
    except sqlite3.Error as e:
        log(f"history: clear failed: {e}")


def current_period(samples: list[Sample], window: UsageWindow) -> list[Sample]:
    """Samples belonging to the window's current period (same reset time)."""
    if window.resets_at:
        return [s for s in samples if s[2] and abs(s[2] - window.resets_at) <= SAME_PERIOD_S]
    out: list[Sample] = []
    for s in reversed(samples):  # no reset time: walk back until usage drops (a reset happened)
        if out and s[1] > out[-1][1] + 0.5:
            break
        out.append(s)
    return list(reversed(out))


# --------------------------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------------------------

@dataclass
class Projection:
    rate: float                 # % per hour
    hits_at: Optional[float]    # epoch when it reaches 100%, if that's before the reset
    at_reset: Optional[float]   # projected % at reset time, if it won't hit the limit first
    source: str                 # "recent" | "average" | "limit"

    @property
    def at_risk(self) -> bool:
        return self.hits_at is not None

    def summary(self, now: Optional[float] = None) -> str:
        now = now or time.time()
        if self.source == "limit":
            return "At the limit"
        if self.hits_at is not None:
            return f"At this pace: limit in ~{fmt_delta(self.hits_at - now)}"
        if self.rate <= 0:
            return "Not increasing lately"
        if self.at_reset is not None:
            return f"On track: ~{self.at_reset:.0f}% by reset"
        return f"Using ~{self.rate:.1f}%/h"


def _slope(points: list[tuple[float, float]]) -> float:
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    return 0.0 if den == 0 else sum((p[0] - mx) * (p[1] - my) for p in points) / den


def project(window: UsageWindow, samples: list[Sample], now: Optional[float] = None) -> Optional[Projection]:
    """Where this limit is heading. None when there isn't enough to say anything."""
    now = now or time.time()
    used = window.used_pct
    if used is None:
        return None
    if used >= 99.95:
        return Projection(0.0, None, None, "limit")
    period = [s for s in current_period(samples, window) if s[0] >= now - RECENT_S]
    slope, source = None, ""
    if len(period) >= 2 and period[-1][0] - period[0][0] >= MIN_SPAN_S:
        slope, source = max(0.0, _slope([(t, u) for t, u, _ in period])), "recent"
    elif window.resets_at and window.length_s:
        elapsed = window.length_s - (window.resets_at - now)
        if elapsed >= MIN_SPAN_S:
            slope, source = used / elapsed, "average"
    if slope is None:
        return None
    if slope < 0.01 / 3600:  # < 0.01 %/h: effectively flat
        return Projection(0.0, None, used if window.resets_at else None, source)
    hits = now + (100 - used) / slope
    # Only call it "at risk" if it runs out clearly before the reset (not in the last 10% of the time left).
    if window.resets_at and hits >= now + RISK_MARGIN * (window.resets_at - now):
        return Projection(slope * 3600, None, min(100.0, used + slope * (window.resets_at - now)), source)
    return Projection(slope * 3600, hits, None, source)
