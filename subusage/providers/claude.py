"""Anthropic Claude Pro / Max, via the endpoint behind Claude Code's /usage screen.

Limits are shared across claude.ai, Claude Desktop, mobile and Claude Code, so this is the
account-wide picture. Auth: the OAuth token Claude Code saves when you log in.
"""
from __future__ import annotations

import json
import time

from ..core import (IS_MAC, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow,
                    fnum, http_json, parse_time, read_json_file, run_cmd, title_case)


class ClaudeProvider(Provider):
    type = "claude"
    default_name = "Claude"
    vendor = "Anthropic"
    plans = "Pro, Max"
    setup = ("Install Claude Code and run `claude` → /login with your subscription. "
             "On macOS the login lives in the Keychain (you may get one access prompt).")
    usage_url = "https://claude.ai/settings/usage"
    accent = "#d97757"
    min_poll = 120  # this endpoint rate-limits hard; 429s can last 30+ minutes

    path_key, login_dir, login_file, env_var = "credentials_path", ".claude", ".credentials.json", "CLAUDE_CONFIG_DIR"
    login_what = "Claude Code login"
    fields = (Field("credentials_path", "Login file", "path",
                    "Blank = default. Use Find… if Claude Code runs in WSL."),)

    URL = "https://api.anthropic.com/api/oauth/usage"
    LABELS = {
        "five_hour": ("Session (5h)", 5 * 3600),
        "seven_day": ("Weekly", 7 * 86400),
        "seven_day_opus": ("Weekly · Opus", 7 * 86400),
        "seven_day_sonnet": ("Weekly · Sonnet", 7 * 86400),
        "seven_day_oauth_apps": ("Weekly · Apps", 7 * 86400),
    }

    def _credentials(self) -> dict:
        path = self.login_path()
        custom = bool(self.setting(self.path_key))
        if path and (path.exists() or custom or not IS_MAC):
            return read_json_file(path, self.login_what)
        # macOS: Claude Code keeps the login in the Keychain.
        out = run_cmd(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], timeout=20)
        if out:
            try:
                return json.loads(out)
            except ValueError:
                raise ProviderError("Couldn't parse the Claude Code Keychain item") from None
        raise NotConfigured("Claude Code login not found (file or macOS Keychain)")

    def fetch(self) -> Snapshot:
        oauth = self._credentials().get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            raise ProviderError("No Claude login found; run `claude` and log in with your subscription")
        expires = parse_time(oauth.get("expiresAt"))
        try:
            j = http_json(self.URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
        except HttpError as e:
            if e.status == 401 and expires and expires < time.time():
                raise ProviderError("Token expired; open Claude Code once to refresh it") from None
            raise
        snap = Snapshot(plan=title_case(oauth.get("subscriptionType")))
        order = list(self.LABELS)
        items = sorted(((k, v) for k, v in j.items() if isinstance(v, dict) and k != "extra_usage"),
                       key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
        for key, val in items:
            util = fnum(val.get("utilization"))
            if util is None:
                continue
            label, length = self.LABELS.get(key, (title_case(key), 7 * 86400 if key.startswith("seven_day") else None))
            snap.windows.append(UsageWindow(label, util, parse_time(val.get("resets_at")), length))
        extra = j.get("extra_usage") or {}
        if isinstance(extra, dict) and extra.get("is_enabled"):
            util = fnum(extra.get("utilization"))
            used, limit = fnum(extra.get("used_credits")), fnum(extra.get("monthly_limit"))
            if util is None and used is not None and limit:
                util = used / limit * 100
            if util is not None:
                snap.windows.append(UsageWindow("Extra usage (mo)", min(100.0, util)))
        if not snap.windows:
            raise ProviderError("Usage endpoint returned no windows")
        return snap
