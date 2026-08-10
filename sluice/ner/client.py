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
    CONSERVATIVE_CHARS_PER_TOKEN,
    FIXED_BATCH_SIZE,
    NerConfig,
    NerIdentityError,
    NerSpan,
    NerTruncationError,
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
            # Andere Schnitte ⇒ andere Spans: die Zerlegungsparameter gehören in den
            # Schlüssel, sonst liefert der Cache nach einer Änderung die alte Zerlegung.
            "max_chars_per_chunk": config.max_chars_per_chunk,
            "chunk_overlap_chars": config.chunk_overlap_chars,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def split_chunks(
    text: str, max_chars: int, overlap: int
) -> tuple[tuple[int, str], ...]:
    """Zerlegt `text` in überlappende Stücke — `(offset_im_original, stück)` (§5.3).

    Nötig, weil das Modell ein festes Token-Fenster hat und längere Eingaben **still**
    kürzt. Ohne Zerlegung prüft `pii_ner` bei langen Texten nur den Anfang und meldet
    trotzdem Erfolg — ein Recall-Loch, das im Betrieb niemandem auffiele.

    Zwei Eigenschaften, auf die es ankommt:

    - **Überlappung.** Eine Entität genau an der Schnittstelle wäre in beiden Stücken nur
      halb enthalten und würde in beiden verfehlt. Der Überlappungsbereich sorgt dafür,
      dass sie in mindestens einem Stück vollständig vorkommt. Er muss deshalb länger
      sein als die längste erwartete Entität.
    - **Deterministisch.** Gleicher Text und gleiche Parameter ⇒ gleiche Schnitte ⇒
      gleiche Spans (§5.4). Es wird nichts an Satzenden geraten und nichts an der
      Auslastung ausgerichtet; geschnitten wird an der letzten Wortgrenze vor der
      Grenze, und wenn es keine gibt, hart.
    """
    if max_chars <= 0:
        raise NerUnavailableError(
            "max_chars_per_chunk muss > 0 sein (§5.3) — ohne Stückgröße ist keine "
            "Zerlegung möglich und lange Texte würden still gekürzt."
        )
    if len(text) <= max_chars:
        return ((0, text),)

    # Die Überlappung darf die Stückgröße nicht auffressen, sonst käme die Zerlegung
    # nicht voran. Die Hälfte ist die Grenze, ab der jeder Schnitt noch echten neuen
    # Text mitbringt.
    effective_overlap = max(0, min(overlap, max_chars // 2))

    chunks: list[tuple[int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            # Auf die letzte Wortgrenze zurückziehen, aber nie über die Hälfte des
            # Stücks hinaus — sonst würde ein sehr langes Wort das Stück zerbröseln.
            cut = text.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        chunks.append((start, text[start:end]))
        if end >= length:
            break
        # Fortschritt ist garantiert: `end` liegt immer > `start + max_chars // 2`,
        # und die Überlappung ist auf `max_chars // 2` gedeckelt.
        start = max(end - effective_overlap, start + 1)
    return tuple(chunks)


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
            # Eine gekürzte Eingabe ist kein Ausfall, sondern eine falsch dimensionierte
            # Zerlegung — als eigener Fehlertyp, damit im Betrieb nicht nach einem toten
            # Dienst gesucht wird. Blockierend ist beides (§5.3).
            if response.status_code == 413 and _error_type(response) == "ner_text_truncated":
                raise NerTruncationError(
                    f"NER-Dienst hat die Eingabe gekürzt ({url}{path}): "
                    f"{response.text[:500]} — max_chars_per_chunk im Profil senken."
                )
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
            max_tokens=int(data.get("max_tokens", 0) or 0),
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

        # Passt die konfigurierte Stückgröße überhaupt in das Fenster des Modells? Wenn
        # nicht, liefe jeder Aufruf in die Kürzung — und die ist ohne die Schranke in der
        # Engine ein *stilles* Recall-Loch. Lieber beim ersten Kontakt blockieren als bei
        # jedem langen Text unbemerkt den hinteren Teil auslassen (§5.3).
        #
        # Gerechnet wird mit einem bewusst pessimistischen Zeichen/Token-Verhältnis: hier
        # zu optimistisch zu sein hieße, die Kürzung erst im Betrieb zu bemerken.
        if info.max_tokens > 0:
            fits = int(info.max_tokens * CONSERVATIVE_CHARS_PER_TOKEN)
            if self._config.max_chars_per_chunk > fits:
                raise NerIdentityError(
                    f"Stückgröße {self._config.max_chars_per_chunk} Zeichen passt nicht in das "
                    f"Kontextfenster des Modells ({info.max_tokens} Token ≈ höchstens {fits} "
                    f"Zeichen). Das Modell würde kürzen und der hintere Teil bliebe ungeprüft "
                    f"(§5.3, fail-closed). max_chars_per_chunk im Profil senken."
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

    async def _detect_chunk(self, chunk: str) -> tuple[NerSpan, ...]:
        """Ein Stück gegen den Dienst — Offsets beziehen sich auf das Stück, nicht den Text."""
        data = await self._post(
            "/v1/detect",
            {
                "text": chunk,
                "labels": list(self._config.labels),
                "batch_size": FIXED_BATCH_SIZE,
            },
        )
        raw_spans = data.get("spans")
        if not isinstance(raw_spans, list):
            raise NerUnavailableError("NER-Antwort ohne Feld 'spans' (fail-closed).")
        return tuple(
            s for s in (_parse_span(raw, len(chunk)) for raw in raw_spans) if s is not None
        )

    async def detect(self, text: str) -> tuple[NerSpan, ...]:
        """Spans oberhalb des Schwellwerts — **ein** Text pro Aufruf (§5.4).

        Kein dynamisches Batching: die Batchgröße ist auf `FIXED_BATCH_SIZE` fixiert,
        damit die Reduktionsreihenfolge in Gleitkommaoperationen nicht mit der Auslastung
        variiert und Grenzfälle am Schwellwert nicht zwischen Läufen kippen.

        **Lange Texte werden zerlegt (§5.3).** Das Modell hat ein festes Token-Fenster und
        kürzt darüber hinaus still — ohne Zerlegung prüfte `pii_ner` nur den Anfang und
        meldete trotzdem Erfolg. Die Stücke überlappen, damit eine Entität an der
        Schnittstelle nicht in beiden Stücken halbiert und damit in beiden verfehlt wird.
        Die Offsets werden auf den Originaltext zurückgerechnet, denn nur dort redigiert
        Sluice (§5.3: Zeichen-Offsets, keine Token-Offsets).

        Ein Stück pro Aufruf, nacheinander: das hält die fixierte Batchgröße ein. Parallel
        zu senden wäre schneller, würde aber genau die Reihenfolgeabhängigkeit einführen,
        die §5.4 ausschließt.
        """
        if not text.strip():
            return ()

        key = content_key(text, self._config)
        if self._config.cache_size > 0 and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        await self.ensure_identity()

        chunks = split_chunks(
            text, self._config.max_chars_per_chunk, self._config.chunk_overlap_chars
        )
        if len(chunks) > 1:
            log.info("ner.chunked", chars=len(text), chunks=len(chunks))

        collected: list[NerSpan] = []
        for offset, chunk in chunks:
            for span in await self._detect_chunk(chunk):
                collected.append(
                    NerSpan(
                        start=span.start + offset,
                        end=span.end + offset,
                        label=span.label,
                        score=span.score,
                    )
                )

        spans = _dedupe_spans(collected)
        # Schwellwert-Filter liegt HIER, nicht im Dienst — er ist Teil der
        # Anonymisierungs-Identität von Sluice (§5.4/§7.5).
        kept = tuple(s for s in spans if s.score >= self._config.threshold)

        if self._config.cache_size > 0:
            self._cache[key] = kept
            self._cache.move_to_end(key)
            while len(self._cache) > self._config.cache_size:
                self._cache.popitem(last=False)
        return kept


def _error_type(response: httpx.Response) -> str:
    """`error.type` aus einer Fehlerantwort — leer, wenn der Body nichts hergibt."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return str(payload["error"].get("type", ""))
    return ""


def _dedupe_spans(spans: list[NerSpan]) -> tuple[NerSpan, ...]:
    """Doppelte Spans aus dem Überlappungsbereich zusammenführen (§5.3).

    Eine Entität im Überlappungsbereich wird zweimal gefunden — einmal je Stück, mit
    *unterschiedlichem Kontext* und deshalb potenziell leicht unterschiedlichem Score.
    Behalten wird der **höhere**: die Stufe ist recall-orientiert (§5.4), und wenn ein
    Kontext die Entität deutlicher zeigt, ist das die belastbarere Beobachtung.

    Nur exakte Dubletten (gleiche Offsets, gleiches Label) werden hier zusammengefasst.
    Echte Überlappungen *verschiedener* Spans bleiben stehen und werden erst in
    `spans.merge_spans` aufgelöst — dort, wo auch der Vorrang der Regex-Stufe entschieden
    wird. Zwei Stellen mit derselben Zuständigkeit wären eine zu viel.
    """
    best: dict[tuple[int, int, str], NerSpan] = {}
    for span in spans:
        key = (span.start, span.end, span.label)
        current = best.get(key)
        if current is None or span.score > current.score:
            best[key] = span
    # Totale Ordnung, damit die Ausgabe reproduzierbar ist (§5.4).
    return tuple(sorted(best.values(), key=lambda s: (s.start, -s.end, s.label)))


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


__all__ = [
    "ALLOW_PLAINTEXT_REMOTE_ENV",
    "NerClient",
    "content_key",
    "resolve_url",
    "split_chunks",
]
