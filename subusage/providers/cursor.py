"""Cursor (Hobby / Pro / Pro+ / Ultra / Teams), via cursor.com's usage-summary API.

Auth: the access token the Cursor desktop app keeps in its local state database (read-only), or the
`cursor-agent` CLI login. Newer Cursor builds on macOS encrypt that token with the Keychain; if so,
paste a session token in Settings instead.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ..core import (AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow,
                    app_data_dirs, fnum, http_json, jwt_claims, parse_time, title_case)


def _decode(v: Any) -> str:
    if isinstance(v, bytes):
        v = v.decode("utf-16-le", "ignore") if b"\x00" in v[:8] else v.decode("utf-8", "ignore")
    return str(v or "").strip().strip('"')


def read_app_token(db: Path) -> Optional[str]:
    if not db.exists():
        return None
    uri = f"{db.resolve().as_uri()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return _decode(row[0]) if row else None


class CursorProvider(Provider):
    type = "cursor"
    default_name = "Cursor"
    vendor = "Anysphere"
    plans = "Hobby, Pro, Pro+, Ultra, Teams"
    setup = "Sign in to the Cursor app (or `cursor-agent login`). The widget reads that login read-only."
    usage_url = "https://cursor.com/dashboard?tab=usage"
    accent = "#9aa4b2"
    fields = (
        Field("db_path", "Cursor state DB", "path", "Blank = auto-detect Cursor's state.vscdb."),
        Field("session_token", "Session token", "secret",
              "Optional: WorkosCursorSessionToken cookie from cursor.com, if the app token is encrypted."),
    )
    path_key = "db_path"
    login_what = "Cursor login"

    @classmethod
    def default_path(cls) -> Optional[Path]:
        for base in app_data_dirs():
            p = base / "Cursor" / "User" / "globalStorage" / "state.vscdb"
            if p.exists():
                return p
        return app_data_dirs()[0] / "Cursor" / "User" / "globalStorage" / "state.vscdb"

    def watch_paths(self) -> list[Path]:
        paths = [Path.home() / ".config" / "cursor" / "auth.json"]
        db = self.login_path()
        if db:
            paths = [db, db.with_name(db.name + "-wal")] + paths
        return paths

    def _cookie(self) -> str:
        manual = self.setting("session_token")
        if manual:
            return manual if "=" in manual else f"WorkosCursorSessionToken={manual}"
        tokens: list[str] = []
        db = self.login_path()
        app = read_app_token(db) if db else None
        if app:
            tokens.append(app)
        agent = Path.home() / ".config" / "cursor" / "auth.json"
        if agent.exists():
            try:
                import json
                t = json.loads(agent.read_text(encoding="utf-8")).get("accessToken")
                if t:
                    tokens.append(str(t))
            except (OSError, ValueError):
                pass
        if not tokens:
            raise NotConfigured("Cursor login not found; sign in to the Cursor app")
        for tok in tokens:
            if tok.startswith("cursor-safe-storage") or tok.count(".") != 2:
                continue
            claims = jwt_claims(tok)
            exp = fnum(claims.get("exp"))
            if exp and exp - 60 < time.time():
                continue
            uid = str(claims.get("sub") or "").split("|")[-1]
            if uid:
                return f"WorkosCursorSessionToken={uid}%3A%3A{tok}"
        if any(t.startswith("cursor-safe-storage") for t in tokens):
            raise ProviderError("Cursor's token is encrypted on this machine; paste a session token in Settings")
        raise AuthExpired("Cursor session expired; it refreshes the next time you open Cursor")

    def fetch(self) -> Snapshot:
        headers = {"Cookie": self._cookie(), "Origin": "https://cursor.com", "Referer": "https://cursor.com/dashboard"}
        try:
            j = http_json("https://cursor.com/api/usage-summary", headers)
        except HttpError as e:
            if e.status in (401, 403):
                raise AuthExpired("Cursor rejected the session; open Cursor to sign in again") from None
            raise
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        snap = Snapshot(plan=title_case(j.get("membershipType")))
        start, end = parse_time(j.get("billingCycleStart")), parse_time(j.get("billingCycleEnd"))
        length = (end - start) if start and end and end > start else (30 * 86400 if end else None)
        ind = j.get("individualUsage") or {}
        plan = ind.get("plan") or {}
        pct = fnum(plan.get("totalPercentUsed"))
        used, limit = fnum(plan.get("used")), fnum(plan.get("limit"))
        if pct is None and used is not None and limit:
            pct = used / limit * 100
        detail = f"${used / 100:,.2f} of ${limit / 100:,.2f}" if used is not None and limit else ""
        if pct is not None:
            snap.windows.append(UsageWindow("Included usage", max(0.0, min(100.0, pct)), end, length, detail))
        for key, label in (("autoPercentUsed", "· Auto"), ("apiPercentUsed", "· API models")):
            v = fnum(plan.get(key))
            if v is not None:
                snap.windows.append(UsageWindow(label, max(0.0, min(100.0, v)), end, length))
        od = ind.get("onDemand") or {}
        od_used, od_limit = fnum(od.get("used")), fnum(od.get("limit"))
        if od.get("enabled") and od_used is not None and od_limit:
            snap.windows.append(UsageWindow("On-demand", max(0.0, min(100.0, od_used / od_limit * 100)), end,
                                            length, f"${od_used / 100:,.2f} of ${od_limit / 100:,.2f}"))
        elif od_used:
            snap.notes.append(f"On-demand spend: ${od_used / 100:,.2f}")
        pooled = (j.get("teamUsage") or {}).get("pooled") or {}
        p_used, p_limit = fnum(pooled.get("used")), fnum(pooled.get("limit"))
        if pooled.get("enabled") and p_used is not None and p_limit:
            snap.windows.append(UsageWindow("Team pool", max(0.0, min(100.0, p_used / p_limit * 100)), end, length))
        if j.get("isUnlimited") and not snap.windows:
            snap.notes.append("Unlimited plan")
        if not snap.windows and not snap.notes:
            raise ProviderError("Cursor returned no usage for this account")
        return snap
