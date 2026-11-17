"""Version/build info and self-update from the GitHub repo.

Checks are read-only and automatic (once a day, can be turned off). Installing an update only ever
happens when the user clicks "Update now", using whichever method matches how SubUsage was installed:

  git     a `git clone` checkout       ->  git pull --ff-only
  pip     pip / pipx install           ->  pip install --upgrade git+https://github.com/<repo>.git@<ref>
  source  an unzipped download         ->  download the release zip from GitHub and replace the files
"""
from __future__ import annotations

import base64
import io
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import __version__
from .core import NO_WINDOW, UA, HttpError, ProviderError, http_json, log, run_cmd, which

REPO = "chrisdelrey/ai_subusage"
API = f"https://api.github.com/repos/{REPO}"
REPO_URL = f"https://github.com/{REPO}"
TOP_LEVEL_FILES = ("subusage.pyw", "README.md", "LICENSE", "PROVIDERS.md", "CONTRIBUTING.md", "pyproject.toml")
TOP_LEVEL_DIRS = ("examples", "docs")


def parse_version(s: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", (s or "").lstrip("vV").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - min(3, len(nums)))


def install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def install_kind(root: Optional[Path] = None) -> str:
    root = root or install_root()
    if (root / ".git").exists():
        return "git"
    if (root / "subusage.pyw").exists():
        return "source"
    return "pip"


def _git() -> Optional[str]:
    return which(["git", "git.exe"])


@lru_cache(maxsize=1)
def build_id() -> str:
    """Short commit for git checkouts (with + if modified), the ref of the last in-app update, or ''."""
    root = install_root()
    if install_kind(root) == "git" and _git():
        sha = (run_cmd([_git(), "-C", str(root), "rev-parse", "--short", "HEAD"]) or "").strip()
        if sha:
            dirty = (run_cmd([_git(), "-C", str(root), "status", "--porcelain", "--untracked-files=no"]) or "").strip()
            return sha + ("+" if dirty else "")
    marker = Path(__file__).resolve().parent / "BUILD"
    if marker.exists():
        try:
            return marker.read_text(encoding="utf-8").strip()[:40]
        except OSError:
            pass
    return ""


def version_string() -> str:
    b = build_id()
    return f"{__version__} ({b})" if b else __version__


@dataclass
class UpdateInfo:
    current: str
    latest: str
    ref: str            # tag or branch to install
    zip_url: str
    url: str            # page to show the user
    notes: str = ""
    checked_at: float = 0.0

    @property
    def available(self) -> bool:
        return parse_version(self.latest) > parse_version(self.current)


def check(timeout: float = 15) -> UpdateInfo:
    """Latest GitHub release; falls back to the version on main when the repo has no releases yet."""
    now = time.time()
    try:
        r = http_json(f"{API}/releases/latest", timeout=timeout)
        tag = str(r.get("tag_name") or "")
        if tag:
            return UpdateInfo(__version__, tag.lstrip("vV"), tag, r.get("zipball_url") or f"{API}/zipball/{tag}",
                              r.get("html_url") or f"{REPO_URL}/releases", str(r.get("body") or "")[:4000], now)
    except HttpError as e:
        if e.status != 404:
            raise
    c = http_json(f"{API}/contents/subusage/__init__.py?ref=main", timeout=timeout)
    try:
        text = base64.b64decode(c.get("content") or "").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ProviderError("Couldn't read the version on GitHub") from None
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    if not m:
        raise ProviderError("Couldn't find a version on GitHub")
    return UpdateInfo(__version__, m.group(1), "main", f"{API}/zipball/main", f"{REPO_URL}/commits/main", "", now)


# --------------------------------------------------------------------------------------------------
# Applying updates
# --------------------------------------------------------------------------------------------------

def _run(args: list[str], cwd: Optional[Path] = None, timeout: float = 300) -> str:
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProviderError(f"Couldn't run {Path(args[0]).name}: {e}") from None
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        raise ProviderError(msg[-1] if msg else f"{Path(args[0]).name} failed ({out.returncode})")
    return out.stdout


def _apply_git(root: Path) -> str:
    git = _git()
    if not git:
        raise ProviderError("git isn't installed; download the latest release instead")
    if _run([git, "-C", str(root), "status", "--porcelain", "--untracked-files=no"]).strip():
        raise ProviderError("You have local changes in the SubUsage folder; commit or stash them, then update")
    out = _run([git, "-C", str(root), "pull", "--ff-only"])
    return "Already up to date" if "Already up to date" in out else "Updated with git pull"


def _apply_pip(info: UpdateInfo) -> str:
    _run([sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
          f"git+{REPO_URL}.git@{info.ref}"], timeout=600)
    return f"Installed {info.latest} with pip"


def _download(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # follows the redirect to codeload.github.com
        return r.read()


def _apply_source(info: UpdateInfo, root: Path, data: Optional[bytes] = None) -> str:
    data = data if data is not None else _download(info.zip_url)
    with tempfile.TemporaryDirectory(prefix="subusage-update-") as tmp:
        tmpdir = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():  # refuse path traversal
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ProviderError("Update archive looks unsafe; not installing")
            zf.extractall(tmpdir)
        tops = [p for p in tmpdir.iterdir() if p.is_dir()]
        new = tops[0] if len(tops) == 1 else tmpdir
        if not (new / "subusage" / "__init__.py").exists():
            raise ProviderError("Update archive doesn't contain SubUsage")
        pkg, backup = root / "subusage", root / f"subusage.old-{int(time.time())}"
        pkg.rename(backup)
        try:
            shutil.copytree(new / "subusage", pkg, ignore=shutil.ignore_patterns("__pycache__"))
            (pkg / "BUILD").write_text(info.ref, encoding="utf-8")
            for f in TOP_LEVEL_FILES:
                if (new / f).exists():
                    shutil.copy2(new / f, root / f)
            for d in TOP_LEVEL_DIRS:
                if (new / d).is_dir():
                    shutil.copytree(new / d, root / d, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(pkg, ignore_errors=True)
            backup.rename(pkg)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    return f"Installed {info.latest}"


def apply(info: UpdateInfo) -> str:
    root = install_root()
    kind = install_kind(root)
    log(f"update: applying {info.ref} via {kind}")
    if kind == "git":
        msg = _apply_git(root)
    elif kind == "pip":
        msg = _apply_pip(info)
    else:
        msg = _apply_source(info, root)
    build_id.cache_clear()
    return msg


def restart() -> None:
    """Start a fresh copy of the widget. The caller should quit right after."""
    from . import core
    if core._instance_sock is not None:
        core._instance_sock.close()
        core._instance_sock = None
    kw: dict = {"close_fds": True}
    if core.IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(core.launch_args(), **kw)
