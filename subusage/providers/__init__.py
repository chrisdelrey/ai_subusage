"""Provider registry.

PROVIDERS: built-in providers, in the order they're offered in "Add subscription".
CATALOG:   other known AI subscriptions with usage limits that aren't built in yet. Shown in the picker
           with a note, so users know they can wire them up with a custom command (and contributors know
           what's wanted). Keep PROVIDERS.md in sync.
"""
from __future__ import annotations

from ..core import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .command import CommandProvider
from .copilot import CopilotProvider
from .cursor import CursorProvider
from .gemini import GeminiProvider
from .github import GitHubProvider
from .jetbrains import JetBrainsProvider
from .keyed import ClinePassProvider, KimiProvider, SyntheticProvider, ZaiProvider
from .nous import NousProvider

_ORDER: list[type[Provider]] = [
    ClaudeProvider, CodexProvider, CopilotProvider, GitHubProvider, GeminiProvider, CursorProvider, JetBrainsProvider,
    NousProvider, ZaiProvider, KimiProvider, SyntheticProvider, ClinePassProvider, CommandProvider,
]
PROVIDERS: dict[str, type[Provider]] = {c.type: c for c in _ORDER}

# (name, vendor, what a usage source would need) — not built in yet.
CATALOG: list[tuple[str, str, str]] = [
    ("Windsurf", "Cognition", "Local state.vscdb cache (cachedPlanInfo) or a windsurf.com session"),
    ("Devin", "Cognition", "Bearer token from app.devin.ai for daily/weekly quotas"),
    ("Grok / SuperGrok", "xAI", "Grok CLI billing RPC or grok.com session"),
    ("Perplexity Pro / Max", "Perplexity", "perplexity.ai session for usage credits"),
    ("Mistral Le Chat Pro", "Mistral AI", "console.mistral.ai session for monthly plan usage"),
    ("MiniMax Coding Plan", "MiniMax", "API key or platform.minimax.io session for 5-hour/weekly windows"),
    ("Qwen Code / Token Plan", "Alibaba", "Model Studio session for 5-hour/weekly token-plan usage"),
    ("Kiro", "AWS", "Kiro CLI usage command for monthly credits"),
    ("Amazon Q Developer Pro", "AWS", "No public usage source known"),
    ("Augment Code", "Augment", "Augment CLI or app.augmentcode.com session for credits"),
    ("Amp", "Sourcegraph", "ampcode.com session for plan usage"),
    ("Factory Droid", "Factory", "app.factory.ai session for monthly tokens"),
    ("Warp AI", "Warp", "Warp API token for request limits and monthly credits"),
    ("Zed Pro", "Zed Industries", "Zed's local session for edit-prediction and prompt quotas"),
    ("Ollama Cloud", "Ollama", "ollama.com session for hourly/daily cloud usage"),
    ("OpenCode Zen / Go", "SST", "opencode.ai workspace session for subscription usage"),
    ("Kilo Pass", "Kilo Code", "Kilo API token for pass usage"),
    ("Codebuff", "Codebuff", "~/.config/manicode/credentials.json for credits and weekly limit"),
    ("GitKraken AI", "GitKraken", "Account token for weekly AI credits"),
    ("Raycast AI", "Raycast", "raycast.com session for monthly AI credits"),
    ("Notion AI", "Notion", "notion.so session for workspace AI allowances"),
    ("T3 Chat", "T3", "t3.chat session for base and overage buckets"),
    ("Poe", "Quora", "Poe API key for point balance"),
    ("Chutes", "Chutes", "API key for subscription quota windows"),
    ("Manus", "Manus", "manus.im session for monthly and daily credits"),
    ("CodeRabbit", "CodeRabbit", "CodeRabbit CLI usage report"),
    ("Muse Code", "Muse", "Muse CLI login for subscription quotas"),
    ("Antigravity", "Google", "Local language-server probe or agy CLI"),
]

__all__ = ["PROVIDERS", "CATALOG", "Provider"]
