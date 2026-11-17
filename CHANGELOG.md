# Changelog

## 2.2.0
- **Time to limit:** warns when a limit is on pace to run out before it resets ("⚠ At this pace: limit in ~47m"), using the burn rate over the last hour or the average since the window started. Shown inline (Full), in place of the countdown (Condensed), and as ⚠ (Compact/collapsed). Can be turned off.
- **Usage history:** each check is stored locally in `history.db` (30-day retention, can be turned off or cleared). Hover any limit for details and a mini chart of the current window with its projection; right-click → History… for 24 h / 7 / 30 days.
- **Declutter:** hide individual limits (right-click), collapse a subscription to one line (▾), sort closest-to-limit first, unhide from Settings.
- **Expired logins are parked:** when a tool's saved login has expired (it hasn't been used lately), its subscription is hidden and SubUsage stops calling the vendor. A cheap local watch on the login file (or the macOS Keychain item for Claude, the GitHub CLI login for Copilot) brings it back automatically once the tool refreshes it. Settings → Expired logins: Hide (default) / Show dimmed / Show the error. API-key subscriptions are never parked, so a wrong key stays visible.
- Fix: the history hook no longer holds a reference to the widget, which could make Tk abort when a fetch thread finished.
- Right-click menus are now contextual to the limit or subscription under the mouse.

## 2.1.0
- Version and build display (Settings → About & updates, `--version`).
- In-app updates: daily check of GitHub Releases; one-click update via git, pip or zip; `--check-update`, `--update`.
- Condensed view (one row per limit) between Full and Compact.
- Release workflow: tag `vX.Y.Z` → GitHub Release.

## 2.0.0
- Cross-platform rewrite (Windows, macOS, Linux) as a package with one file per provider.
- Providers: Claude, ChatGPT (Codex), GitHub Copilot, Gemini CLI, Cursor, JetBrains AI, Nous Portal, GLM Coding Plan (z.ai), Kimi Code, Synthetic, ClinePass, custom command.
- Add-subscription picker with a catalog of not-yet-built-in subscriptions; Settings generated from provider fields; start at login on all OSes.
