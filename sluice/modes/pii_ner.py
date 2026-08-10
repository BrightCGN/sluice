"""PiiNerMode — Regex **plus** Modellerkennung (Spec §3/§5.3, Rev. 12).

`pii_ner` ist **additiv, nicht alternativ**: beide Stufen laufen, das Ergebnis ist die
**Vereinigungsmenge** der erkannten Spans. Der Modus erbt die Regex-Stufe wörtlich von
`PiiRegexMode` — nicht als Kopie, sondern als Unterklasse, damit die Zusage
„`pii_ner` erkennt alles, was `pii_regex` erkennt" strukturell gilt und nicht bloß
per Testfall.

**Warum das Modell die Regex-Stufe nie ersetzt (§5.3):** strukturierte Identifikatoren
sind über Prüfsummen zu 100 % präzise erkennbar (Mod-97, Luhn, die Prüfziffernverfahren
für Steuer-ID/SVNR/KVNR). Kein NER-Modell erreicht das. Das Modell übernimmt
ausschließlich, was Regex prinzipiell nicht kann: Personennamen, Organisationen,
Freitext-Adressen und Ortsangaben, kontextabhängige Fälle ohne festes Format. Bei
**überlappenden Spans hat die Regex-Erkennung Vorrang**, weil nur sie den *validierten*
Typ kennt (`merge_spans`, §5.3).

**Fail-closed (§5.3):** ist der NER-Dienst nicht erreichbar oder reißt das Timeout-Budget,
wird die Anfrage **blockiert**. Es gibt hier bewusst **kein** `except` um den
Client-Aufruf, das auf die Regex-Stufe zurückfiele — ein solcher Rückfall wäre eine
stille Degradation, und ein Chokepoint, der bei Ausfall durchlässiger wird, ist keiner.
Der Fehler (`NerUnavailableError`) läuft als eigener Typ bis zum Aufrufer durch und wird
im Audit als solcher geführt, nicht als Policy-Ablehnung.
"""

from __future__ import annotations

import httpx

from sluice.identity import AnonymizationIdentity, build_identity
from sluice.ner import NerConfig, placeholder_for
from sluice.ner.client import NerClient
from sluice.spans import SOURCE_NER, Span, detect_regex_spans, merge_spans
from sluice.modes.pii_regex import PiiRegexMode


class PiiNerMode(PiiRegexMode):
    reversible = False
    name = "pii_ner"
    enforce_verifier = True  # der Verifier bleibt auch hier der harte Boden (§5)

    def __init__(
        self,
        *,
        detector_profile: str = "pii_de",
        dictionary_terms: tuple[str, ...] = (),
        ner_config: NerConfig | None = None,
        client: NerClient | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            detector_profile=detector_profile, dictionary_terms=dictionary_terms
        )
        self._ner_config = ner_config if ner_config is not None else NerConfig()
        self._client = (
            client
            if client is not None
            else NerClient(self._ner_config, http_client=http_client)
        )

    @property
    def ner_config(self) -> NerConfig:
        return self._ner_config

    def identity(self) -> AnonymizationIdentity:
        """Anonymisierungs-Identität inkl. Modell-Repo, Revision, Präzision, Schwellwert (§5.4)."""
        return build_identity(
            mode=self.name,
            detector_profile=self._detector_profile,
            dictionary_terms=self._dictionary_terms,
            ner_config=self._ner_config,
        )

    async def spans_for(self, text: str) -> tuple[Span, ...]:
        """Vereinigungsmenge beider Stufen — Regex zuerst, NER ergänzend (§5.3).

        Die Regex-Stufe läuft **immer und zuerst**; erst danach wird das Modell gefragt.
        Schlägt der Dienst fehl, propagiert der Fehler — es wird nicht mit dem
        Regex-Teilergebnis weitergemacht.
        """
        regex_spans = detect_regex_spans(
            text, self._detector_profile, dictionary_terms=self._dictionary_terms
        )
        ner_spans = tuple(
            Span(
                start=s.start,
                end=s.end,
                label=placeholder_for(s.label),
                source=SOURCE_NER,
                finding=f"NER-Entität '{s.label}' erkannt (score={s.score:.3f})",
                score=s.score,
            )
            for s in await self._client.detect(text)
        )
        return merge_spans(regex_spans, ner_spans)
