"""Lokaler ``llama-server`` als regulärer Provider (Spec §7.3).

Ein Modell im eigenen Netz ist kein Egress — und genau deshalb läuft es trotzdem
**durch Sluice**: Crate (und jeder andere Client) soll einen einzigen Weg zum
Modell kennen, mit einer Fehlerbehandlung, einem Audit und einer Key-Isolation.
Ein zweiter, direkter Pfad „nur für lokal" wäre die Abkürzung, die man später
nicht mehr unterscheiden kann.

**Zwei Abweichungen von den Cloud-Adaptern**, beide mit Absicht:

1. **Keine Default-Basis-URL.** Für `api.openai.com` gibt es eine kanonische
   Adresse, für „der llama-server im Heimnetz" nicht. Ein geratener Default
   (`localhost:8080`) wäre schlimmer als keiner: er zeigt auf die *Gateway*-VM
   statt auf die Modell-Maschine, und der Fehler sieht aus wie ein Netzproblem.
   Ohne ``SLUICE_LLAMACPP_BASE_URL`` also fail-closed, wie überall sonst.

2. **Der API-Key wird erzwungen, mit ausdrücklichem Opt-out.**
   ``llama-server --api-key …`` ist der Normalfall; ein offener Inferenz-Endpunkt
   im LAN ist einer, auf dem jeder Gast Modelle laufen lässt. Wer ihn bewusst
   ungeschützt fährt, erklärt das mit ``SLUICE_LLAMACPP_ALLOW_NO_AUTH=1`` — dann
   geht die Anfrage ohne ``Authorization``-Header raus. Stillschweigend ohne Key
   zu senden wäre die Sorte Bequemlichkeit, die man erst bemerkt, wenn sie
   ausgenutzt wird.

Tool-Calling trägt dieser Adapter **nicht** (``TOOL_CAPABLE_PROVIDERS`` kennt ihn
nicht): llama.cpp beherrscht es je Modell und Chat-Template unterschiedlich, und
ein Dialekt, der nur manchmal funktioniert, ist schlechter als keiner.
"""

from __future__ import annotations

import os

# Wie in ``openai_compat``: aus dem Paket, nicht aus ``sluice.errors`` — dort
# wohnt die Klasse nicht. Zyklenfrei, weil ``select_provider`` die Adapter erst
# beim Aufruf importiert, also nach dem Laden des Pakets.
from sluice.providers import ProviderConfigError
from sluice.providers.openai_compat import OpenAICompatAdapter

#: Woher die Adresse kommt. Kein Default — siehe Modul-Docstring.
BASE_URL_ENV = "SLUICE_LLAMACPP_BASE_URL"
#: Ausdrückliche Erklärung, dass der Endpunkt ohne Key betrieben wird.
ALLOW_NO_AUTH_ENV = "SLUICE_LLAMACPP_ALLOW_NO_AUTH"


class LlamaCppAdapter(OpenAICompatAdapter):
    """``llama-server`` über den OpenAI-kompatiblen ``/v1``-Dialekt."""

    name = "llamacpp"
    key_env = "SLUICE_LLAMACPP_API_KEY"
    # Bewusst leer: ohne SLUICE_LLAMACPP_BASE_URL gibt es keinen Durchlass.
    default_base_url = ""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        if not self._base_url:
            self._base_url = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
        if not self._base_url:
            raise ProviderConfigError(
                f"Keine Basis-URL für Provider '{self.name}': {BASE_URL_ENV} "
                "setzen (z. B. http://192.0.2.50:8080/v1). "
                "Ohne sie fail-closed (§7.3)."
            )

    def _headers(self) -> dict[str, str]:
        """Bearer-Header wie OpenAI — ohne Key nur nach ausdrücklichem Opt-in.

        ``llama-server --api-key …`` ist der Normalfall und wird hier erzwungen.
        Wer den Server bewusst offen betreibt, erklärt es über
        ``SLUICE_LLAMACPP_ALLOW_NO_AUTH=1``; dann geht die Anfrage ohne
        ``Authorization`` raus.
        """
        key = self._api_key or os.environ.get(self.key_env, "").strip()
        if key:
            return {"Authorization": f"Bearer {key}"}
        if os.environ.get(ALLOW_NO_AUTH_ENV, "").strip() in ("1", "true", "yes"):
            return {}
        raise ProviderConfigError(
            f"Kein API-Key für Provider '{self.name}': {self.key_env} setzen, "
            f"oder den ungeschützten Betrieb ausdrücklich mit "
            f"{ALLOW_NO_AUTH_ENV}=1 erklären (§7.3)."
        )
