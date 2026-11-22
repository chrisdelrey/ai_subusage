# SubUsage: AI subscription usage, on your desktop

A small, always-on-top desktop widget that shows how much of your AI **subscription** limits you've used: Claude, ChatGPT, GitHub Copilot, Gemini, Cursor, JetBrains AI, Nous and more. Each limit gets a bar, a reset countdown and a pace marker, so you don't have to keep opening each vendor's usage page.

Runs on **Windows, macOS and Ubuntu/Linux**. Pure Python standard library, no dependencies.

<p align="center">
  <img src="docs/widget.png" alt="SubUsage widget showing Claude, ChatGPT, Copilot, Gemini and Nous usage bars" width="320">
</p>

> **Subscriptions, not API billing.** SubUsage tracks the limits on plans you pay for monthly (Claude Max, ChatGPT Plus, Copilot Pro…). It doesn't track pay-per-token API spend.

---

## Contents

- [Features](#features)
- [Supported subscriptions](#supported-subscriptions)
- [Install](#install)
- [Using the widget](#using-the-widget)
- [Adding and configuring subscriptions](#adding-and-configuring-subscriptions)
- [System tray and background mode](#system-tray-and-background-mode)
- [Start at login](#start-at-login)
- [Updating](#updating)
- [Platform notes](#platform-notes)
- [Privacy and security](#privacy-and-security)
- [Command line](#command-line)
- [Troubleshooting](#troubleshooting)
- [How it works (and its limits)](#how-it-works-and-its-limits)
- [Contributing](#contributing)
- [License and acknowledgements](#license-and-acknowledgements)

## Features

- **Every limit at a glance:** 5-hour sessions, weekly caps, daily model quotas, monthly credits, each with a reset countdown.
- **Pace marker:** a white tick on each bar shows where you'd be if you used the window evenly. If the bar is past the tick, you're burning faster than the window allows.
- **Time to limit:** when a limit is on pace to run out before it resets, SubUsage says so ("⚠ At this pace: limit in ~47m"). It uses your burn rate over the last hour, or the average since the window started.
- **Usage history:** every check is saved locally (30 days). Hover a limit for a mini chart of the current window with its projection, or right-click → **History…** for the last 24 h / 7 / 30 days.
- **Declutter:** hide limits you don't care about, collapse a subscription to one line (▾), and sort so whatever's closest to its limit is on top.
- **Only shows what you're using:** if a tool's saved login has expired because you haven't used it lately (say, Hermes), that subscription is parked. It's hidden, and SubUsage stops asking the vendor about it. It comes back by itself the moment you use the tool again and its login refreshes.
- **Color thresholds:** green, then amber (70%), then red (90%). Both thresholds are adjustable.
- **Uses the logins you already have:** reads the sign-in saved by Claude Code, Codex CLI, Gemini CLI, the Cursor app, JetBrains IDEs, Hermes and more. There are no passwords to enter.
- **API usage too:** month-to-date spend or remaining credit for 14 pay-as-you-go APIs (OpenAI, Anthropic, OpenRouter, xAI, DeepSeek…). Paste just the key; every endpoint is pre-set, and each one has a **Get key ↗** button and step-by-step instructions.
- **39 subscriptions built in, with search:** type in the picker to find one (Enter adds it), or filter your own list in Settings. Paste an API key for key-based plans, a browser session for website-only ones, or plug in anything else with a custom command.
- **Settings window:** a login-file field for each subscription with **Find…** (also searches WSL on Windows), **Browse…** and **Test**, plus one-click **Sign in with GitHub** for GitHub Copilot.
- **Gentle on vendors:** polls every 5 minutes by default, backs off on rate limits (honoring `Retry-After`), and keeps showing the last good data while it waits.
- **Tray / menu bar icon on all three OSes:** a ring that fills with your tightest limit, a tooltip listing every subscription, and a menu to show/hide the widget, pause/resume updates, refresh, and toggle start at login.
- **Run in background:** one switch makes SubUsage start at login and live in the tray, with the widget hidden until you want it. Control it from the command line too (`--show`, `--hide`, `--pause`, `--resume`, `--quit`).
- **Three views:** Full, Condensed (one tight row per limit) and Compact (one line per subscription). Plus opacity, always-on-top, and start at login on all three OSes.
- **Built-in updates with release notes:** shows the version and build, checks GitHub daily, shows what's new, and updates in one click.

## Supported subscriptions

| Subscription | Plans | How it signs in | What it shows |
|---|---|---|---|
| **Claude** (Anthropic) | Pro, Max | Claude Code login (file, or macOS Keychain) | 5-hour session, weekly, per-model weekly, extra usage |
| **ChatGPT** (OpenAI) | Plus, Pro, Business | Codex CLI login (`codex login`) | 5-hour and weekly Codex limits, credits |
| **GitHub Copilot** | Free, Pro, Pro+, Business, Enterprise | Sign in with GitHub (in Settings) or `gh auth login` | Premium requests, chat/completions (Free) |
| **GitHub** (plan allowances) | Free, Pro | Fine-grained token with Plan (read) | Actions minutes, Codespaces compute and storage vs. included; billed beyond plan; Copilot overage |
| **Gemini** (Google) | Free, Google AI Pro / Ultra, Code Assist | Gemini CLI login | Daily Pro / Flash / Flash Lite quotas |
| **Cursor** | Hobby, Pro, Pro+, Ultra, Teams | Cursor app login (read-only) | Included usage, Auto / API split, on-demand, team pool |
| **JetBrains AI** | AI Free, AI Pro, AI Ultimate | Local IDE quota file (no network) | Monthly credits, top-up credits |
| **Nous Portal** | Plus, Super, Ultra | Hermes Agent / Hermes Desktop login | Daily, weekly, monthly allowances, top-ups |
| **GLM Coding Plan** (Z.ai) | Lite, Pro, Max | API key | 5-hour, weekly, MCP calls |
| **Kimi Code** (Moonshot) | Moderato, Allegretto, Allegro | API key | 5-hour, weekly, monthly |
| **Synthetic** | Standard, Pro | API key | Weekly, search hourly |
| **ClinePass** (Cline) | ClinePass | `cline auth` login or API key | 5-hour, weekly, monthly |
| **Windsurf** (Cognition) | Free, Pro, Teams | Windsurf app's cached plan (read-only) | Daily, weekly quota (or prompt credits) |
| **Kiro** (AWS) | Free, Pro, Pro+, Power | kiro-cli or Kiro IDE login | Monthly credits, bonus credits, overage |
| **Amazon Q Developer** | Free, Pro | Amazon Q CLI login (`q login`) | Monthly limits |
| **Antigravity** (Google) | Free, Pro, Ultra | The running app's local language server | Per-model quotas |
| **Grok** (xAI) | SuperGrok, Heavy | Grok CLI login (`grok login`) | Monthly credits |
| **Zed** | Free, Pro | Zed editor sign-in (macOS Keychain) or zed.dev session | Edit predictions, token spend |
| **Factory Droid** | Pro, Max, Teams | `FACTORY_API_KEY` (or `~/.factory/.env`) | 5-hour, weekly, monthly |
| **OpenCode Go** (SST) | Go | `opencode auth login` or API key | 5-hour, weekly, monthly |
| **Kilo Pass** | Starter, Pro, Expert | `kilo auth login` or API key | Pass credits, balance |
| **Codebuff** | Pro, Max | `codebuff login` or API key | Weekly limit, credits |
| **Amp** (Sourcegraph) | Free, paid tiers | `amp` CLI login or access token | Amp Free, subscription, credits |
| **CodeRabbit** | Lite, Pro | `coderabbit auth login` | Reviews this period |
| **MiniMax** | Coding Plan | API key | 5-hour, weekly |
| **Alibaba Coding Plan** (Qwen) | Lite, Pro | API key | 5-hour, weekly, monthly |
| **Warp** | Free, Build, Business | API key | Monthly credits, bonus credits |
| **GitKraken AI** | Pro, Advanced, Business | Personal access token | Weekly credits, org pool |
| **Chutes** | Base, Plus, Pro | API key | Rolling 4-hour, monthly |
| **Poe** (Quora) | Subscriber | API key | Points left (and % of your allowance) |
| **Notion AI** | Business, Enterprise | Browser session (`token_v2`) | Standard / Select credits (or 6-hour + monthly) |
| **Mistral** (Le Chat) | Pro, Team | Browser session (admin.mistral.ai) | Vibe and API budgets |
| **Perplexity** | Pro, Max | Browser session | Monthly credits, balance |
| **Augment Code** | Indie … Max | Browser session | Credits this billing cycle |
| **Devin** (Cognition) | Core, Team | Browser request (token + org) | Daily, weekly |
| **Raycast AI** | Pro, Pro+, Max | Browser session | Monthly AI credits |
| **T3 Chat** | Pro | Browser session | 4-hour base, monthly overage |
| **Manus** | Starter, Pro | Browser session | Monthly and daily credits |
| **Ollama Cloud** | Free, Pro, Max | Browser session | Session / weekly / monthly usage |
| **Custom** | anything | a command you write | whatever it prints |

**API usage (pay as you go).** Paste the key; optionally set a budget to get a bar (see [API usage](#api-usage)).

| API | Key you need | What it shows |
|---|---|---|
| **OpenAI API** | Admin key (`sk-admin-…`) | Spend this month (optionally one project) |
| **Anthropic API** | Admin key (`sk-ant-admin…`) | Spend this month |
| **OpenRouter** | API key | Credit balance, the key's limit, this month's spend on the key |
| **xAI API** | Management key + Team ID | Prepaid credit |
| **DeepSeek API** | API key | Balance |
| **Moonshot API** (Kimi) | API key (global or China) | Balance |
| **DeepInfra** | API key | Spend this month, credit or amount owed |
| **Vercel AI Gateway** | AI Gateway key | Credit balance, used to date |
| **SiliconFlow** | API key (international) | Balance |
| **Venice API** | API key | Daily DIEM allocation, USD balance |
| **fal** | Admin-scope key | Credit balance |
| **ElevenLabs** | API key with User → Read | Monthly credits |
| **Tavily** | API key | Monthly credits, per-key limit |
| **Linkup** | API key | Credit balance |

[PROVIDERS.md](PROVIDERS.md) has per-provider details (files read, endpoints, quirks), plus the two known subscriptions that aren't built in yet. Anything else can be added today with a [custom command](#custom-providers), and native support is welcome as a [contribution](CONTRIBUTING.md).

The providers added in 2.6 were built against the vendors' documented response shapes (largely from CodexBar's research) and are marked **new**: if one shows an error with a login you know is good, please [open an issue](https://github.com/chrisdelrey/ai_subusage/issues) with the `--once --json` output, tokens removed.

## Install

### 1. Python 3.10+ with Tk

| OS | What to do |
|---|---|
| **Windows** | Install Python from [python.org](https://www.python.org/downloads/). Tk is included. |
| **macOS** | Python from [python.org](https://www.python.org/downloads/) includes Tk. With Homebrew Python, also run `brew install python-tk`. |
| **Ubuntu / Debian** | `sudo apt install python3 python3-tk` |
| **Fedora** | `sudo dnf install python3 python3-tkinter` |

### 2. Get SubUsage

```bash
git clone https://github.com/chrisdelrey/ai_subusage.git
cd ai_subusage
```

Or install it as a command with [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/chrisdelrey/ai_subusage.git
subusage            # run it
```

### 3. Check, then run

```bash
python3 -m subusage --once     # prints what it can read for each subscription, then exits
python3 -m subusage            # starts the widget
python3 -m subusage --demo     # sample data, no logins needed
```

On Windows, use `python` instead of `python3`. You can also double-click **`subusage.pyw`** to start the widget with no console window.

## Using the widget

| Action | How |
|---|---|
| Move it | Drag anywhere. The position is remembered. |
| Hide it / bring it back | Click **▁** in the footer or menu → **Hide to tray**. Click the tray icon (Windows/Linux: left-click; macOS: its menu → Show widget) to bring it back. Launching SubUsage again, or `python3 -m subusage --show`, also brings it back. |
| Pause / resume updates | Tray icon menu or right-click menu → **Pause updates**. Nothing is fetched until you resume; the last numbers stay on screen. |
| Menu | Right-click (macOS: Ctrl-click or two-finger click) |
| Settings | Click **⚙** (bottom right) |
| Refresh now | Click **⟳**, or use the menu. There's a 60-second floor so you can't trip vendor rate limits. |
| Open a vendor's usage page | Double-click the subscription's name |
| Details for one limit | Hover it: usage, reset, burn rate, projection, and this window's chart |
| Hide a limit / see its history | Right-click the limit → **Hide "…"** / **History of "…"**. Unhide from the same menu or in Settings. |
| Collapse a subscription | Click **▾** next to its name (one line, worst percentages only). Menu → **Expand all** to undo. |
| Most urgent first | Menu → **Sort → Closest to limit first**. Limits on pace to run out come first, then the highest percentage. |
| Change density | Menu → **View**: **Full** (bars + details), **Condensed** (one row per limit with a mini bar), **Compact** (one line per subscription) |

<p align="center"><img src="docs/views.png" alt="Full, Condensed and Compact views" width="720"></p>
<p align="center">
  <img src="docs/tooltip.png" alt="Hover tooltip with burn rate, projection and chart" width="300">
  <img src="docs/history.png" alt="History window" width="500">
</p>

Subscriptions without a login on this machine are hidden. So are subscriptions whose login has **expired** because you haven't used that tool lately. SubUsage watches the login file (a cheap local check, no network) and brings the subscription back as soon as the tool refreshes it. Settings → **Expired logins** can instead show them as one dim line, or show the error. **Refresh now** also retries them. To list them anyway, turn off "Hide subscriptions with no login" in Settings.

## Adding and configuring subscriptions

Open **⚙ Settings → + Add subscription**. The picker groups everything by how it signs in:

- **Detected from your existing logins:** add it, and if you're signed in to that vendor's CLI or app, it just works.
- **Paste an API key:** add it, then paste the key in its Settings row.
- **Paste a browser session:** for subscriptions whose usage is only on the vendor's website (Notion AI, Mistral, Perplexity…). See [Browser sessions](#browser-sessions).
- **Anything else:** a custom command (see below).
- **Not built in yet:** the rest of the catalog, with notes on where usage data could come from.

Type in the **Search providers…** box to narrow the list (by name, vendor or plan); press **Enter** to add the only match. In Settings, **Filter subscriptions…** does the same for your own list.

Each subscription's row in Settings has:

- **Login file** with **Find…** and **Browse…**. Leave it blank for the default location. On Windows, **Find…** also searches every WSL distro, which helps if Claude Code or Codex runs in WSL.
- **API key** for key-based plans. Write `env:VAR_NAME` to read the key from an environment variable instead of saving it.
- **Sign in with GitHub** (GitHub Copilot): shows a code, copies it, and opens github.com/login/device.
- **Test**: checks the login right away and shows the plan and current percentages.
- **Name, refresh interval, on/off, Remove.**

<p align="center">
  <img src="docs/settings.png" alt="Settings window with Claude, Copilot and GLM rows" width="440">
  <img src="docs/picker.png" alt="Add subscription picker" width="440">
</p>

**Save** applies changes immediately. Everything lives in a JSON config file you can also edit by hand:

| OS | Config file |
|---|---|
| Windows | `%APPDATA%\SubUsageWidget\config.json` |
| macOS | `~/Library/Application Support/SubUsage/config.json` |
| Linux | `~/.config/subusage/config.json` |

### API usage

Add one from the picker's **API usage** group, then press **Get key ↗** in its row: it opens the exact page where that key is created, and the row's instructions say which kind of key it must be (OpenAI and Anthropic need an **Admin** key; normal keys can't read billing). Paste the key and press **Test**.

Most billing APIs report a number, not a limit, so you can give SubUsage one:

- **Monthly budget** (spend APIs like OpenAI, Anthropic, DeepInfra): this month's spend becomes a bar against it, resetting on the 1st (UTC). Your thresholds and the pace marker apply, so you'll see if you're on track to overshoot.
- **Full balance** (prepaid APIs like OpenRouter, DeepSeek, fal): what you top up to; the bar shows how much of it you've used, so it turns red as the credit runs low.

Leave it blank to just show the amount as a line. API usage is checked every 15 minutes by default.

### Browser sessions

Some vendors only show subscription usage on their website. For those, SubUsage uses your signed-in browser session, which you paste once:

1. Sign in to the site and open its usage or settings page.
2. Open developer tools (F12, or ⌥⌘I on a Mac) → **Network**, and reload.
3. Right-click any request to that site → **Copy → Copy as cURL**.
4. Paste it into the subscription's **Browser session** box and press **Test**.

Pasting just the `Cookie` header, or only the cookie the provider names (for example Notion's `token_v2`), works too. SubUsage keeps only the cookies that vendor needs, sends them only to that vendor, and stores them like an API key (`env:VAR` works). Sessions expire now and then; when one does, the subscription says "session expired" and you paste a fresh one. SubUsage doesn't read your browser's cookie store: doing that safely across three OSes would need third-party crypto libraries.

### Custom providers

Anything that can print a usage number can be a subscription. Write a script that prints JSON like this:

```json
{"plan": "Pro",
 "windows": [{"label": "Daily", "used_pct": 42.5,
              "resets_at": "2026-11-17T00:00:00Z", "length_seconds": 86400}],
 "notes": ["optional extra line"]}
```

Then go to Settings → + Add subscription → **Custom** and enter the command (for example `python ~/bin/my_usage.py`). Start from [`examples/custom_provider.py`](examples/custom_provider.py).

## System tray and background mode

SubUsage puts a small ring icon in the Windows notification area, the macOS menu bar, or the Linux tray. It fills clockwise with your **tightest** limit and turns green, amber or red at your thresholds. Hover it for one line per subscription (⚠ marks a limit on pace to run out). Its menu:

**Show/Hide widget · Pause/Resume updates · Refresh now · Settings… · Add subscription… · Start at login ✓ · Quit**

On Windows and Linux, a left-click shows or hides the widget; on macOS, clicking opens the menu.

**Settings → System tray** (Menu bar icon on macOS) has three choices: **Tray icon + widget**, **Tray icon only** (starts in the tray; open the widget when you want to track), and **No tray icon**.

**Settings → Run in background** is the "set and forget" option: SubUsage starts at login, shows only the icon, and the widget stays hidden until you open it. Turning it off also turns off start at login. It's a per-user background app rather than an OS service, because system services can't show a tray icon or windows.

Linux needs a panel that supports StatusNotifierItem tray icons: KDE Plasma, Ubuntu (its GNOME has the AppIndicator extension), Cinnamon, XFCE, LXQt, Budgie, waybar and most others. On stock GNOME (Fedora, for example), install the "AppIndicator and KStatusNotifierItem Support" extension. Without a tray host, SubUsage keeps the widget visible and Settings explains why.

On Windows, if the icon hides behind the **^** overflow arrow, drag it onto the taskbar, or turn it on under Settings → Personalization → Taskbar → Other system tray icons.

## Start at login

Right-click the widget → **Start at login**. What that does on each OS:

| OS | What it does | Undo |
|---|---|---|
| Windows | Adds `SubUsage` to `HKCU\…\CurrentVersion\Run` (runs `pythonw`, no console) | Untick, or Task Manager → Startup apps |
| macOS | Writes `~/Library/LaunchAgents/com.subusage.widget.plist` | Untick, or delete the file |
| Linux | Writes `~/.config/autostart/subusage.desktop` (10 s delay) | Untick, or delete the file |

It points at wherever SubUsage is installed. If you move the folder, untick and re-tick it. Only one copy of the widget runs at a time.

## Updating

The version and build show in **⚙ Settings → About & updates**. Build means the commit for a git checkout, or the release tag after an in-app update. SubUsage checks GitHub once a day, and you can turn that off. When a new release is out, a small **⬆ Update x.y.z** link appears in the widget's footer. Click it, then **Update now**. The widget updates itself the right way for how you installed it, then restarts:

| Installed with | Update does |
|---|---|
| `git clone` | `git pull --ff-only` (refuses if you have local changes) |
| pip / pipx | `pip install --upgrade git+https://github.com/chrisdelrey/ai_subusage.git@<tag>` |
| Downloaded zip | Downloads the release zip from GitHub and replaces the program files. Your settings live elsewhere and are kept. |

**Release notes come with every update.** Before you update, **What's new** shows the notes for every version since yours. After the restart, a **What's new** window appears once, including after a `git pull` or pip upgrade. All notes are in [CHANGELOG.md](CHANGELOG.md).

From a terminal: `python3 -m subusage --check-update` (prints the notes) or `python3 -m subusage --update`.

Updates are only installed when you click **Update now**. The daily check just looks at the GitHub Releases API (or the version on `main` if there are no releases yet).

## Platform notes

**Windows**
- If a CLI runs inside **WSL**, use **Find…** in Settings. It finds logins under `\\wsl.localhost\<distro>\home\<you>\`. Reading a WSL path starts that distro if it isn't running.
- GitHub Copilot's `gh auth token` fallback needs the GitHub CLI on your PATH. **Sign in with GitHub** works without it.

**macOS**
- Claude Code stores its login in the **Keychain**. The first time, macOS asks whether Python may read the "Claude Code-credentials" item. Choose **Always Allow** to avoid repeat prompts.
- Recent Cursor builds encrypt the app token with the Keychain. If Cursor shows "token is encrypted", paste the `WorkosCursorSessionToken` cookie from cursor.com into Cursor's **Session token** field.
- Right-click is Ctrl-click or a two-finger click.

**Ubuntu / Linux**
- Install Tk: `sudo apt install python3-tk`.
- Transparency needs a compositing window manager (GNOME, KDE). Without one, the opacity setting is ignored.
- On Wayland, Tk runs through XWayland. Positioning and always-on-top usually work, but some compositors ignore them.

## Privacy and security

- **It only reads logins, never writes them.** SubUsage reads tokens that your CLIs and apps already saved. It never refreshes them on disk, rewrites them, or changes their permissions, so it can't sign you out of anything. Gemini is the one exception to "never refreshes": its short-lived token is refreshed **in memory only**.
- **It only talks to the vendors.** Each provider calls only its own vendor's usage endpoint. There's no telemetry, no analytics, and no third-party server.
- **Secrets in the config file:** API keys, browser sessions and GitHub tokens you add in Settings are stored in plain text in your config file. On macOS/Linux the file is `chmod 600`; on Windows it lives in your user-only `%APPDATA%`. To keep a key out of the file entirely, use `env:VAR_NAME`.
- **History stays local:** usage samples go to `history.db` next to the config file and are pruned after 30 days. Turn history off or clear it in Settings.
- **The source is small:** one file per provider under `subusage/providers/`. Read it before you run it.

## Command line

```
python3 -m subusage              start the widget
python3 -m subusage --once       fetch every subscription once, print, exit
python3 -m subusage --once --json    same, as JSON (handy for scripts and bug reports)
python3 -m subusage --demo       sample data, no logins
python3 -m subusage --settings   start with the Settings window open
python3 -m subusage --version        version, build and install type
python3 -m subusage --check-update   is a newer version on GitHub?
python3 -m subusage --update         install it (then restart the widget)
python3 -m subusage --background     start with only the tray icon showing

Control the running widget:
python3 -m subusage --show | --hide | --toggle
python3 -m subusage --pause | --resume       stop / restart polling
python3 -m subusage --refresh | --quit
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| A subscription doesn't appear | It has no login on this machine. Run `--once` to see why, or turn off "Hide subscriptions with no login". |
| A subscription disappeared | Its login expired (you haven't used that tool lately). Use the tool once and it reappears. To see these, set Settings → Expired logins → Show dimmed. |
| "Login expired; it refreshes the next time you use …" | Shown when Expired logins is set to "Show the error". Use that tool once (`claude`, `codex`, `gemini`, Hermes, Cursor). |
| "Rate limited, retrying in …" | The vendor throttled its usage endpoint. Claude's is strict: keep its interval at 300 s or more. Old data stays visible. |
| GitHub Copilot: "didn't accept the GitHub CLI token" |
| GitHub: "needs a fine-grained token with Account → Plan: Read-only" | Create one at github.com → Settings → Developer settings → Fine-grained tokens. Under Account permissions, set **Plan** to Read-only. Paste it into the GitHub row in Settings. | Use **Sign in with GitHub** in Settings instead of the `gh` fallback. |
| Cursor: "token is encrypted" | Paste a cursor.com session token (see [macOS notes](#platform-notes)). |
| JetBrains numbers look old | The IDE only saves its quota file now and then. Open the IDE to update it. |
| `tkinter` missing | Ubuntu: `sudo apt install python3-tk`. Homebrew: `brew install python-tk`. |
| Update failed: "local changes" | Your git checkout has edits. Commit or stash them, or run `git pull` yourself. |
| Nothing happens on launch | Another copy is already running (one copy per user). Check the log: menu → Open log. |

Logs are in the same folder as the config file (`widget.log`).

## How it works (and its limits)

Most vendors don't publish an API for subscription usage. SubUsage uses the same internal endpoints the vendors' own tools use: Claude Code's `/usage`, Codex's `/status`, Copilot's IDE plugin, Gemini CLI's quota API, and so on. That means:

- **Any provider can break** when a vendor changes something. The others keep working, and the broken one shows a warning. Please [open an issue](https://github.com/chrisdelrey/ai_subusage/issues) with the `--once --json` output, with tokens removed.
- **The numbers are the vendor's.** SubUsage displays what the endpoint reports and doesn't estimate anything.
- **Per-surface splits aren't available.** For example, Claude's limits are shared across claude.ai, Desktop and Claude Code, and the endpoint reports only the combined total.

## Contributing

Contributions are very welcome, especially **new providers**. Each one is a single file with one `fetch()` method. Settings rows are generated from the fields it declares, so no UI code is needed. See [CONTRIBUTING.md](CONTRIBUTING.md) for the walkthrough, the test helpers and the PR checklist.

```bash
python3 -m unittest discover -s tests -t .
```

## License and acknowledgements

Made by **Delrey Productions**. [MIT](LICENSE).

- The **[CodexBar](https://github.com/steipete/CodexBar)** project (MIT, by Peter Steinberger) documents many of these usage endpoints, and its provider sources were the reference for most of the subscriptions added in 2.6. It was an invaluable reference.
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** (MIT, Nous Research) shows how Nous Portal usage is fetched.

SubUsage is an independent project, not affiliated with or endorsed by any of the vendors whose subscriptions it tracks. All product names and trademarks belong to their owners.
