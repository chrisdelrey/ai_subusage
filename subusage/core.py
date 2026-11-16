"""Shared plumbing: data model, provider base class, HTTP, config, and platform helpers.

Nothing in here imports tkinter, so providers and tests run headless.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import APP, __version__

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW for subprocesses on Windows


# --------------------------------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------------------------------

def config_dir() -> Path:
    override = os.environ.get("SUBUSAGE_CONFIG_DIR")
    if override:
        return Path(override)
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "SubUsageWidget"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "SubUsage"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "subusage"


def app_data_dirs() -> list[Path]:
    """Where desktop apps keep per-user data (VS Code-style apps, JetBrains...)."""
    if IS_WIN:
        return [Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")]
    if IS_MAC:
        return [Path.home() / "Library" / "Application Support"]
    return [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")]


def config_path() -> Path:
    return config_dir() / "config.json"


def log_path() -> Path:
    return config_dir() / "widget.log"


def log(msg: str) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 512_000:
            p.unlink()
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------------

@dataclass
class UsageWindow:
    label: str
    used_pct: Optional[float]           # 0..100
    resets_at: Optional[float] = None   # epoch seconds
    length_s: Optional[float] = None    # window length, enables the pace marker
    detail: str = ""                    # e.g. "$12.40 of $20.00"


@dataclass
class Snapshot:
    plan: str = ""
    windows: list[UsageWindow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fetched_at: Optional[float] = None
    error: str = ""


class ProviderError(Exception):
    """A user-facing problem (expired token, unexpected response...)."""


class NotConfigured(ProviderError):
    """No login/key for this provider on this machine. The widget hides these by default."""


class HttpError(Exception):
    def __init__(self, status: int, body: str, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status}")
        self.status, self.body, self.retry_after = status, body, retry_after


# --------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------

UA = f"{APP}/{__version__}"


def http_json(url: str, headers: Optional[dict[str, str]] = None, *, method: str = "GET",
              json_body: Any = None, form: Optional[dict[str, str]] = None, timeout: float = 15) -> Any:
    """Small JSON-over-HTTPS helper on top of urllib. Raises HttpError / ProviderError."""
    data = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
        method = "POST" if method == "GET" else method
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST" if method == "GET" else method
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        body = ""
        try:
            body = e.read()[:400].decode("utf-8", "replace")
        except Exception:
            pass
        raise HttpError(e.code, body, retry_after) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error: {e.reason}") from None
    except ValueError:
        raise ProviderError("Usage endpoint returned something that isn't JSON") from None


def parse_time(v: Any) -> Optional[float]:
    """ISO-8601 string, epoch seconds or epoch ms -> epoch seconds."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v <= 0:
            return None
        return v / 1000.0 if v > 1e12 else float(v)
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.replace(".", "", 1).isdigit():
            return parse_time(float(s))
        s = s.replace("Z", "+00:00")
        if len(s) == 10:  # date only, e.g. "2026-12-01"
            s += "T00:00:00+00:00"
        # Trim fractional seconds beyond 6 digits (Python < 3.11 can't parse them).
        if "." in s:
            head, _, tail = s.partition(".")
            n = 0
            while n < len(tail) and tail[n].isdigit():
                n += 1
            frac, tz = tail[:n], tail[n:]
            s = f"{head}.{frac[:6].ljust(6, '0')}{tz}" if frac else head + tz
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    return None


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def read_json_file(p: Path, what: str) -> dict[str, Any]:
    if not p.exists():
        raise NotConfigured(f"{what} not found at {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProviderError(f"Couldn't read {p.name}: {e}") from None
    if not isinstance(data, dict):
        raise ProviderError(f"Unexpected format in {p.name}")
    return data


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def fmt_delta(seconds: float) -> str:
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def title_case(s: Any) -> str:
    return str(s).replace("_", " ").replace("-", " ").strip().title() if s else ""


def fnum(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def clamp_pct(v: Optional[float]) -> Optional[float]:
    return None if v is None else max(0.0, min(100.0, v))


PCT_USED_KEYS = ("usedPercent", "used_percent", "percentUsed", "percent_used", "usagePercent", "utilization")
PCT_LEFT_KEYS = ("percentRemaining", "remainingPercent", "remaining_percent", "percent_remaining")
LIMIT_KEYS = ("limit", "max", "maximum", "total", "quota", "entitlement", "max_requests", "request_limit")
USED_KEYS = ("used", "usage", "current", "currentValue", "requests", "consumed", "count")
LEFT_KEYS = ("remaining", "left", "available")
RESET_KEYS = ("resetsAt", "resets_at", "resetAt", "reset_at", "resetTime", "reset_time", "renewsAt",
              "renews_at", "nextRegenAt", "nextResetTime", "next_reset_at", "windowRefreshesAt", "until")


def first_num(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for k in keys:
        v = fnum(d.get(k))
        if v is not None:
            return v
    return None


def normalize_quota(d: Any) -> Optional[tuple[float, Optional[float]]]:
    """Best-effort (used_pct, resets_at) from a loosely-shaped quota object."""
    if not isinstance(d, dict):
        return None
    pct = first_num(d, PCT_USED_KEYS)
    if pct is None:
        left = first_num(d, PCT_LEFT_KEYS)
        if left is not None:
            pct = 100 - left
    if pct is None:
        limit, used, rem = first_num(d, LIMIT_KEYS), first_num(d, USED_KEYS), first_num(d, LEFT_KEYS)
        if limit is None and used is not None and rem is not None:
            limit = used + rem
        if used is None and limit is not None and rem is not None:
            used = limit - rem
        if limit and used is not None and limit > 0:
            pct = used / limit * 100
    if pct is None:
        return None
    reset = None
    for k in RESET_KEYS:
        reset = parse_time(d.get(k))
        if reset:
            break
    return max(0.0, min(100.0, pct)), reset


def run_cmd(args: list[str], timeout: float = 10) -> Optional[str]:
    """Run a command and return stdout, or None if it isn't available / fails."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def which(names: list[str], extra_dirs: tuple[str, ...] = ()) -> Optional[str]:
    """shutil.which, plus common install dirs that GUI-launched apps often miss on macOS/Linux."""
    import shutil
    dirs = list(extra_dirs) + ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")]
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
        for d in dirs:
            p = Path(d) / n
            if p.exists():
                return str(p)
    return None


# --------------------------------------------------------------------------------------------------
# Provider base class
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """A setting shown in the Settings window for a provider.

    kind: "path" (login file, with Find/Browse), "secret" (masked; accepts env:VAR), "text",
          "choice" (options = ((value, label), ...)), "command", "device_login" (button).
    """
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    default: str = ""


class Provider:
    """Subclass this to add a subscription. Implement fetch() and declare the class attributes."""

    # --- catalog metadata (shown in "Add subscription")
    type = "base"                 # unique id, also the config "type"
    default_name = "Provider"     # label in the widget
    vendor = ""                   # e.g. "Anthropic"
    plans = ""                    # e.g. "Pro, Max"
    auth = "auto"                 # "auto" (reads an existing CLI/app login), "key" (paste a key), "command"
    setup = ""                    # one or two sentences: how to make this provider work
    usage_url = ""                # opened on double-click
    accent = "#5aa9e6"            # dot color
    platforms = ("win", "mac", "linux")
    multi = False                 # allow adding more than one instance

    # --- polling
    min_poll = 60
    default_poll = 300

    # --- login file discovery (for auth == "auto" providers that read a file)
    path_key = "auth_path"        # config key holding a custom path
    login_dir = ""                # folder under the home dir, e.g. ".codex"
    login_file = ""               # file inside it, e.g. "auth.json"
    env_var = ""                  # env var that relocates login_dir
    login_what = "login file"

    fields: tuple[Field, ...] = ()

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.name = cfg.get("name") or self.default_name
        self.poll = max(self.min_poll, int(fnum(cfg.get("poll_seconds")) or self.default_poll))
        self.usage_url = cfg.get("usage_url") or type(self).usage_url
        self.snapshot = Snapshot()
        self.configured = True
        self.next_at = 0.0
        self.last_attempt = 0.0
        self.backoff = 0.0
        self.busy = False
        self.lock = threading.Lock()

    # ----- settings access
    def setting(self, key: str, default: str = "") -> str:
        """Config value for key; "env:NAME" values are read from the environment."""
        v = self.cfg.get(key)
        if v is None or v == "":
            for f in self.fields:
                if f.key == key and f.default:
                    v = f.default
                    break
        if v is None:
            return default
        v = str(v).strip()
        if v.startswith("env:"):
            return os.environ.get(v[4:].strip(), "").strip()
        return v or default

    def require_key(self, key: str = "api_key", what: str = "API key") -> str:
        v = self.setting(key)
        if not v:
            raise NotConfigured(f"No {what} set; add it in Settings")
        return v

    @classmethod
    def default_path(cls) -> Optional[Path]:
        if not cls.login_file:
            return None
        base = os.environ.get(cls.env_var) if cls.env_var else None
        return (Path(base) if base else Path.home() / cls.login_dir) / cls.login_file

    def login_path(self) -> Optional[Path]:
        custom = self.setting(self.path_key)
        return expand(custom) if custom else self.default_path()

    # ----- the one method subclasses must implement
    def fetch(self) -> Snapshot:  # pragma: no cover - abstract
        raise NotImplementedError

    def http_error_message(self, e: HttpError) -> str:
        if e.status in (401, 403):
            return "Login expired or rejected; sign in again with the vendor's app or CLI"
        return f"HTTP {e.status} from usage endpoint"

    # ----- scheduler entry point (don't override)
    def run_fetch(self) -> None:
        self.last_attempt = time.time()
        try:
            snap = self.fetch()
            snap.fetched_at = time.time()
            with self.lock:
                self.snapshot, self.configured, self.backoff = snap, True, 0.0
            self.next_at = time.time() + self.poll
        except NotConfigured as e:
            with self.lock:
                self.configured = False
                self.snapshot = Snapshot(error=str(e))
            self.next_at = time.time() + max(self.poll, 600)
        except HttpError as e:
            if e.status == 429:
                self.backoff = min(max(self.backoff * 2, self.poll, e.retry_after or 0), 3600)
                msg = f"Rate limited, retrying in {fmt_delta(self.backoff)}"
            else:
                self.backoff = min(max(self.backoff * 2, self.poll), 1800)
                msg = self.http_error_message(e)
            log(f"{self.name}: HTTP {e.status} {e.body[:200]!r}")
            self._keep_old(msg)
            self.next_at = time.time() + self.backoff
        except ProviderError as e:
            self._keep_old(str(e))
            self.next_at = time.time() + self.poll
        except Exception as e:  # never let one provider take down the widget
            log(f"{self.name}: {traceback.format_exc()}")
            self._keep_old(f"Error: {e}")
            self.next_at = time.time() + self.poll
        finally:
            self.busy = False

    def _keep_old(self, msg: str) -> None:
        with self.lock:
            old = self.snapshot
            self.snapshot = Snapshot(plan=old.plan, windows=old.windows, notes=old.notes,
                                     fetched_at=old.fetched_at, error=msg)


# --------------------------------------------------------------------------------------------------
# Login discovery (Find… in Settings)
# --------------------------------------------------------------------------------------------------

def wsl_distros() -> list[str]:
    """Installed WSL distros (Windows only). `wsl -l -q` prints UTF-16."""
    if not IS_WIN:
        return []
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    raw = out.stdout or b""
    text = raw.decode("utf-16-le", "ignore") if b"\x00" in raw else raw.decode("utf-8", "ignore")
    names = [ln.replace("\x00", "").strip() for ln in text.splitlines()]
    return [n for n in names if n and not n.lower().startswith("docker-desktop")]


def find_login_files(login_dir: str, login_file: str, default: Optional[Path] = None) -> list[Path]:
    """Look for a CLI login file in the home dir and, on Windows, in every WSL distro's home dirs.
    Touching a WSL path starts that distro if it isn't running."""
    found: list[Path] = []
    for cand in (default, Path.home() / login_dir / login_file):
        if cand and cand.exists() and cand not in found:
            found.append(cand)
    for distro in wsl_distros():
        for root in (Path(rf"\\wsl.localhost\{distro}"), Path(rf"\\wsl$\{distro}")):
            try:
                homes = [h for h in (root / "home").iterdir() if h.is_dir()]
            except OSError:
                continue
            homes.append(root / "root")
            for h in homes:
                try:
                    cand = h / login_dir / login_file
                    if cand.exists() and cand not in found:
                        found.append(cand)
                except OSError:
                    pass
            break  # \\wsl.localhost worked; don't double-list via \\wsl$
    return found


# --------------------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------------------

DEFAULT_PROVIDERS = ["claude", "codex", "copilot", "gemini", "cursor", "jetbrains", "nous"]

DEFAULT_CONFIG: dict[str, Any] = {
    "providers": [],          # filled from DEFAULT_PROVIDERS on first run
    "always_on_top": True,
    "compact": False,
    "opacity": 0.95,
    "width": 300,
    "warn_pct": 70,
    "crit_pct": 90,
    "hide_unconfigured": True,
    "x": None,
    "y": None,
}


def default_config() -> dict[str, Any]:
    from .providers import PROVIDERS
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["providers"] = [{"type": t, "enabled": True, "name": PROVIDERS[t].default_name,
                         "poll_seconds": PROVIDERS[t].default_poll} for t in DEFAULT_PROVIDERS if t in PROVIDERS]
    return cfg


def load_config() -> dict[str, Any]:
    cfg = default_config()
    p = config_path()
    try:
        if p.exists():
            user = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        else:
            save_config(cfg)
    except (OSError, ValueError) as e:
        log(f"config load failed: {e}")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        if not IS_WIN:
            os.chmod(tmp, 0o600)  # may hold API keys
        tmp.replace(p)
    except OSError as e:
        log(f"config save failed: {e}")


def build_providers(cfg: dict[str, Any]) -> list[Provider]:
    from .providers import PROVIDERS
    out = []
    for pc in cfg.get("providers", []):
        if not pc.get("enabled", True):
            continue
        cls = PROVIDERS.get(pc.get("type"))
        if cls:
            out.append(cls(pc))
        else:
            log(f"unknown provider type {pc.get('type')!r}")
    return out


# --------------------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------------------

class Scheduler(threading.Thread):
    def __init__(self, providers: list[Provider]):
        super().__init__(daemon=True)
        self.providers = providers
        self.wake = threading.Event()

    def run(self) -> None:
        while True:
            now = time.time()
            for p in list(self.providers):
                if not p.busy and now >= p.next_at:
                    p.busy = True
                    threading.Thread(target=p.run_fetch, daemon=True).start()
            self.wake.wait(5)
            self.wake.clear()

    def refresh_all(self) -> None:
        now = time.time()
        for p in self.providers:
            # A 60 s floor so mashing Refresh can't trip a vendor's 429 penalty box.
            p.next_at = min(p.next_at, max(now, p.last_attempt + 60))
        self.wake.set()


# --------------------------------------------------------------------------------------------------
# Platform integration: start at login, open files, single instance
# --------------------------------------------------------------------------------------------------

def launch_args() -> list[str]:
    """Command that starts the widget without a console window."""
    exe = Path(sys.executable)
    if IS_WIN:
        pyw = exe.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else exe
    launcher = Path(__file__).resolve().parent.parent / "subusage.pyw"
    if launcher.exists():
        return [str(exe), str(launcher)]
    return [str(exe), "-m", "subusage"]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.subusage.widget.plist"


def linux_autostart() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "subusage.desktop"


def startup_enabled() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    if IS_MAC:
        return MAC_PLIST.exists()
    return linux_autostart().exists()


def set_startup(on: bool) -> None:
    args = launch_args()
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, " ".join(f'"{a}"' for a in args))
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except OSError:
                    pass
        return
    if IS_MAC:
        if on:
            from xml.sax.saxutils import escape
            items = "".join(f"<string>{escape(a)}</string>" for a in args)
            MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
            MAC_PLIST.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>'
                '<key>Label</key><string>com.subusage.widget</string>'
                f'<key>ProgramArguments</key><array>{items}</array>'
                '<key>RunAtLoad</key><true/>'
                '<key>ProcessType</key><string>Interactive</string>'
                '</dict></plist>\n', encoding="utf-8")
        elif MAC_PLIST.exists():
            MAC_PLIST.unlink()
        return
    path = linux_autostart()
    if on:
        import shlex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\nName=SubUsage\n"
                        "Comment=AI subscription usage widget\n"
                        f"Exec={' '.join(shlex.quote(a) for a in args)}\n"
                        "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=10\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def open_path(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    if IS_WIN:
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


_instance_sock: Optional[socket.socket] = None


def acquire_single_instance(port: int = 47823) -> bool:
    """True if no other widget is running (holds a localhost port for the process lifetime)."""
    global _instance_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WIN:
        s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _instance_sock = s
    return True


def bg(fn: Callable[[], Any], done: Callable[[Any], None], after: Callable[[int, Callable], Any]) -> None:
    """Run fn off the UI thread, then call done(result_or_exception) via a Tk-style after()."""
    box: dict[str, Any] = {}

    def work():
        try:
            box["r"] = fn()
        except Exception as e:
            box["r"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()

    def poll():
        if t.is_alive():
            after(150, poll)
        else:
            done(box["r"])
    after(150, poll)
