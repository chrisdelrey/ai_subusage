# Providers

What each built-in provider reads, where it sends requests, and its quirks. Then the catalog of AI subscriptions that aren't built in yet.

All endpoints below are **unofficial**: they're the ones the vendors' own apps use, and they can change without notice.

## Built in

### Claude (Anthropic): Pro, Max
- **Login:** Claude Code's OAuth token. Linux/Windows: `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`). macOS: the Keychain item `Claude Code-credentials`. WSL: use **Find…** in Settings.
- **Request:** `GET https://api.anthropic.com/api/oauth/usage` with header `anthropic-beta: oauth-2025-04-20`
- **Shows:** 5-hour session, weekly, per-model weekly (Opus/Sonnet when present), extra usage %
- **Notes:** Limits are shared across claude.ai, Desktop, mobile and Claude Code. The endpoint rate-limits hard (429s can last 30+ min), so the minimum interval is 120 s and the default is 300 s. If the token has expired, open Claude Code once.

### ChatGPT (OpenAI): Plus, Pro, Business
- **Login:** Codex CLI: `~/.codex/auth.json` (or `$CODEX_HOME`). Requires `codex login` with ChatGPT, not an API key.
- **Request:** `GET https://chatgpt.com/backend-api/wham/usage` (with `ChatGPT-Account-ID`)
- **Shows:** 5-hour and weekly Codex windows, credits balance
- **Notes:** ChatGPT's in-app chat message caps aren't exposed anywhere.

### GitHub Copilot: Free, Pro, Pro+, Business, Enterprise
- **Login, in order:** token from **Sign in with GitHub** in Settings (OAuth device flow with the VS Code Copilot app, `read:user` scope), then a pasted token, then `gh auth token`
- **Request:** `GET https://api.github.com/copilot_internal/user` (or `api.<enterprise host>`)
- **Shows:** premium requests (paid plans); chat and completions (Free). Resets monthly.
- **Notes:** Some `gh` CLI tokens are rejected by this endpoint. Sign in with GitHub always works.

### GitHub (plan allowances): Free, Pro
- **Login:** a fine-grained personal access token with **Account permissions → Plan: Read-only** (github.com → Settings → Developer settings → Fine-grained tokens). Falls back to `gh auth token`, but the GitHub CLI's token usually can't read billing.
- **Requests (official REST API):** `GET /user` (login + plan), `GET /users/{login}/settings/billing/usage/summary?year&month`, falling back to `…/billing/usage`; `GET /users/{login}/settings/billing/premium_request/usage`
- **Shows, for the current month:** Actions minutes vs. included (Free 2,000 / Pro 3,000; Windows ×2, macOS ×10; larger runners excluded). Codespaces compute (core-hours, Free 120 / Pro 180) and storage (GB-months, Free 15 / Pro 20), once used. Amount billed beyond the plan. Copilot premium requests beyond the allowance.
- **Notes:** The API reports usage but not the included amounts, so those come from GitHub's published plan table (`INCLUDED` in `github.py`). Only usage billed to your personal account is included, not usage billed to an organization or enterprise. Requires the enhanced billing platform. Polls every 30 min by default. Override the plan in Settings if detection is wrong.

### Gemini (Google): Free, Google AI Pro / Ultra, Code Assist
- **Login:** Gemini CLI: `~/.gemini/oauth_creds.json`
- **Requests:** `POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` (project + tier), `…:retrieveUserQuota`
- **Shows:** daily quota for Pro / Flash / Flash Lite (the most-used model in each family)
- **Notes:** Access tokens last 1 hour. When expired, SubUsage refreshes **in memory** with the OAuth client ID/secret from your installed Gemini CLI (found via `npm root -g` and common install paths), or from `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET`. It never writes back to `~/.gemini`. API-key and Vertex AI auth aren't supported.

### Cursor (Anysphere): Hobby, Pro, Pro+, Ultra, Teams
- **Login:** the Cursor app's `state.vscdb`, key `cursorAuth/accessToken`, opened read-only. Windows: `%APPDATA%\Cursor\User\globalStorage\`. macOS: `~/Library/Application Support/Cursor/User/globalStorage/`. Linux: `~/.config/Cursor/User/globalStorage/`. Falls back to `~/.config/cursor/auth.json` (`cursor-agent`), or to a pasted `WorkosCursorSessionToken`.
- **Request:** `GET https://cursor.com/api/usage-summary`
- **Shows:** included usage % with $ used, Auto vs API-model split, on-demand budget, team pool. Resets each billing cycle.
- **Notes:** Newer macOS builds encrypt the token with the Keychain (`cursor-safe-storage:v1:`). Paste a session token instead.

### JetBrains AI: AI Free, AI Pro, AI Ultimate
- **Login:** none. Reads `options/AIAssistantQuotaManager2.xml` from the most recently updated JetBrains IDE config (`%APPDATA%\JetBrains\*`, `~/Library/Application Support/JetBrains/*`, `~/.config/JetBrains/*`; Android Studio under `Google/*`)
- **Request:** none, local file only
- **Shows:** monthly credits (100,000 quota units = 1 credit), top-up credits, next refill
- **Notes:** The IDE saves this file only now and then, so open an IDE to refresh it.

### Nous Portal (Nous Research): Plus, Super, Ultra
- **Login:** Hermes Agent / Hermes Desktop: `~/.hermes/auth.json` → `providers.nous` (or `$HERMES_HOME`)
- **Request:** `GET {portal_base_url}/api/oauth/account`
- **Shows:** daily / weekly / monthly allowances and the large-model monthly gauge on usage-window plans; monthly credits on older plans; top-up credits
- **Notes:** Hermes Desktop's "Hermes Cloud" sign-in is encrypted inside the app and can't be read. Choose Nous Portal as the model provider instead.

### GLM Coding Plan (Z.ai / Zhipu): Lite, Pro, Max
- **Login:** API key (Settings), region Global or China
- **Request:** `GET https://api.z.ai/api/monitor/usage/quota/limit` (China: `open.bigmodel.cn`)
- **Shows:** 5-hour and weekly token windows, monthly MCP calls

### Kimi Code (Moonshot AI): Moderato, Allegretto, Allegro
- **Login:** API key from the Kimi Code console; region kimi.com or kimi.ai
- **Request:** `GET https://api.kimi.com/coding/v1/usages` (or `api.kimi.ai`)
- **Shows:** 5-hour, weekly, monthly total

### Synthetic: Standard, Pro
- **Login:** API key
- **Request:** `GET https://api.synthetic.new/v2/quotas`
- **Shows:** weekly plan limit, hourly search quota (older plans: 5-hour)

### ClinePass (Cline)
- **Login:** `cline auth` browser login (`~/.cline/data/settings/providers.json`) or an API key
- **Request:** `GET https://api.cline.bot/api/v1/users/me/plan/usage-limits`
- **Shows:** 5-hour, weekly, monthly

### Custom command
Runs your command and reads JSON from stdout. See the [README](README.md#custom-providers) and [`examples/custom_provider.py`](examples/custom_provider.py).

## Not built in yet

These AI subscriptions have usage limits, but SubUsage doesn't support them natively yet. Most expose usage only through a logged-in browser session. Reading browser cookies safely across three OSes is out of scope for a dependency-free tool, which is why these need either a CLI/token source or a custom command.

| Subscription | Vendor | Possible usage source |
|---|---|---|
| Windsurf | Cognition | Local `state.vscdb` cache (`windsurf.settings.cachedPlanInfo`) or a windsurf.com session |
| Devin | Cognition | Bearer token from app.devin.ai for daily/weekly quotas |
| Grok / SuperGrok | xAI | Grok CLI billing RPC or grok.com session |
| Perplexity Pro / Max | Perplexity | perplexity.ai session for usage credits |
| Mistral Le Chat Pro | Mistral AI | console.mistral.ai session for monthly plan usage |
| MiniMax Coding Plan | MiniMax | API key or platform.minimax.io session for 5-hour/weekly windows |
| Qwen Code / Token Plan | Alibaba | Model Studio session for 5-hour/weekly token-plan usage |
| Kiro | AWS | Kiro CLI usage command for monthly credits |
| Amazon Q Developer Pro | AWS | No public usage source known |
| Augment Code | Augment | Augment CLI or app.augmentcode.com session for credits |
| Amp | Sourcegraph | ampcode.com session for plan usage |
| Factory Droid | Factory | app.factory.ai session for monthly tokens |
| Warp AI | Warp | Warp API token for request limits and monthly credits |
| Zed Pro | Zed Industries | Zed's local session for edit-prediction and prompt quotas |
| Ollama Cloud | Ollama | ollama.com session for hourly/daily cloud usage |
| OpenCode Zen / Go | SST | opencode.ai workspace session for subscription usage |
| Kilo Pass | Kilo Code | Kilo API token for pass usage |
| Codebuff | Codebuff | `~/.config/manicode/credentials.json` for credits and weekly limit |
| GitKraken AI | GitKraken | Account token for weekly AI credits |
| Raycast AI | Raycast | raycast.com session for monthly AI credits |
| Notion AI | Notion | notion.so session for workspace AI allowances |
| T3 Chat | T3 | t3.chat session for base and overage buckets |
| Poe | Quora | Poe API key for point balance |
| Chutes | Chutes | API key for subscription quota windows |
| Manus | Manus | manus.im session for monthly and daily credits |
| CodeRabbit | CodeRabbit | CodeRabbit CLI usage report |
| Muse Code | Muse | Muse CLI login for subscription quotas |
| Antigravity | Google | Local language-server probe or `agy` CLI |

Want one of these? Open an issue, or better, a PR. The ones with a CLI, local file or API key (Kiro, Codebuff, Warp, Kilo, GitKraken, Poe, Chutes, Windsurf's local cache) are the easiest to add. See [CONTRIBUTING.md](CONTRIBUTING.md).
