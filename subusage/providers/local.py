"""Subscriptions read from an app or CLI that's already signed in on this computer.

Windsurf: the plan cache in its state database. Kiro / Amazon Q Developer: the CLI's (or Kiro IDE's)
token, sent to AWS's GetUsageLimits. Antigravity: the running app's local language server. Grok: the
`grok` CLI's billing call. CodeRabbit: `coderabbit usage`. All logins are read-only.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..core import (IS_MAC, IS_WIN, NO_WINDOW, AuthExpired, Field, HttpError, NotConfigured, Provider, ProviderError,
                    Snapshot, UsageWindow, app_data_dirs, fnum, http_json, parse_time, pct_of, run_cmd, title_case,
                    which)


def sqlite_value(db: Path, sql: str) -> Optional[str]:
    """One value from an app's SQLite database, opened read-only."""
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(sql).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    v = row[0]
    if isinstance(v, bytes):
        v = v.decode("utf-16-le", "ignore") if b"\x00" in v[:8] else v.decode("utf-8", "ignore")
    return str(v)


def local_data_dirs() -> list[Path]:
    """Where CLIs keep per-user data (XDG data home, Application Support, %LOCALAPPDATA%)."""
    if IS_WIN:
        return [Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")] + app_data_dirs()
    if IS_MAC:
        return [Path.home() / "Library" / "Application Support"]
    return [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")]


# --------------------------------------------------------------------------------------------------
# Windsurf
# --------------------------------------------------------------------------------------------------

class WindsurfProvider(Provider):
    type = "windsurf"
    default_name = "Windsurf"
    vendor = "Cognition"
    plans = "Free, Pro, Teams"
    setup = "Sign in to the Windsurf app. SubUsage reads its cached plan info (read-only); it updates while Windsurf runs."
    usage_url = "https://windsurf.com/subscription/usage"
    accent = "#34d399"
    path_key = "db_path"
    login_what = "Windsurf state database"
    fields = (Field("db_path", "Windsurf state DB", "path", "Blank = auto-detect Windsurf's state.vscdb."),)
    KEY = "windsurf.settings.cachedPlanInfo"

    @classmethod
    def default_path(cls) -> Optional[Path]:
        for base in app_data_dirs():
            for app in ("Windsurf", "Windsurf - Next"):
                p = base / app / "User" / "globalStorage" / "state.vscdb"
                if p.exists():
                    return p
        return app_data_dirs()[0] / "Windsurf" / "User" / "globalStorage" / "state.vscdb"

    def watch_paths(self) -> list[Path]:
        db = self.login_path()
        return [db, db.with_name(db.name + "-wal")] if db else []

    def fetch(self) -> Snapshot:
        db = self.login_path()
        if not db or not db.exists():
            raise NotConfigured(f"Windsurf not found at {db}")
        raw = sqlite_value(db, f"SELECT value FROM ItemTable WHERE key = '{self.KEY}' LIMIT 1")
        if not raw:
            raise NotConfigured("Windsurf has no cached plan yet; sign in to Windsurf")
        try:
            return self.parse(json.loads(raw))
        except ValueError:
            raise ProviderError("Couldn't read Windsurf's cached plan") from None

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        snap = Snapshot(plan=str(j.get("planName") or ""))
        q = j.get("quotaUsage") if isinstance(j.get("quotaUsage"), dict) else {}
        for key, reset_key, label, length in (("dailyRemainingPercent", "dailyResetAtUnix", "Daily", 86400),
                                              ("weeklyRemainingPercent", "weeklyResetAtUnix", "Weekly", 7 * 86400)):
            left = fnum(q.get(key))
            if left is not None:
                snap.windows.append(UsageWindow(label, max(0.0, min(100.0, 100 - left)), parse_time(q.get(reset_key)),
                                                length))
        u = j.get("usage") if isinstance(j.get("usage"), dict) else {}
        end = parse_time(j.get("endTimestamp"))
        start = parse_time(j.get("startTimestamp"))
        if not snap.windows:
            for total_k, used_k, left_k, label in (("messages", "usedMessages", "remainingMessages", "Prompt credits"),
                                                   ("flowActions", "usedFlowActions", "remainingFlowActions",
                                                    "Flow actions"),
                                                   ("flexCredits", "usedFlexCredits", "remainingFlexCredits",
                                                    "Flex credits")):
                total = fnum(u.get(total_k))
                used = fnum(u.get(used_k))
                if used is None and total and fnum(u.get(left_k)) is not None:
                    used = total - float(u[left_k])
                if pct_of(used, total) is not None:
                    snap.windows.append(UsageWindow(label, pct_of(used, total), end,
                                                    (end - start) if start and end else None,
                                                    f"{used:,.0f} of {total:,.0f}"))
        if not snap.windows:
            raise ProviderError("Windsurf's cached plan has no usage yet")
        return snap


# --------------------------------------------------------------------------------------------------
# Kiro and Amazon Q Developer (AWS GetUsageLimits)
# --------------------------------------------------------------------------------------------------

class KiroProvider(Provider):
    type = "kiro"
    default_name = "Kiro"
    vendor = "AWS"
    plans = "Free, Pro, Pro+, Power"
    setup = "Sign in with kiro-cli (`kiro-cli login`) or the Kiro IDE. SubUsage reads that login read-only."
    usage_url = "https://app.kiro.dev/account/usage"
    accent = "#7c3aed"
    login_what = "Kiro login"
    fields = (Field("auth_path", "Login database", "path", "Blank = auto-detect kiro-cli's data.sqlite3."),)
    APP_DIR = "kiro-cli"
    TOKEN_KEY = "kirocli:odic:token"
    IDE_TOKEN = Path(".aws") / "sso" / "cache" / "kiro-auth-token.json"
    ENDPOINTS = {"us-east-1": "https://codewhisperer.us-east-1.amazonaws.com/",
                 "eu-central-1": "https://q.eu-central-1.amazonaws.com/"}

    @classmethod
    def default_path(cls) -> Optional[Path]:
        env = os.environ.get("KIRO_DATA_DIR") if cls.APP_DIR == "kiro-cli" else None
        if env:
            return Path(env) / "data.sqlite3"
        cands = [d / cls.APP_DIR / "data.sqlite3" for d in local_data_dirs()]
        return next((c for c in cands if c.exists()), cands[0])

    def watch_paths(self) -> list[Path]:
        db = self.login_path()
        return ([db] if db else []) + ([Path.home() / self.IDE_TOKEN] if self.IDE_TOKEN else [])

    def identity(self) -> tuple[str, Optional[str]]:
        db = self.login_path()
        if db and db.exists():
            tok = sqlite_value(db, f"SELECT value FROM auth_kv WHERE key = '{self.TOKEN_KEY}'")
            if tok:
                try:
                    t = json.loads(tok)
                except ValueError:
                    t = {}
                access = t.get("access_token") or t.get("accessToken")
                if access:
                    prof = sqlite_value(db, "SELECT value FROM state WHERE key = 'api.codewhisperer.profile'")
                    arn = None
                    try:
                        arn = (json.loads(prof) or {}).get("arn") if prof else None
                    except ValueError:
                        pass
                    exp = parse_time(t.get("expires_at") or t.get("expiresAt"))
                    if exp and exp < time.time():
                        raise AuthExpired(f"{self.default_name} login expired; it refreshes the next time you use it")
                    return str(access), arn
        if self.IDE_TOKEN:
            p = Path.home() / self.IDE_TOKEN
            if p.exists():
                try:
                    t = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    t = {}
                if t.get("accessToken"):
                    exp = parse_time(t.get("expiresAt"))
                    if exp and exp < time.time():
                        raise AuthExpired("Kiro login expired; it refreshes the next time you use Kiro")
                    return str(t["accessToken"]), t.get("profileArn")
        raise NotConfigured(f"No {self.default_name} login found; {self.setup[:1].lower()}{self.setup[1:]}")

    def endpoint(self, arn: Optional[str]) -> str:
        if not arn:
            return self.ENDPOINTS["us-east-1"]
        parts = arn.split(":")
        if len(parts) == 6 and parts[2] == "codewhisperer" and parts[3] in self.ENDPOINTS:
            return self.ENDPOINTS[parts[3]]
        raise ProviderError(f"Unsupported {self.default_name} profile region")

    def fetch(self) -> Snapshot:
        token, arn = self.identity()
        try:
            j = http_json(self.endpoint(arn), {"Authorization": f"Bearer {token}",
                                               "Content-Type": "application/x-amz-json-1.0",
                                               "X-Amz-Target": "AmazonCodeWhispererService.GetUsageLimits"},
                          method="POST", json_body={"profileArn": arn} if arn else {})
        except HttpError as e:
            if e.status in (401, 403):
                raise AuthExpired(f"{self.default_name} login expired; it refreshes the next time you use it") from None
            raise
        return self.parse(j)

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        rows = [r for r in (j.get("usageBreakdownList") or []) if isinstance(r, dict)]
        sub = j.get("subscriptionInfo") if isinstance(j.get("subscriptionInfo"), dict) else {}
        snap = Snapshot(plan=title_case(sub.get("subscriptionTitle") or sub.get("type") or ""))
        default_reset = parse_time(j.get("nextDateReset"))
        names = {"CREDIT": "Credits", "AGENTIC_REQUEST": "Agentic requests", "TRANSFORM": "Code transformation"}
        for r in rows:
            limit = fnum(r.get("usageLimitWithPrecision", r.get("usageLimit")))
            used = fnum(r.get("currentUsageWithPrecision", r.get("currentUsage")))
            over = fnum(r.get("currentOveragesWithPrecision", r.get("currentOverages"))) or 0
            if limit is None or used is None or limit <= 0:
                continue
            plan_used = max(0.0, used - over)
            rt = str(r.get("resourceType") or "")
            label = names.get(rt, title_case(rt.lower()) or "Usage") + " (mo)"
            reset = parse_time(r.get("nextDateReset")) or default_reset
            snap.windows.append(UsageWindow(label, pct_of(plan_used, limit), reset, 30 * 86400 if reset else None,
                                            f"{plan_used:,.1f} of {limit:,.0f}"))
            if over:
                snap.notes.append(f"Overage: {over:,.1f} {names.get(rt, 'units').lower()}")
            bonuses = [b for b in (r.get("bonuses") or r.get("freeTrialInfo") and [r["freeTrialInfo"]] or [])
                       if isinstance(b, dict)]
            for b in bonuses:
                bl = fnum(b.get("usageLimitWithPrecision", b.get("usageLimit")))
                bu = fnum(b.get("currentUsageWithPrecision", b.get("currentUsage")))
                if pct_of(bu, bl) is not None:
                    snap.windows.append(UsageWindow("Bonus credits", pct_of(bu, bl),
                                                    parse_time(b.get("expiresAt") or b.get("freeTrialExpiry")), None,
                                                    f"{bu:,.1f} of {bl:,.0f}"))
        if not snap.windows:
            raise ProviderError("AWS returned no usage limits for this login")
        return snap


class AmazonQProvider(KiroProvider):
    type = "amazonq"
    default_name = "Amazon Q Developer"
    vendor = "AWS"
    plans = "Free, Pro"
    setup = "Sign in with the Amazon Q CLI (`q login`). SubUsage reads that login read-only."
    usage_url = "https://console.aws.amazon.com/amazonq/developer"
    accent = "#ff9900"
    login_what = "Amazon Q login"
    fields = (Field("auth_path", "Login database", "path", "Blank = auto-detect the Amazon Q CLI's data.sqlite3."),)
    APP_DIR = "amazon-q"
    TOKEN_KEY = "codewhisperer:odic:token"
    IDE_TOKEN = None  # type: ignore[assignment]


# --------------------------------------------------------------------------------------------------
# Antigravity (Google)
# --------------------------------------------------------------------------------------------------

class AntigravityProvider(Provider):
    type = "antigravity"
    default_name = "Antigravity"
    vendor = "Google"
    plans = "Free, Pro, Ultra"
    setup = ("Keep the Antigravity app open and signed in. SubUsage asks its local language server for your quota "
             "(nothing leaves this computer except Antigravity's own traffic).")
    usage_url = "https://antigravity.google"
    accent = "#4285f4"
    parks_on_auth = False
    min_poll = 120
    fields = ()
    PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

    @staticmethod
    def _flag(cmd: str, name: str) -> Optional[str]:
        m = re.search(rf"{re.escape(name)}[=\s]+(\S+)", cmd)
        return m.group(1).strip("'\"") if m else None

    def processes(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        if IS_WIN:
            ps = run_cmd(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name like 'language_server%'\" | "
                          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"], timeout=20)
            try:
                rows = json.loads(ps or "[]")
            except ValueError:
                rows = []
            for r in rows if isinstance(rows, list) else [rows]:
                if isinstance(r, dict) and r.get("CommandLine"):
                    out.append((int(r.get("ProcessId") or 0), str(r["CommandLine"])))
        else:
            ps = run_cmd(["ps", "-axo", "pid=,command="] if IS_MAC else ["ps", "-eo", "pid=,args="], timeout=10)
            for line in (ps or "").splitlines():
                pid, _, cmd = line.strip().partition(" ")
                if pid.isdigit():
                    out.append((int(pid), cmd))
        return [(pid, cmd) for pid, cmd in out
                if "language_server" in cmd and "--csrf_token" in cmd
                and ("antigravity" in cmd.lower() or "--app_data_dir" in cmd)]

    def ports(self, pid: int, cmd: str) -> list[int]:
        found: list[int] = []
        if IS_WIN:
            out = run_cmd(["powershell", "-NoProfile", "-Command",
                           f"(Get-NetTCPConnection -State Listen -OwningProcess {pid}).LocalPort -join ','"], timeout=20)
            found = [int(p) for p in re.findall(r"\d+", out or "")]
        else:
            lsof = which(["lsof"], ("/usr/sbin", "/usr/bin"))
            if lsof:
                out = run_cmd([lsof, "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"], timeout=10)
                found = [int(p) for p in re.findall(r"(?:127\.0\.0\.1|localhost|\*|\[::1\]):(\d+)\s+\(LISTEN\)", out or "")]
            if not found and not IS_MAC:
                out = run_cmd(["ss", "-ltnpH"], timeout=10)
                for line in (out or "").splitlines():
                    if f"pid={pid}," in line:
                        m = re.search(r":(\d+)\s", line)
                        if m:
                            found.append(int(m.group(1)))
        ext = self._flag(cmd, "--extension_server_port")
        return [p for p in dict.fromkeys(found) if str(p) != ext]

    def fetch(self) -> Snapshot:
        procs = self.processes()
        if not procs:
            raise NotConfigured("Antigravity isn't running (its quota comes from the open app)")
        body = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "ideVersion": "unknown",
                             "locale": "en"}}
        last: Optional[Exception] = None
        for pid, cmd in procs:
            token = self._flag(cmd, "--csrf_token")
            for port in self.ports(pid, cmd):
                for scheme in ("https", "http"):
                    try:
                        j = http_json(f"{scheme}://127.0.0.1:{port}{self.PATH}",
                                      {"X-Codeium-Csrf-Token": token or "", "Connect-Protocol-Version": "1"},
                                      json_body=body, timeout=5, local_tls=True)
                        return self.parse(j)
                    except (HttpError, ProviderError) as e:
                        last = e
        raise ProviderError(f"Couldn't reach Antigravity's language server ({last})" if last
                            else "Couldn't find Antigravity's language server port")

    @staticmethod
    def parse(j: dict[str, Any]) -> Snapshot:
        us = j.get("userStatus") if isinstance(j.get("userStatus"), dict) else None
        if not us:
            raise ProviderError(f"Antigravity: {j.get('message') or 'no user status'}")
        info = ((us.get("planStatus") or {}).get("planInfo") or {}) if isinstance(us.get("planStatus"), dict) else {}
        plan = next((info.get(k) for k in ("planDisplayName", "displayName", "planName", "productName") if info.get(k)),
                    "") or ((us.get("userTier") or {}).get("name") if isinstance(us.get("userTier"), dict) else "")
        snap = Snapshot(plan=str(plan or ""))
        cfgs = ((us.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []) \
            if isinstance(us.get("cascadeModelConfigData"), dict) else []
        seen = set()
        for c in cfgs:
            q = c.get("quotaInfo") if isinstance(c, dict) else None
            if not isinstance(q, dict) or fnum(q.get("remainingFraction")) is None:
                continue
            label = str(c.get("label") or "Model")
            if label in seen:
                continue
            seen.add(label)
            snap.windows.append(UsageWindow(label, max(0.0, min(100.0, (1 - float(q["remainingFraction"])) * 100)),
                                            parse_time(q.get("resetTime")), 5 * 3600))
        if not snap.windows:
            raise ProviderError("Antigravity reported no model quotas")
        return snap


# --------------------------------------------------------------------------------------------------
# Grok (xAI) via the grok CLI
# --------------------------------------------------------------------------------------------------

def jsonrpc_session(args: list[str], requests: list[dict[str, Any]], want_id: int, timeout: float = 20) -> dict:
    """Start a newline-delimited JSON-RPC server (stdin/stdout), send requests, return the reply to want_id."""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, creationflags=NO_WINDOW)
    except OSError as e:
        raise ProviderError(f"Couldn't start {Path(args[0]).name}: {e}") from None
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=reader, daemon=True).start()
    try:
        assert proc.stdin is not None
        deadline = time.time() + timeout
        for r in requests:  # one at a time: each waits for its reply, like the vendors' own clients
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", **r}) + "\n")
            proc.stdin.flush()
            while True:
                try:
                    line = lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    raise ProviderError(f"{Path(args[0]).name} didn't answer in time") from None
                if line is None:
                    raise ProviderError(f"{Path(args[0]).name} exited without answering")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == r.get("id"):
                    if r.get("id") == want_id:
                        return msg
                    if msg.get("error"):
                        return msg
                    break
        raise ProviderError(f"{Path(args[0]).name} didn't answer")
    finally:
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.SubprocessError):
            pass
        for f in (proc.stdin, proc.stdout):
            try:
                f and f.close()
            except OSError:
                pass


class GrokProvider(Provider):
    type = "grok"
    default_name = "Grok"
    vendor = "xAI"
    plans = "SuperGrok, SuperGrok Heavy"
    setup = "Install the Grok CLI and run `grok login`. SubUsage asks it for your billing usage."
    usage_url = "https://grok.com/?_s=usage"
    accent = "#e5e7eb"
    login_dir, login_file, login_what = ".grok", "auth.json", "Grok login"
    fields = (Field("auth_path", "Login file", "path", "Blank = ~/.grok/auth.json (only watched, never read)."),)
    min_poll = 300

    def fetch(self) -> Snapshot:
        exe = which(["grok.cmd", "grok"] if IS_WIN else ["grok"])
        if not exe:
            raise NotConfigured("Grok CLI not found; install it and run `grok login`")
        p = self.login_path()
        if p and not p.exists():
            raise NotConfigured("No Grok login; run `grok login`")
        msg = jsonrpc_session([exe, "agent", "stdio"], [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "1", "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}},
            {"id": 2, "method": "_x.ai/billing", "params": {}},
        ], want_id=2)
        if msg.get("error"):
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]}
            text = str(err.get("message") or "error")
            if re.search(r"auth|login|unauthor", text, re.I):
                raise AuthExpired("Grok login expired; run `grok login`")
            raise ProviderError(f"Grok: {text}")
        return self.parse(msg.get("result") or {})

    @staticmethod
    def parse(r: dict[str, Any]) -> Snapshot:
        cents = lambda v: fnum(v.get("val")) if isinstance(v, dict) else fnum(v)  # noqa: E731
        cfg = r.get("config") if isinstance(r.get("config"), dict) else None
        if cfg:
            tier = cfg.get("subscriptionTier") or r.get("subscription_tier") or r.get("subscriptionTier") or ""
            snap = Snapshot(plan=title_case(str(tier).replace("SUBSCRIPTION_TIER_", "")))
            period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
            end = parse_time(period.get("end")) or parse_time(cfg.get("billingPeriodEnd"))
            start = parse_time(period.get("start")) if period.get("end") else parse_time(cfg.get("billingPeriodStart"))
            pct = fnum(cfg.get("creditUsagePercent"))
            if pct is None:
                pct = pct_of(cents(cfg.get("onDemandUsed")), cents(cfg.get("onDemandCap")))
            if pct is not None:
                snap.windows.append(UsageWindow("Credits", max(0.0, min(100.0, pct)), end,
                                                (end - start) if start and end and end > start else None))
            bal = cfg.get("prepaidBalance")
            usd = fnum(bal.get("usd")) if isinstance(bal, dict) else None
            if usd:
                snap.notes.append(f"Prepaid balance: ${usd / 100:,.2f}")
            if snap.windows or snap.notes:
                return snap
        limit, usage = cents(r.get("monthlyLimit")), r.get("usage") if isinstance(r.get("usage"), dict) else {}
        used = cents(usage.get("includedUsed")) if usage else None
        if pct_of(used, limit) is not None:
            cyc = r.get("billingCycle") if isinstance(r.get("billingCycle"), dict) else {}
            end = parse_time(cyc.get("billingPeriodEnd"))
            return Snapshot(windows=[UsageWindow("Monthly", pct_of(used, limit), end, 30 * 86400 if end else None,
                                                 f"${used / 100:,.2f} of ${limit / 100:,.2f}")])
        raise ProviderError("Grok returned no billing usage")


# --------------------------------------------------------------------------------------------------
# CodeRabbit
# --------------------------------------------------------------------------------------------------

class CodeRabbitProvider(Provider):
    type = "coderabbit"
    default_name = "CodeRabbit"
    vendor = "CodeRabbit"
    plans = "Lite, Pro"
    setup = "Install the CodeRabbit CLI and run `coderabbit auth login`. SubUsage runs `coderabbit usage`."
    usage_url = "https://app.coderabbit.ai/settings/subscription"
    accent = "#ff570a"
    parks_on_auth = False
    min_poll = 600
    default_poll = 900
    fields = ()

    def fetch(self) -> Snapshot:
        exe = which(["coderabbit.cmd", "coderabbit", "cr"] if IS_WIN else ["coderabbit", "cr"])
        if not exe:
            raise NotConfigured("CodeRabbit CLI not found")
        try:
            out = subprocess.run([exe, "usage"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            raise ProviderError(f"`coderabbit usage` failed: {e}") from None
        return self.parse((out.stdout or "") + "\n" + (out.stderr or ""))

    @staticmethod
    def parse(text: str) -> Snapshot:
        t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
        fields: dict[str, str] = {}
        for line in t.splitlines():
            k, sep, v = line.partition(":")
            if sep and v.strip() and k.strip().lower() not in fields:
                fields[k.strip().lower()] = v.strip()
        reviews = fields.get("your reviews")
        if reviews is None and "usage billing" not in fields and "period resets" not in fields:
            if re.search(r"not authenticated|please log in|auth login|unauthorized|no session", t, re.I):
                raise AuthExpired("CodeRabbit CLI isn't signed in; run `coderabbit auth login`")
            raise ProviderError("Couldn't read `coderabbit usage`")
        snap = Snapshot(plan=fields.get("plan", ""))
        reset = parse_time(fields.get("period resets"))
        if reviews is not None:
            snap.windows.append(UsageWindow("Reviews", None, reset, None, f"{reviews} this period"))
        if fields.get("usage billing"):
            snap.notes.append(f"Billing: {fields['usage billing']}")
        return snap
