"""Test helpers: an isolated fake home directory and a fake http_json."""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable


def jwt(claims: dict[str, Any]) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'none'})}.{b(claims)}.sig"


class FakeHTTP:
    """Stand-in for core.http_json. routes: {url_substring: response | exception | callable}."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, headers=None, *, method="GET", json_body=None, form=None, timeout=15, **kw):
        self.calls.append({"url": url, "headers": headers or {}, "json": json_body, "form": form, "kw": kw})
        for key, resp in self.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, Callable):  # type: ignore[arg-type]
                    return resp(url, json_body, form)
                if isinstance(resp, str):  # http_text routes
                    return resp
                return json.loads(json.dumps(resp))
        raise AssertionError(f"unexpected URL {url}")

    def last(self, key: str) -> dict[str, Any]:
        return [c for c in self.calls if key in c["url"]][-1]


class HomeTestCase(unittest.TestCase):
    """Each test gets a throwaway HOME / APPDATA / XDG_CONFIG_HOME and config dir."""

    ENV = ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "SUBUSAGE_CONFIG_DIR", "CLAUDE_CONFIG_DIR",
           "CODEX_HOME", "HERMES_HOME", "GEMINI_DIR", "GEMINI_OAUTH_CLIENT_ID", "GEMINI_OAUTH_CLIENT_SECRET",
           "TEST_KEY", "XDG_DATA_HOME", "LOCALAPPDATA", "FACTORY_API_KEY", "OPENCODE_API_KEY", "KILO_API_KEY",
           "CODEBUFF_API_KEY", "AMP_API_KEY", "KIRO_DATA_DIR")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        self.home = Path(tempfile.mkdtemp(prefix="subusage-test-"))
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)
        os.environ["APPDATA"] = str(self.home / "AppData")
        os.environ["XDG_CONFIG_HOME"] = str(self.home / ".config")
        os.environ["SUBUSAGE_CONFIG_DIR"] = str(self.home / "cfg")
        self.now = time.time()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, rel: str, data: Any) -> Path:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p
