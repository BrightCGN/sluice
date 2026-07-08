"""Provider-Adapter — direkte Provider-Kommunikation (Spec §7.3, Revision 2).

Ein Adapter pro Provider hinter demselben Verifier/Gate. Adapter werden
AUSSCHLIESSLICH vom Dispatch aufgerufen, *nachdem* `guarded_egress` released hat —
nie mit unverifiziertem Text (Invariante 2). Kein Modul ruft je an der Kette vorbei
nach außen.

- API-Keys per Env (`SLUICE_<PROVIDER>_API_KEY`), nie im Profil-TOML, nie im Audit.
  Fehlender Key ⇒ fail-closed (`ProviderConfigError`), kein stiller Fallback (§7.3).
- Timeouts: endlicher Connect-Timeout, KEIN Read-Timeout — agentische Turns
  streamen lange (Code-Konvention, §7.3).
- Failover/Health/Tenant-Order: post-v1. v1 ruft genau den einen per Profil
  erlaubten und vom Konsumenten gewählten Provider.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

# Endlicher Connect-, kein Read-Timeout (§7.3 / Code-Konventionen).
PROVIDER_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)


class ProviderConfigError(RuntimeError):
    """Fehlkonfiguration (z. B. fehlender API-Key) — fail-closed, kein Fallback."""


class ProviderError(RuntimeError):
    """Upstream-Fehler des Providers (Non-2xx, unerwartetes Antwortformat)."""


@dataclass(frozen=True)
class ProviderResponse:
    """Normalisierte Antwort eines Providers — die Fläche, die der Dispatch reverst."""

    text: str
    model: str
    provider: str


@runtime_checkable
class ProviderAdapter(Protocol):
    """Das Adapter-Interface (Spec §7.3). Neuer Provider = eine weitere Implementierung."""

    name: str

    async def complete(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> ProviderResponse:
        """Eine nicht-streamende Completion über sanitisierte Messages."""
        ...

    def stream(
        self, messages: list[dict[str, Any]], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Text-Deltas der Antwort als async Iterator (SSE-normalisiert)."""
        ...


def require_api_key(env_var: str, provider: str) -> str:
    """Key aus der Env — fehlt er, ist das ein Konfigurationsfehler, kein Durchlass."""
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise ProviderConfigError(
            f"Kein API-Key für Provider '{provider}': Env-Variable {env_var} fehlt (§7.3)."
        )
    return key


def select_provider(
    name: str, *, http_client: httpx.AsyncClient | None = None
) -> ProviderAdapter:
    """Registry-Auswahl per Provider-Name (§7.3). Unbekannter Name ⇒ fail-closed."""
    from sluice.providers.anthropic import AnthropicAdapter
    from sluice.providers.gemini import GeminiAdapter
    from sluice.providers.mistral import MistralAdapter
    from sluice.providers.openai import OpenAIAdapter

    registry: dict[str, type] = {
        "anthropic": AnthropicAdapter,
        "claude": AnthropicAdapter,  # Alias — Profile sprechen historisch von "claude" (§4)
        "openai": OpenAIAdapter,
        "gemini": GeminiAdapter,
        "mistral": MistralAdapter,
    }
    adapter_cls = registry.get(name)
    if adapter_cls is None:
        raise ProviderConfigError(f"Unbekannter Provider '{name}' (§7.3): kein Adapter registriert.")
    return adapter_cls(http_client=http_client)


__all__ = [
    "PROVIDER_TIMEOUT",
    "ProviderAdapter",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponse",
    "require_api_key",
    "select_provider",
]
