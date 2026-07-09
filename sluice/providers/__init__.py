"""Provider-Adapter — direkte Provider-Kommunikation (Spec §7.3, Revision 2).

Ein Adapter pro Provider hinter demselben Verifier/Gate. Adapter werden
AUSSCHLIESSLICH vom Dispatch aufgerufen, *nachdem* `guarded_egress` released hat —
nie mit unverifiziertem Text (Invariante 2). Kein Modul ruft je an der Kette vorbei
nach außen.

Seit Revision 7 laufen die direkten Adapter (`select_provider`) NUR noch in den
Gateway-Prozessen (`sluice/gateway.py`); der Kern wählt seine Adapter über
`select_egress_adapter` und erreicht Provider ausschließlich über die Gateways.

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


# Aliase auf den kanonischen Adapter-Namen — Profile sprechen historisch von "claude" (§4).
_ALIASES = {"claude": "anthropic"}


def canonical_provider(name: str) -> str:
    """Kanonischer Provider-Name für Vergleiche (z. B. `SLUICE_PROVIDER`-Lock, §7.3)."""
    return _ALIASES.get(name, name)


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


def select_egress_adapter(
    name: str, *, http_client: httpx.AsyncClient | None = None
) -> ProviderAdapter:
    """Adapter-Wahl des Sluice-Kerns (§7.3, Rev. 7): AUSSCHLIESSLICH über Gateways.

    Der Kern spricht Provider nie direkt an — jeder Provider braucht seinen
    eigenständigen Gateway-Service und dessen URL (`SLUICE_GATEWAY_<PROVIDER>_URL`).
    Fehlende URL ⇒ fail-closed (`ProviderConfigError`), kein stiller Fallback auf
    den direkten Adapter. Der Kern hält damit keine Provider-Keys (Key-Isolation).
    Der Dispatch ruft den Adapter erst nach `released=true` (Invariante 2).
    """
    canonical = canonical_provider(name)
    if canonical not in ("anthropic", "openai", "gemini", "mistral"):
        raise ProviderConfigError(
            f"Unbekannter Provider '{name}' (§7.3): kein Adapter registriert."
        )
    env_var = f"SLUICE_GATEWAY_{canonical.upper()}_URL"
    gateway_url = os.environ.get(env_var, "").strip()
    if not gateway_url:
        raise ProviderConfigError(
            f"Kein Gateway für Provider '{canonical}': Env-Variable {env_var} fehlt. "
            f"Der Kern ruft Provider nie direkt — Gateway-Service starten und URL "
            f"konfigurieren (§7.3, Rev. 7)."
        )
    from sluice.providers.remote import RemoteGatewayAdapter

    return RemoteGatewayAdapter(
        provider=canonical,
        base_url=gateway_url,
        token=os.environ.get("SLUICE_GATEWAY_TOKEN", "").strip() or None,
        http_client=http_client,
    )


__all__ = [
    "PROVIDER_TIMEOUT",
    "ProviderAdapter",
    "canonical_provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponse",
    "require_api_key",
    "select_egress_adapter",
    "select_provider",
]
