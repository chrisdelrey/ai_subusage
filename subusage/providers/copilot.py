"""GitHub Copilot (Free / Pro / Pro+ / Business), via the internal endpoint Copilot's IDE plugins use.

Auth, in order: a token saved by "Sign in with GitHub" in Settings (OAuth device flow, read:user scope),
a token you paste, or the GitHub CLI's login (`gh auth token`).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Optional

from ..core import (IS_WIN, AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError, Snapshot, UsageWindow, fnum,
                    http_json, parse_time, run_cmd, title_case, which)

# The VS Code Copilot OAuth app; device flow with read:user is enough for the usage endpoint.
CLIENT_ID = "Iv1.b507a08c87ecfe98"


class CopilotProvider(Provider):
    type = "copilot"
    default_name = "Copilot"
    vendor = "GitHub"
    plans = "Free, Pro, Pro+, Business, Enterprise"
    setup = "Use “Sign in with GitHub” in Settings, or log in with the GitHub CLI (`gh auth login`)."
    usage_url = "https://github.com/settings/copilot/features"
    accent = "#8250df"
    fields = (
        Field("token", "GitHub token", "secret", "Filled by Sign in with GitHub. Blank = use `gh auth token`."),
        Field("device_login", "Sign in with GitHub", "device_login"),
        Field("host", "Enterprise host", "text", "Blank for github.com; e.g. octocorp.ghe.com"),
    )

    def watch_signature(self):
        """A new token in Settings or a new `gh auth login` both count as a changed login."""
        gh_hosts = (Path(os.environ.get("APPDATA", "")) / "GitHub CLI" / "hosts.yml" if IS_WIN
                    else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh" / "hosts.yml")
        try:
            mtime = gh_hosts.stat().st_mtime_ns
        except OSError:
            mtime = None
        tok = self.setting("token")
        return (hashlib.sha256(tok.encode()).hexdigest()[:12] if tok else "", mtime)

    def _hosts(self) -> tuple[str, str]:
        host = self.setting("host").replace("https://", "").replace("http://", "").strip("/ ")
        host = host.split("/")[0]
        if not host or host == "github.com":
            return "https://github.com", "https://api.github.com"
        return f"https://{host}", f"https://api.{host}"

    token_source = "settings"

    def _token(self) -> str:
        tok = self.setting("token")
        if tok:
            self.token_source = "settings"
            return tok
        gh = which(["gh", "gh.exe"])
        if gh:
            args = [gh, "auth", "token"]
            host = self.setting("host")
            if host:
                args += ["--hostname", host]
            out = run_cmd(args, timeout=10)
            if out and out.strip():
                self.token_source = "gh"
                return out.strip()
        raise NotConfigured("No GitHub login; click Sign in with GitHub in Settings or run `gh auth login`")

    def fetch(self) -> Snapshot:
        _, api = self._hosts()
        headers = {
            "Authorization": f"token {self._token()}",
            "Editor-Version": "vscode/1.96.2",
            "Editor-Plugin-Version": "copilot-chat/0.26.7",
            "User-Agent": "GitHubCopilotChat/0.26.7",
            "X-Github-Api-Version": "2025-04-01",
        }
        try:
            j = http_json(f"{api}/copilot_internal/user", headers)
        except HttpError as e:
            if e.status == 404:
                raise ProviderError("This GitHub account has no Copilot subscription") from None
            if e.status in (401, 403) and self.token_source == "gh":
                raise ProviderError("Copilot didn't accept the GitHub CLI token; use Sign in with GitHub in Settings") from None
            if e.status == 401:
                raise AuthExpired("GitHub token expired or was revoked; Sign in with GitHub again in Settings") from None
            raise
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        if j.get("access_type_sku") == "subscription_ended":
            raise ProviderError("Copilot subscription ended")
        plan = title_case(j.get("copilot_plan") or j.get("access_type_sku") or "")
        snap = Snapshot(plan=plan)
        reset = parse_time(j.get("quota_reset_date") or j.get("limited_user_reset_date"))
        length = 30 * 86400 if reset else None
        snaps = j.get("quota_snapshots") or {}
        for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat"),
                           ("completions", "Completions")):
            q = snaps.get(key)
            if not isinstance(q, dict) or q.get("unlimited"):
                continue
            left = fnum(q.get("percent_remaining"))
            ent, rem = fnum(q.get("entitlement")), fnum(q.get("remaining"))
            if left is None and ent and rem is not None:
                left = rem / ent * 100
            if left is None:
                continue
            detail = f"{ent - rem:,.0f} of {ent:,.0f}" if ent and rem is not None else ""
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, 100 - left)), reset, length, detail))
        # Copilot Free reports counts instead of snapshots.
        if not snap.windows:
            monthly, left_q = j.get("monthly_quotas") or {}, j.get("limited_user_quotas") or {}
            for key, label in (("chat", "Chat"), ("completions", "Completions")):
                total, rem = fnum(monthly.get(key)), fnum(left_q.get(key))
                if total and rem is not None:
                    snap.windows.append(UsageWindow(label, max(0.0, min(100.0, (total - rem) / total * 100)),
                                                    reset, length, f"{total - rem:,.0f} of {total:,.0f}"))
        if not snap.windows:
            if any(isinstance(q, dict) and q.get("unlimited") for q in snaps.values()):
                snap.notes.append("Unlimited on this plan")
            else:
                raise ProviderError("Copilot returned no quota data")
        return snap

    # ----- OAuth device flow, driven by the Settings window
    @classmethod
    def device_start(cls, cfg: dict[str, Any]) -> dict[str, Any]:
        web, _ = cls(cfg)._hosts()
        r = http_json(f"{web}/login/device/code", form={"client_id": CLIENT_ID, "scope": "read:user"})
        if not r.get("device_code"):
            raise ProviderError(f"GitHub didn't start the sign-in: {r.get('error_description') or r}")
        r["_web"] = web
        r["_next"] = time.time() + float(r.get("interval") or 5)
        return r

    @classmethod
    def device_poll(cls, state: dict[str, Any]) -> Optional[str]:
        """Returns the token once the user approves, None while pending. Raises on denial/expiry."""
        r = http_json(f"{state['_web']}/login/oauth/access_token",
                      form={"client_id": CLIENT_ID, "device_code": state["device_code"],
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if r.get("access_token"):
            return str(r["access_token"])
        err = r.get("error")
        if err == "slow_down":
            state["interval"] = float(state.get("interval") or 5) + 5
        elif err not in (None, "authorization_pending"):
            raise ProviderError(r.get("error_description") or err)
        return None
