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
from .apikeys import (AlibabaProvider, AmpProvider, ChutesProvider, CodebuffProvider, FactoryProvider, GitKrakenProvider,
                      KiloProvider, MiniMaxProvider, OpenCodeGoProvider, PoeProvider, WarpProvider)
from .local import (AmazonQProvider, AntigravityProvider, CodeRabbitProvider, GrokProvider, KiroProvider,
                    WindsurfProvider)
from .web import (AugmentProvider, DevinProvider, ManusProvider, MistralProvider, NotionProvider, OllamaProvider,
                  PerplexityProvider, RaycastProvider, T3ChatProvider, ZedProvider)

_ORDER: list[type[Provider]] = [
    # Detected from logins already on this computer
    ClaudeProvider, CodexProvider, CopilotProvider, GitHubProvider, GeminiProvider, CursorProvider, JetBrainsProvider,
    NousProvider, WindsurfProvider, KiroProvider, AmazonQProvider, AntigravityProvider, GrokProvider, ZedProvider,
    FactoryProvider, OpenCodeGoProvider, KiloProvider, CodebuffProvider, AmpProvider, CodeRabbitProvider,
    ClinePassProvider,
    # API key
    ZaiProvider, KimiProvider, SyntheticProvider, MiniMaxProvider, AlibabaProvider, WarpProvider, GitKrakenProvider,
    ChutesProvider, PoeProvider,
    # Browser session
    NotionProvider, MistralProvider, PerplexityProvider, AugmentProvider, DevinProvider, RaycastProvider,
    T3ChatProvider, ManusProvider, OllamaProvider,
    CommandProvider,
]
PROVIDERS: dict[str, type[Provider]] = {c.type: c for c in _ORDER}

# (name, vendor, what a usage source would need) — not built in yet.
CATALOG: list[tuple[str, str, str]] = [
    ("Qwen Cloud Token Plan", "Alibaba", "home.qwencloud.com session (sec_token) for 5-hour/weekly token-plan usage"),
    ("Muse Code", "Meta", "Muse CLI device-code login for 5-hour and weekly quotas"),
]

__all__ = ["PROVIDERS", "CATALOG", "Provider"]
