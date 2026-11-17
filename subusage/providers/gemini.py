"""Google Gemini (Gemini CLI quotas: free, Google AI Pro / Ultra, Code Assist), via the quota API
Gemini CLI itself uses.

Google access tokens last an hour. When the saved one has expired, the widget refreshes it *in memory*
with the OAuth client ID/secret shipped inside your installed Gemini CLI. Nothing is written back to
~/.gemini, so the CLI's own login is untouched.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..core import (IS_WIN, AuthExpired, Field, HttpError, Provider, ProviderError, Snapshot, UsageWindow, fnum, http_json,
                    log, parse_time, read_json_file, run_cmd, which)

BASE = "https://cloudcode-pa.googleapis.com/v1internal"
_ID_RE = re.compile(r"""OAUTH_CLIENT_ID\s*=\s*['"]([^'"]+)['"]""")
_SECRET_RE = re.compile(r"""OAUTH_CLIENT_SECRET\s*=\s*['"]([^'"]+)['"]""")


def _npm_roots() -> list[Path]:
    roots: list[Path] = []
    npm = which(["npm.cmd", "npm"] if IS_WIN else ["npm"])
    if npm:
        out = run_cmd([npm, "root", "-g"], timeout=15)
        if out and out.strip():
            roots.append(Path(out.strip()))
    if IS_WIN:
        roots.append(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules")
    roots += [Path("/opt/homebrew/lib/node_modules"), Path("/usr/local/lib/node_modules"),
              Path("/usr/lib/node_modules"), Path.home() / ".npm-global/lib/node_modules",
              Path.home() / ".bun/install/global/node_modules"]
    for cellar in (Path("/opt/homebrew/opt/gemini-cli"), Path("/usr/local/opt/gemini-cli")):
        roots.append(cellar / "libexec/lib/node_modules")
    return [r for r in roots if r.is_dir()]


def find_client_credentials() -> Optional[tuple[str, str]]:
    env_id, env_secret = os.environ.get("GEMINI_OAUTH_CLIENT_ID"), os.environ.get("GEMINI_OAUTH_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret
    for root in _npm_roots():
        pkg = root / "@google" / "gemini-cli"
        if not pkg.is_dir():
            continue
        candidates = list(pkg.glob("node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js"))
        candidates += list((root / "@google").glob("gemini-cli-core/dist/src/code_assist/oauth2.js"))
        candidates += list(pkg.glob("bundle/*.js")) + list(pkg.glob("dist/*.js"))
        for f in candidates:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m1, m2 = _ID_RE.search(text), _SECRET_RE.search(text)
            if m1 and m2:
                return m1.group(1), m2.group(1)
    return None


class GeminiProvider(Provider):
    type = "gemini"
    default_name = "Gemini"
    vendor = "Google"
    plans = "Free, Google AI Pro, Ultra, Code Assist"
    setup = "Install Gemini CLI (npm i -g @google/gemini-cli) and sign in with Google. Shows the CLI's daily model quotas."
    usage_url = "https://aistudio.google.com/usage"
    accent = "#4285f4"

    login_dir, login_file, env_var, login_what = ".gemini", "oauth_creds.json", "GEMINI_DIR", "Gemini CLI login"
    fields = (Field("auth_path", "Login file", "path", "Blank = default (~/.gemini/oauth_creds.json)."),)

    _cache: dict[str, tuple[str, float]] = {}   # refresh_token -> (access_token, expiry)
    _project: dict[str, tuple[Optional[str], str]] = {}  # access token prefix -> (project, tier)

    def _access_token(self) -> str:
        creds = read_json_file(self.login_path(), self.login_what)
        token, expiry = creds.get("access_token"), parse_time(creds.get("expiry_date"))
        if token and expiry and expiry - 60 > time.time():
            return str(token)
        refresh = creds.get("refresh_token")
        if not refresh:
            if token and not expiry:
                return str(token)
            raise AuthExpired("Login expired; it refreshes the next time you use Gemini CLI")
        cached = self._cache.get(refresh)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        client = find_client_credentials()
        if not client:
            raise ProviderError("Gemini token expired and Gemini CLI's OAuth client wasn't found; "
                                "run `gemini` once, or set GEMINI_OAUTH_CLIENT_ID/SECRET")
        try:
            r = http_json("https://oauth2.googleapis.com/token",
                          form={"client_id": client[0], "client_secret": client[1],
                                "refresh_token": refresh, "grant_type": "refresh_token"})
        except HttpError as e:
            if e.status in (400, 401):  # invalid_grant: refresh token revoked/expired
                raise AuthExpired("Gemini sign-in expired; run `gemini` to sign in again") from None
            raise
        new = r.get("access_token")
        if not new:
            raise AuthExpired("Google didn't refresh the Gemini token; run `gemini` to sign in again")
        self._cache[refresh] = (new, time.time() + float(fnum(r.get("expires_in")) or 3000))
        return new

    def _project_and_tier(self, token: str) -> tuple[Optional[str], str]:
        key = token[:24]
        if key in self._project:
            return self._project[key]
        project, tier = os.environ.get("GOOGLE_CLOUD_PROJECT"), ""
        try:
            r = http_json(f"{BASE}:loadCodeAssist", {"Authorization": f"Bearer {token}"},
                          json_body={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}})
            project = project or r.get("cloudaicompanionProject") or None
            if isinstance(project, dict):
                project = project.get("id")
            cur = r.get("currentTier") or {}
            tier = str(cur.get("name") or cur.get("id") or "")
        except (HttpError, ProviderError) as e:
            log(f"gemini loadCodeAssist failed: {e}")
        self._project[key] = (project, tier)
        return project, tier

    @staticmethod
    def family(model: str) -> Optional[str]:
        m = model.lower()
        if "flash-lite" in m or "flash_lite" in m:
            return "Flash Lite"
        if "flash" in m:
            return "Flash"
        if "pro" in m:
            return "Pro"
        return None

    def fetch(self) -> Snapshot:
        token = self._access_token()
        project, tier = self._project_and_tier(token)
        body: dict[str, Any] = {"project": project} if project else {}
        j = http_json(f"{BASE}:retrieveUserQuota", {"Authorization": f"Bearer {token}"}, json_body=body)
        return self.parse(j, tier)

    @classmethod
    def parse(cls, j: dict[str, Any], tier: str = "") -> Snapshot:
        worst: dict[str, tuple[float, Optional[float]]] = {}
        for b in j.get("buckets") or []:
            if not isinstance(b, dict):
                continue
            fam = cls.family(str(b.get("modelId") or ""))
            frac = fnum(b.get("remainingFraction"))
            if fam is None or frac is None:
                continue
            if fam not in worst or frac < worst[fam][0]:
                worst[fam] = (frac, parse_time(b.get("resetTime")))
        snap = Snapshot(plan=tier.replace("Gemini Code Assist", "Code Assist").strip())
        for fam in ("Pro", "Flash", "Flash Lite"):
            if fam in worst:
                frac, reset = worst[fam]
                snap.windows.append(UsageWindow(f"{fam} (daily)", max(0.0, min(100.0, (1 - frac) * 100)),
                                                reset, 86400))
        if not snap.windows:
            raise ProviderError("Gemini returned no quota buckets for this account")
        return snap
