# Contributing

Thanks for helping! The most valuable contributions are **new providers** and **fixes when a vendor changes its endpoint**. Bug reports, platform fixes (especially macOS and Linux desktop quirks) and docs are welcome too.

## Ground rules

- **Standard library only.** SubUsage has no dependencies, and that's a feature. Please don't add any.
- **Read-only credentials.** Never write to, refresh on disk, move or re-permission a vendor's login file. Refreshing in memory is OK; see `gemini.py`.
- **Only talk to the vendor.** A provider may contact only its own vendor's endpoints. No telemetry, no third-party proxies.
- **Be gentle.** The default poll is 300 s. Set `min_poll` higher if a vendor rate-limits.
- **Python 3.10+, Windows / macOS / Linux.** Use `pathlib` and the helpers in `core.py` (`app_data_dirs()`, `IS_WIN`, `IS_MAC`) rather than hard-coded paths.

## Dev setup

```bash
git clone https://github.com/chrisdelrey/ai_subusage.git
cd ai_subusage
python3 -m subusage --demo                       # see the UI
python3 -m unittest discover -s tests -t .       # run the tests (UI tests skip without a display)
```

## Project layout

```
subusage/
  core.py              data model, Provider base class, http_json, config, scheduler, start-at-login
  ui.py                the widget (tkinter)
  settings.py          Settings window + "Add subscription" picker (generated from provider fields)
  update.py            version/build info, update check (GitHub Releases) and self-update
  history.py           local usage history (SQLite) + time-to-limit projections
  charts.py            hover tooltip mini chart and the History window
  providers/
    __init__.py        registry (PROVIDERS) + catalog of not-yet-built-in subscriptions (CATALOG)
    claude.py, codex.py, copilot.py, gemini.py, cursor.py, jetbrains.py, nous.py
    keyed.py           small API-key providers (z.ai, Kimi, Synthetic, ClinePass)
    command.py         custom command provider + demo data
tests/                 unittest suites; helpers.py has FakeHTTP and an isolated fake HOME
examples/              custom provider template
subusage.pyw           double-click launcher
```

## Adding a provider

### 1. Write the class

Create `subusage/providers/acme.py`:

```python
from ..core import Field, Provider, ProviderError, Snapshot, UsageWindow, http_json, parse_time, read_json_file


class AcmeProvider(Provider):
    # Catalog metadata: shown in "Add subscription" and the Settings row
    type = "acme"                       # unique id (config "type")
    default_name = "Acme AI"
    vendor = "Acme"
    plans = "Plus, Pro"
    auth = "auto"                       # "auto" = reads an existing login, "key" = paste an API key
    setup = "Run `acme login`. SubUsage reads that login read-only."
    usage_url = "https://acme.ai/settings/usage"
    accent = "#ff6b00"

    # Where the CLI keeps its login (enables Find… / Browse… / default path in Settings)
    login_dir, login_file, env_var, login_what = ".acme", "auth.json", "ACME_HOME", "Acme CLI login"

    # Settings rows are generated from these. Kinds: path, secret, text, choice, command, device_login
    fields = (Field("auth_path", "Login file", "path", "Blank = ~/.acme/auth.json"),)

    def fetch(self) -> Snapshot:
        token = read_json_file(self.login_path(), self.login_what).get("token")  # NotConfigured if missing
        if not token:
            raise ProviderError("No Acme login; run `acme login`")
        j = http_json("https://api.acme.ai/v1/usage", {"Authorization": f"Bearer {token}"})
        return self.parse(j)

    @staticmethod
    def parse(j) -> Snapshot:            # keep parsing separate so tests can feed it JSON directly
        snap = Snapshot(plan=j.get("plan", ""))
        snap.windows.append(UsageWindow("Weekly", j["used_pct"], parse_time(j.get("resets_at")), 7 * 86400))
        return snap
```

Key-based providers set `auth = "key"`, declare `Field("api_key", "API key", "secret")`, and call `self.require_key()`. Values can be `env:VAR`, which `setting()` resolves for you.

### 2. Signal errors the right way

| Raise | When | Widget shows |
|---|---|---|
| `NotConfigured` | No login/key on this machine | Hidden (unless the user turns hiding off) |
| `AuthExpired("…")` | The saved login exists but is expired/rejected | Parked: hidden, no network calls until `watch_signature()` changes (default: the login file's mtime). Override `watch_paths()`/`watch_signature()` if the login lives elsewhere; set `parks_on_auth = False` for API-key providers. |
| `ProviderError("…")` | Something the user can fix: expired token, no subscription | Warning line, keeps last good data |
| `HttpError` (from `http_json`) | Let it propagate | 429 → backoff with `Retry-After`; 401/403 → parked (or an error for key providers) |

Any other exception is caught and logged, so one provider can never crash the widget.

### 3. Register it

Add the class to `_ORDER` in `subusage/providers/__init__.py`. If it was listed in `CATALOG`, remove it from there. Add `"acme"` to `DEFAULT_PROVIDERS` in `core.py` **only** if it's an auto-detected login that most users would want shown when present.

### 4. Test it

Add a test to `tests/test_providers.py` using the helpers:

```python
class AcmeTests(HomeTestCase):          # fake HOME / APPDATA / XDG_CONFIG_HOME per test
    def test_windows(self):
        self.write(".acme/auth.json", {"token": "t"})
        http = FakeHTTP({"api.acme.ai/v1/usage": {"plan": "Pro", "used_pct": 40}})
        with mock.patch.object(acme, "http_json", http):
            s = acme.AcmeProvider({}).fetch()
        self.assertEqual(s.windows[0].used_pct, 40)
        self.assertEqual(http.last("usage")["headers"]["Authorization"], "Bearer t")
```

Base the canned JSON on a **real response with every secret and personal detail removed**. Cover the missing-login case (`NotConfigured`) and at least one odd shape the vendor is known to return.

### 5. Document it

Add a section to [PROVIDERS.md](PROVIDERS.md) (login source, request, what it shows, quirks) and a row to the table in the README.

### 6. Try it for real

```bash
python3 -m subusage --once            # your provider should print real numbers
python3 -m subusage --settings        # Add it, then Test from its row
```

## PR checklist

- [ ] `python3 -m unittest discover -s tests -t .` passes
- [ ] Standard library only; credentials are read-only
- [ ] Tested against a real account (say which plan/OS in the PR)
- [ ] PROVIDERS.md + README table updated; `CATALOG` updated if applicable
- [ ] No tokens, emails or account IDs in tests, screenshots or logs

## Releasing (maintainers)

1. Bump `__version__` in `subusage/__init__.py` (semver: provider fixes = patch, new providers/features = minor).
2. Add a `## X.Y.Z` section at the top of `CHANGELOG.md`, written for users. **Required:** CI fails without it, and so does the release.
3. Merge to `main`.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The `release` workflow checks that the tag matches `__version__`, runs the tests, and publishes a GitHub Release whose notes are that CHANGELOG section. Running widgets pick it up on their next daily check, show the notes under **What's new** before updating, and show them again once after restarting.

## Reporting a broken provider

Vendors change these endpoints. Open an issue with:

1. The provider, your plan and your OS
2. The output of `python3 -m subusage --once --json`. **Remove anything that looks like a token, email or ID.**
3. The relevant lines of `widget.log` (in the same folder as `config.json`)
