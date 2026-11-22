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

### Windsurf (Cognition): Free, Pro, Teams
- **Login:** none sent; reads `windsurf.settings.cachedPlanInfo` from Windsurf's `User/globalStorage/state.vscdb` (read-only)
- **Request:** none (local). The cache updates while Windsurf runs.
- **Shows:** daily and weekly quota, or prompt credits / flow actions on older plans

### Kiro (AWS): Free, Pro, Pro+, Power
- **Login:** kiro-cli's `data.sqlite3` (`auth_kv` → `kirocli:odic:token`, profile ARN from `state`), or the Kiro IDE's `~/.aws/sso/cache/kiro-auth-token.json`
- **Request:** `POST https://codewhisperer.us-east-1.amazonaws.com/` (or `q.eu-central-1`) with `X-Amz-Target: AmazonCodeWhispererService.GetUsageLimits`
- **Shows:** monthly credits, bonus credits, overage. Parks when the token has expired.

### Amazon Q Developer (AWS): Free, Pro
- **Login:** Amazon Q CLI's `data.sqlite3` (`codewhisperer:odic:token`)
- **Request:** same `GetUsageLimits` call as Kiro
- **Shows:** each monthly limit AWS reports

### Antigravity (Google): Free, Pro, Ultra
- **Login:** none read. Finds the running Antigravity language server (`ps` / PowerShell), takes its `--csrf_token` and listening port (`lsof`/`ss` / `Get-NetTCPConnection`)
- **Request:** `POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/GetUserStatus` (self-signed certificate; certificate checks are skipped only for 127.0.0.1)
- **Shows:** each model's quota (`remainingFraction`, `resetTime`). Only works while the app is open.

### Grok (xAI): SuperGrok, SuperGrok Heavy
- **Login:** the Grok CLI's own (`grok login`); SubUsage only checks that `~/.grok/auth.json` exists
- **Request:** runs `grok agent stdio` and calls the JSON-RPC method `_x.ai/billing`
- **Shows:** credit usage for the billing period, prepaid balance

### Zed: Free, Pro
- **Login:** macOS: the editor's Keychain item for `zed.dev` (via `security`); elsewhere a pasted zed.dev session
- **Request:** `GET https://cloud.zed.dev/client/users/me` (editor) or `GET https://cloud.zed.dev/frontend/billing/usage` (session)
- **Shows:** edit predictions, token spend

### Factory Droid: Pro, Max, Teams
- **Login:** API key, `FACTORY_API_KEY`, or `FACTORY_API_KEY=` in `~/.factory/.env`
- **Request:** `GET https://api.factory.ai/api/billing/limits`, falling back to `/api/organization/subscription/usage`
- **Shows:** 5-hour, weekly, monthly (or standard/premium token allowances)

### OpenCode Go (SST)
- **Login:** API key, `OPENCODE_API_KEY`, or the `opencode-go` entry in `~/.local/share/opencode/auth.json`
- **Request:** `GET https://opencode.ai/zen/go/v1/usage`
- **Shows:** 5-hour, weekly, monthly

### Kilo Pass (Kilo Code)
- **Login:** API key, `KILO_API_KEY`, or `kilo.access` in `~/.local/share/kilo/auth.json`
- **Request:** `GET https://app.kilo.ai/api/trpc/kiloPass.getState,user.getCreditBlocks?batch=1`
- **Shows:** pass credits this period, credit balance

### Codebuff
- **Login:** API key, `CODEBUFF_API_KEY`, or `~/.config/manicode/credentials.json`
- **Request:** `POST https://www.codebuff.com/api/v1/usage` and `GET /api/user/subscription`
- **Shows:** weekly limit, credits

### Amp (Sourcegraph)
- **Login:** an access token (`AMP_API_KEY`), or runs `amp usage` with the CLI's own login
- **Request:** `POST https://ampcode.com/api/internal?userDisplayBalanceInfo` (token) — both return the same text, which SubUsage parses
- **Shows:** Amp Free, subscription usage, agent usage, individual and workspace credits

### CodeRabbit
- **Login:** the CodeRabbit CLI's own; runs `coderabbit usage`
- **Shows:** reviews this period and billing state (no limit is reported, so there's no bar)

### MiniMax: Coding Plan
- **Login:** Coding Plan API key; Global or China region
- **Request:** `GET https://api.minimax.io/v1/token_plan/remains`, falling back to `/v1/api/openplatform/coding_plan/remains`
- **Quirk:** MiniMax names the *remaining* count `current_interval_usage_count`
- **Shows:** 5-hour, weekly

### Alibaba Coding Plan (Qwen Code): Lite, Pro
- **Login:** Coding Plan API key; International (Singapore) or China (Beijing)
- **Request:** `POST https://modelstudio.console.alibabacloud.com/data/api.json?action=…queryCodingPlanInstanceInfoV2`
- **Shows:** 5-hour, weekly, monthly

### Warp: Free, Build, Business
- **Login:** API key (`WARP_API_KEY`)
- **Request:** `POST https://app.warp.dev/graphql/v2?op=GetRequestLimitInfo`
- **Shows:** monthly credits, bonus credits

### GitKraken AI
- **Login:** personal access token; optional organization ID
- **Request:** `GET https://api.gitkraken.dev/v1/ai-tasks/usage`
- **Shows:** weekly credits, organization pool

### Chutes: Base, Plus, Pro
- **Login:** API key (`CHUTES_API_KEY`)
- **Request:** `GET https://api.chutes.ai/users/me/subscription_usage`, falling back to `/users/me/quotas`
- **Shows:** rolling 4-hour and monthly quotas (loosely shaped; parsed best-effort)

### Poe (Quora)
- **Login:** API key from poe.com/api_key; optional monthly point allowance
- **Request:** `GET https://api.poe.com/usage/current_balance`
- **Shows:** points left, or a bar against the allowance you enter

### Browser-session providers

These read usage only the vendor's website shows. You paste the session (Cookie header or "Copy as cURL"); SubUsage keeps only the listed cookies. A 401/403 or a redirect to a login page shows "session expired" (they're never parked).

| Subscription | Cookies kept | Request | Shows |
|---|---|---|---|
| Notion AI (Business/Enterprise) | `token_v2` (+ `notion_user_id`) | `POST app.notion.com/api/v3/getSpaces`, then `getCreditRateLimitStatus` (falls back to www.notion.so) | Standard and Select credits, or rolling 6-hour + monthly |
| Mistral (Le Chat Pro/Team) | `ory_session_*`, `csrftoken` | `GET admin.mistral.ai/subscription` (budgets in the page's data), falling back to `console.mistral.ai/api-ui/trpc/billing.vibeUsage` | Vibe and API budgets |
| Perplexity | `__Secure-next-auth.session-token` (or authjs) | `GET www.perplexity.ai/rest/billing/credits` | Monthly credits, balance |
| Augment Code | session cookies | `GET app.augmentcode.com/api/credits` and `/api/subscription` | Credits this cycle |
| Devin | Bearer token + org (from the pasted cURL) | `GET app.devin.ai/api/<org>/billing/quota/usage` | Daily, weekly |
| Raycast AI | `__raycast_session`, `csrf_token` | `GET www.raycast.com/frontend_api/current_user/ai_credits` | Monthly AI credits |
| T3 Chat | all t3.chat cookies (+ captured headers) | `GET t3.chat/api/trpc/getCustomerData` | 4-hour base, monthly overage |
| Manus | `session_id` (sent as a Bearer token) | `POST api.manus.im/user.v1.UserService/GetAvailableCredits` | Monthly and daily credits |
| Ollama Cloud | all ollama.com cookies | `GET ollama.com/settings` (HTML) | Session/weekly/monthly usage |

## Not built in yet

| Subscription | Vendor | Possible usage source |
|---|---|---|
| Qwen Cloud Token Plan | Alibaba | home.qwencloud.com session (`sec_token`) for 5-hour/weekly token-plan usage |
| Muse Code | Meta | Muse CLI device-code login for 5-hour and weekly quotas |

Want one? Open an issue, or better, a PR. See [CONTRIBUTING.md](CONTRIBUTING.md).
