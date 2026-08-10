"""NER-Stufe — Konfiguration, Fehler und Vertrag (Spec §5.3/§7.5, Rev. 12).

Die **zweite** Erkennungsstufe der Modi `pii_regex`/`pii_ner` (§3). Sie deckt genau das
ab, was keine feste Form hat und deshalb prinzipiell außerhalb der Regex-Stufe liegt:
Personennamen, Organisationen, Freitext-Adressen, Ortsangaben. Sie **ersetzt die
Regex-Stufe nie** (§5.3) — eine Prüfsumme ist eine Gewissheit, ein Modell-Score nicht.

Der NER-Dienst ist ein **eigenständiger Prozess mit bewusst schmaler Schnittstelle**
(§7.5): Text rein, Spans raus. Er enthält **keine** Anonymisierungslogik — keine
Pseudonym-Zuordnung, keinen Modus-Schalter, keine Profilbindung, keine Maskierung. Das
alles bleibt in Sluice. Erst diese Trennung macht das Modell austauschbar und erlaubt,
zwei Modelle vergleichend zu betreiben, ohne den Chokepoint zu duplizieren.

**Fail-closed (§5.3):** Ist der Dienst nicht erreichbar oder reißt das Timeout-Budget,
wird die Anfrage **blockiert**. Kein stiller Rückfall auf die Regex-Stufe, keine
Degradation — ein Chokepoint, der bei Ausfall durchlässiger wird, ist keiner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sluice.errors import ModeUnavailableError

# Recall-orientierter Startwert, **bewusst unter dem F1-Optimum** (§5.4): Übermaskierung
# ist ein Kostenfaktor, ein übersehener Span ist ein Datenleck. Der Wert ist NUR der
# unkalibrierte Default — die produktive Schwelle gehört ins Profil (`threshold`) und
# wird auf einem Dev-Split kalibriert (scripts/eval_ner.py). Ein Profil ohne eigenen
# Wert wird in der Anonymisierungs-Identität als `calibrated=false` ausgewiesen.
DEFAULT_THRESHOLD = 0.30

# Entitätstypen, die die NER-Stufe anfordert. GLiNER nimmt die Typen zur Laufzeit als
# Label-Liste entgegen — deshalb ist die Liste Konfiguration und Teil der versionierten
# Anonymisierungs-Identität (§5.4), nicht im Code verdrahtet.
DEFAULT_LABELS: tuple[str, ...] = (
    "person",
    "organization",
    "address",
    "location",
)

# Label → typisierter Platzhalter der Redaktion. Unbekannte Labels fallen auf `[PII]`
# zurück (fail-closed: lieber maskiert und generisch etikettiert als durchgelassen).
LABEL_PLACEHOLDERS: dict[str, str] = {
    "person": "[PERSON]",
    "organization": "[ORGANISATION]",
    "address": "[ADRESSE]",
    "location": "[ORT]",
}
FALLBACK_PLACEHOLDER = "[PII]"

# Fixierte Batchgröße (§5.4). **Kein dynamisches Batching** — sonst ändert sich die
# Reduktionsreihenfolge in Gleitkommaoperationen und Grenzfälle am Schwellwert kippen
# zwischen sonst identischen Läufen. 1 ist die einzige Größe, die das strukturell
# garantiert, unabhängig von der Auslastung des Dienstes.
FIXED_BATCH_SIZE = 1

DEFAULT_TIMEOUT_SECONDS = 5.0


class NerError(ModeUnavailableError):
    """Basis aller NER-Fehler.

    Erbt von `ModeUnavailableError`, damit der Guard generisch am *Mechanismus* greift
    („ein Modus kann nicht liefern") statt NER namentlich zu kennen — dieselbe Trennung
    wie bei `enforce_verifier` (§3): der Guard prüft Merkmale, keine Modus-Namen.
    """


class NerUnavailableError(NerError):
    """Der NER-Dienst ist nicht erreichbar, antwortet fehlerhaft oder reißt das Timeout.

    Führt **immer** zur Blockade der Anfrage (§5.3). Wird vom Guard als eigener
    Fehlertyp geführt, damit ein Ausfall im Audit und beim Aufrufer nicht mit einer
    Policy-Ablehnung verwechselt wird.
    """


class NerIdentityError(NerError):
    """Der Dienst meldet eine andere Modellidentität als das Profil verankert (§5.4).

    Ebenfalls fail-closed: liefe die Anfrage weiter, wäre die im Audit protokollierte
    Anonymisierungs-Identität eine Lüge.
    """


@dataclass(frozen=True)
class NerConfig:
    """Profilverankerte NER-Konfiguration (§4/§5.4, Rev. 12).

    url:              Basis-URL des NER-Dienstes; fehlt sie (auch als `SLUICE_NER_URL`),
                      ist das ein Konfigurationsfehler ⇒ fail-closed, nie ein Bypass.
    threshold:        Konfidenz-Schwelle, **recall-optimiert** (§5.4). Versionierter
                      Konfigurationswert, keine Code-Konstante.
    labels:           angeforderte Entitätstypen — Teil der Identität (§5.4).
    timeout_seconds:  Timeout-Budget; ein Riss zählt als Ausfall (§5.3).
    model_repo/_revision/_precision: die verankerte Modellidentität. Sind sie gesetzt,
                      prüft der Client sie gegen `GET /info` und blockiert bei Abweichung.
    cache_size:       Einträge des Inhalts-Hash-Caches (§5.4) — macht Wiederholungen
                      bitgleich und senkt die Latenz. 0 schaltet ihn ab.
    threshold_declared: ob das Profil den Schwellwert selbst gesetzt hat. Nur für die
                      Ehrlichkeit der Identität — ein geerbter Default heißt unkalibriert.
    """

    url: str | None = None
    threshold: float = DEFAULT_THRESHOLD
    labels: tuple[str, ...] = DEFAULT_LABELS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    model_repo: str = ""
    model_revision: str = ""
    model_precision: str = ""
    cache_size: int = 1024
    threshold_declared: bool = False


@dataclass(frozen=True)
class NerSpan:
    """Ein Span, wie ihn der Dienst liefert — Zeichen-Offsets (§7.5), nie Token-Offsets."""

    start: int
    end: int
    label: str
    score: float


@dataclass(frozen=True)
class ServiceInfo:
    """Antwort von `GET /info` — die Modellidentität, die Sluice ins Profil übernimmt.

    `score_floor` ist die grobe Vorfilterung des Dienstes. Sie muss **unter** jedem
    Profil-Schwellwert liegen, sonst wäre die profilverankerte Schwelle wirkungslos —
    der Dienst hätte die Spans schon weggeworfen, bevor Sluice sie sieht. Der Client
    prüft das fail-closed (§5.4).
    """

    model: str
    revision: str
    precision: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    backend: str = ""
    score_floor: float = 0.0


def placeholder_for(label: str) -> str:
    """Typisierter Platzhalter zu einem Modell-Label; unbekannt ⇒ generisch maskieren."""
    return LABEL_PLACEHOLDERS.get(label.strip().lower(), FALLBACK_PLACEHOLDER)


__all__ = [
    "DEFAULT_LABELS",
    "DEFAULT_THRESHOLD",
    "DEFAULT_TIMEOUT_SECONDS",
    "FALLBACK_PLACEHOLDER",
    "FIXED_BATCH_SIZE",
    "LABEL_PLACEHOLDERS",
    "NerConfig",
    "NerError",
    "NerIdentityError",
    "NerSpan",
    "NerUnavailableError",
    "ServiceInfo",
    "placeholder_for",
]
