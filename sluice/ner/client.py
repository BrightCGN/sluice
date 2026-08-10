"""NER-Client — Sluice-Kern spricht den eigenständigen NER-Dienst an (Spec §5.3/§7.5, Rev. 12).

Spiegelt bewusst `providers/remote.py`: der Kern kennt den Dienst nur über seine URL
(`SLUICE_NER_URL` bzw. `[profile.X.ner] url`), damit er jederzeit auf einen eigenen
Server oder unter einen eigenen System-User umziehen kann (§7.3, Rev. 8).

**Was hier liegt und NICHT im Dienst** (§7.5): die Schwellwert-Anwendung, die
Label→Platzhalter-Abbildung, der Inhalts-Hash-Cache und die Identitätsprüfung. Der
Dienst bleibt „Text rein, Spans raus" und damit austauschbar.

**Fail-closed (§5.3):** jeder Fehlerpfad — keine URL, Verbindungsfehler, Timeout,
HTTP != 200, unlesbares JSON, Identitätsabweichung — endet in `NerUnavailableError`
bzw. `NerIdentityError`. Es gibt **keinen** Rückgabepfad, der bei Ausfall eine leere
Span-Menge liefert; der wäre eine stille Degradation zur reinen Regex-Stufe.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
import structlog

from sluice.ner import (
    FIXED_BATCH_SIZE,
    NerConfig,
    NerIdentityError,
    NerSpan,
    NerUnavailableError,
    ServiceInfo,
)

log = structlog.get_logger("sluice.ner.client")


ALLOW_PLAINTEXT_REMOTE_ENV = "SLUICE_NER_ALLOW_PLAINTEXT_REMOTE"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_loopback(host: str) -> bool:
    cleaned = host.strip().lower()
    if cleaned in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(cleaned.strip("[]")).is_loopback
    except ValueError:
        return False


def resolve_url(config: NerConfig) -> str:
    """URL aus Profil > Env `SLUICE_NER_URL`. Fehlt beides ⇒ fail-closed (§5.3).

    Bewusst dieselbe Härte wie die Gateway-Pflicht (Rev. 7): eine fehlende URL ist ein
    Konfigurationsfehler, der die Anfrage blockiert — nie ein Grund, die Stufe zu
    überspringen.

    **Transport-Schranke (Rev. 12).** Der NER-Dienst ist die einzige Komponente, die
    *unsanitisierten* Rohtext sieht — das ist sein Zweck, er soll die PII ja finden,
    bevor redigiert wird. Dieser Hop trägt damit **mehr** Personenbezug als der spätere
    Provider-Aufruf, der nur noch Sanitisiertes über TLS transportiert. Läuft der Dienst
    auf einem anderen Host und die URL ist `http://`, ginge genau dieser Rohtext im Klartext
    über das Netz.

    Deshalb: nicht-loopback **und** unverschlüsselt ist nur nach ausdrücklichem Opt-in
    (`SLUICE_NER_ALLOW_PLAINTEXT_REMOTE=1`) erlaubt — dieselbe Haltung wie bei fail-open-Modi
    (§4.1, Rev. 11): eine schwächere Absicherung gibt es nur nach bewusster Wahl, nie durch
    Vergessen. `https://` und Loopback sind frei.
    """
    url = (config.url or os.environ.get("SLUICE_NER_URL", "")).strip()
    if not url:
        raise NerUnavailableError(
            "NER-Dienst nicht konfiguriert: [profile.<name>.ner] url oder SLUICE_NER_URL "
            "setzen. Ohne Dienst blockiert der Modus fail-closed (§5.3)."
        )

    parsed = urlparse(url)
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname or ""):
        allowed = os.environ.get(ALLOW_PLAINTEXT_REMOTE_ENV, "").strip().lower()
        if allowed not in ("1", "true", "yes"):
            raise NerUnavailableError(
                f"NER-Dienst '{url}' liegt auf einem anderen Host und spricht unverschlüsseltes "
                f"HTTP. Über diesen Hop geht **Rohtext** (unsanitisiert) — mehr Personenbezug "
                f"als über den späteren Provider-Aufruf. Entweder TLS/Tunnel verwenden "
                f"(https://, WireGuard, stunnel) oder den Klartext-Transport ausdrücklich "
                f"erklären: {ALLOW_PLAINTEXT_REMOTE_ENV}=1 (§5.3, fail-closed)."
            )
    return url.rstrip("/")


def content_key(text: str, config: NerConfig) -> str:
    """Inhalts-Hash über Text **und** alle identitätsrelevanten Parameter (§5.4).

    Der Schlüssel muss die Parameter mitführen, sonst liefert der Cache nach einer
    Schwellwert- oder Label-Änderung noch die alten Spans — der Cache würde die
    Anonymisierungs-Identität aushebeln, die er eigentlich stabilisieren soll.
    """
    material = json.dumps(
        {
            "text": text,
            "threshold": config.threshold,
            "labels": list(config.labels),
            "model_repo": config.model_repo,
            "model_revision": config.model_revision,
            "model_precision": config.model_precision,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class NerClient:
    """Ruft `POST /v1/detect` und filtert auf den profilverankerten Schwellwert.

    Der Cache ist prozesslokal und rein additiv: er macht *Wiederholungen* bitgleich
    und senkt die Latenz (§5.4). Die Determinismus-Zusage über **Prozessneustarts**
    hinweg trägt er nicht — die kommt aus der fixierten Batchgröße und Präzision im
    Dienst (§5.4, `FIXED_BATCH_SIZE`).
    """

    def __init__(
        self, config: NerConfig, *, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._client = http_client
        self._cache: OrderedDict[str, tuple[NerSpan, ...]] = OrderedDict()
        self._info: ServiceInfo | None = None
        self._identity_checked = False

    @property
    def config(self) -> NerConfig:
        return self._config

    def _timeout(self) -> httpx.Timeout:
        """Endliches Budget auf allen Achsen — ein Riss zählt als Ausfall (§5.3).

        Anders als beim Provider-Streaming (dort: kein Read-Timeout, weil agentische
        Turns lange streamen) ist die NER-Erkennung ein kurzer, begrenzter Aufruf im
        *synchronen* Egress-Pfad. Ein unbegrenztes Read-Timeout würde den Chokepoint
        bei einem hängenden Dienst blockieren statt ihn fail-closed abzuweisen.
        """
        return httpx.Timeout(self._config.timeout_seconds)

    async def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        url = resolve_url(self._config)
        client = self._client or httpx.AsyncClient(timeout=self._timeout())
        try:
            response = await client.post(f"{url}{path}", json=payload, timeout=self._timeout())
        except httpx.TimeoutException as exc:
            raise NerUnavailableError(
                f"NER-Dienst Timeout nach {self._config.timeout_seconds}s ({url}{path}): {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise NerUnavailableError(f"NER-Dienst nicht erreichbar ({url}{path}): {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code != 200:
            raise NerUnavailableError(
                f"NER-Dienst antwortet HTTP {response.status_code} ({url}{path}): "
                f"{response.text[:500]}"
            )
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise NerUnavailableError(f"NER-Dienst liefert kein gültiges JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise NerUnavailableError("NER-Dienst liefert kein JSON-Objekt.")
        return data

    async def info(self) -> ServiceInfo:
        """`GET /v1/info` — Modellidentität für die Profilverankerung (§5.4)."""
        url = resolve_url(self._config)
        client = self._client or httpx.AsyncClient(timeout=self._timeout())
        try:
            response = await client.get(f"{url}/v1/info", timeout=self._timeout())
        except httpx.TimeoutException as exc:
            raise NerUnavailableError(f"NER-Dienst Timeout bei /v1/info: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NerUnavailableError(f"NER-Dienst nicht erreichbar bei /v1/info: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code != 200:
            raise NerUnavailableError(f"NER-Dienst /v1/info: HTTP {response.status_code}")
        data = response.json()
        return ServiceInfo(
            model=str(data.get("model", "")),
            revision=str(data.get("revision", "")),
            precision=str(data.get("precision", "")),
            labels=tuple(data.get("labels", ()) or ()),
            backend=str(data.get("backend", "")),
            score_floor=float(data.get("score_floor", 0.0) or 0.0),
        )

    async def ensure_identity(self) -> ServiceInfo:
        """Prüft die gemeldete Modellidentität gegen die Profilverankerung (§5.4).

        Nur was das Profil *deklariert*, wird geprüft — ein Profil ohne `model_repo`
        verankert eben nichts und bekommt die gemeldete Identität nur zu sehen. Weicht
        ein deklarierter Wert ab, ist das fail-closed: liefe die Anfrage weiter, wäre
        die im Audit protokollierte Anonymisierungs-Identität nicht die tatsächliche.
        """
        if self._identity_checked and self._info is not None:
            return self._info

        info = await self.info()
        expected = (
            ("model_repo", self._config.model_repo, info.model),
            ("model_revision", self._config.model_revision, info.revision),
            ("model_precision", self._config.model_precision, info.precision),
        )
        for field_name, declared, reported in expected:
            if declared and declared != reported:
                raise NerIdentityError(
                    f"NER-Modellidentität weicht ab: Profil verankert {field_name}="
                    f"'{declared}', Dienst meldet '{reported}' (§5.4, fail-closed)."
                )

        # Der Dienst darf nicht schärfer vorfiltern als das Profil filtert — sonst wäre
        # der profilverankerte Schwellwert wirkungslos und die Recall-Zusage (§5.4) eine
        # Behauptung ohne Deckung.
        if info.score_floor > self._config.threshold:
            raise NerIdentityError(
                f"NER-Dienst filtert bei score_floor={info.score_floor} schärfer vor als "
                f"der Profil-Schwellwert {self._config.threshold}; die verankerte Schwelle "
                f"wäre wirkungslos (§5.4, fail-closed). SLUICE_NER_SCORE_FLOOR senken."
            )

        self._info = info
        self._identity_checked = True
        log.info(
            "ner.identity",
            model=info.model,
            revision=info.revision,
            precision=info.precision,
            backend=info.backend,
        )
        return info

    async def detect(self, text: str) -> tuple[NerSpan, ...]:
        """Spans oberhalb des Schwellwerts — **ein** Text pro Aufruf (§5.4).

        Kein dynamisches Batching: die Batchgröße ist auf `FIXED_BATCH_SIZE` fixiert,
        damit die Reduktionsreihenfolge in Gleitkommaoperationen nicht mit der Auslastung
        variiert und Grenzfälle am Schwellwert nicht zwischen Läufen kippen.
        """
        if not text.strip():
            return ()

        key = content_key(text, self._config)
        if self._config.cache_size > 0 and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        await self.ensure_identity()
        data = await self._post(
            "/v1/detect",
            {
                "text": text,
                "labels": list(self._config.labels),
                "batch_size": FIXED_BATCH_SIZE,
            },
        )

        raw_spans = data.get("spans")
        if not isinstance(raw_spans, list):
            raise NerUnavailableError("NER-Antwort ohne Feld 'spans' (fail-closed).")

        spans = tuple(
            sorted(
                (s for s in (_parse_span(raw, len(text)) for raw in raw_spans) if s is not None),
                # Totale Ordnung, damit die Ausgabe reproduzierbar ist (§5.4).
                key=lambda s: (s.start, -s.end, s.label),
            )
        )
        # Schwellwert-Filter liegt HIER, nicht im Dienst — er ist Teil der
        # Anonymisierungs-Identität von Sluice (§5.4/§7.5).
        kept = tuple(s for s in spans if s.score >= self._config.threshold)

        if self._config.cache_size > 0:
            self._cache[key] = kept
            self._cache.move_to_end(key)
            while len(self._cache) > self._config.cache_size:
                self._cache.popitem(last=False)
        return kept


def _parse_span(raw: object, text_length: int) -> NerSpan | None:
    """Ein Span aus der Dienst-Antwort; unbrauchbare Einträge werden verworfen.

    Verwerfen ist hier **nicht** fail-open: ein Span, dessen Offsets außerhalb des
    Textes liegen oder nicht ganzzahlig sind, ist keine Erkennung, die man maskieren
    könnte — er würde `apply_spans` beschädigen. Ein *strukturell* kaputte Antwort
    (kein `spans`-Feld) blockiert dagegen oben fail-closed.
    """
    if not isinstance(raw, dict):
        return None
    try:
        start = int(raw["start"])
        end = int(raw["end"])
        label = str(raw["label"])
        score = float(raw.get("score", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= start < end <= text_length:
        return None
    return NerSpan(start=start, end=end, label=label, score=score)


__all__ = ["ALLOW_PLAINTEXT_REMOTE_ENV", "NerClient", "content_key", "resolve_url"]
