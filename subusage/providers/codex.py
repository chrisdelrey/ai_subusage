"""OpenAI ChatGPT plans (Plus / Pro / Business), via the endpoint behind Codex CLI's /status.

Shows the Codex allowance (5-hour and weekly). ChatGPT's in-app message caps aren't exposed anywhere.
"""
from __future__ import annotations

import time
from typing import Optional

from ..core import (AuthExpired, Field, HttpError, Provider, ProviderError, Snapshot, UsageWindow, fnum, http_json,
                    jwt_claims, parse_time, read_json_file, title_case)


class CodexProvider(Provider):
    type = "codex"
    default_name = "ChatGPT"
    vendor = "OpenAI"
    plans = "Plus, Pro, Business"
    setup = "Install Codex CLI and run `codex login` → Sign in with ChatGPT (not an API key)."
    usage_url = "https://chatgpt.com/codex/settings/usage"
    accent = "#10a37f"

    login_dir, login_file, env_var, login_what = ".codex", "auth.json", "CODEX_HOME", "Codex CLI login"
    fields = (Field("auth_path", "Login file", "path", "Blank = default. Use Find… if Codex runs in WSL."),)

    URL = "https://chatgpt.com/backend-api/wham/usage"

    @staticmethod
    def window_label(key: str, length: Optional[float]) -> str:
        if length:
            if abs(length - 18000) < 60:
                return "Session (5h)"
            if abs(length - 604800) < 3600:
                return "Weekly"
            return f"{round(length / 3600)}h window" if length < 86400 else f"{round(length / 86400)}d window"
        return "Session" if key == "primary_window" else "Weekly"

    def fetch(self) -> Snapshot:
        data = read_json_file(self.login_path(), self.login_what)
        tokens = data.get("tokens") or {}
        token = tokens.get("access_token")
        if not token:
            raise ProviderError("No ChatGPT login in Codex; run `codex login` (not an API key)")
        claims = jwt_claims(token)
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
        account_id = tokens.get("account_id") or auth_claims.get("chatgpt_account_id")
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        residency = auth_claims.get("chatgpt_data_residency") or auth_claims.get("chatgpt_compute_residency")
        if residency:
            headers["x-openai-internal-codex-residency"] = str(residency)
        try:
            j = http_json(self.URL, headers)
        except HttpError as e:
            exp = fnum(claims.get("exp"))
            if e.status == 401 and exp and exp < time.time():
                raise AuthExpired("Login expired; it refreshes the next time you use Codex") from None
            raise
        snap = Snapshot(plan=title_case(j.get("plan_type") or auth_claims.get("chatgpt_plan_type")))
        rl = j.get("rate_limit") or {}
        now = time.time()
        for key in ("primary_window", "secondary_window"):
            w = rl.get(key)
            if not isinstance(w, dict) or fnum(w.get("used_percent")) is None:
                continue
            length = fnum(w.get("limit_window_seconds"))
            reset = parse_time(w.get("reset_at"))
            if reset is None and fnum(w.get("reset_after_seconds")) is not None:
                reset = now + float(w["reset_after_seconds"])
            snap.windows.append(UsageWindow(self.window_label(key, length), float(w["used_percent"]), reset, length))
        credits = j.get("credits") or {}
        if isinstance(credits, dict) and credits.get("has_credits"):
            if credits.get("unlimited"):
                snap.notes.append("Credits: unlimited")
            elif fnum(credits.get("balance")) is not None:
                snap.notes.append(f"Credits: {float(credits['balance']):,.2f}")
        if rl.get("limit_reached"):
            snap.notes.append("Limit reached")
        if not snap.windows:
            raise ProviderError("Usage endpoint returned no windows")
        return snap
