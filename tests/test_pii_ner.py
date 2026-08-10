"""`pii_ner` — Vereinigung, Fail-closed, Determinismus, Identität (Spec §5.3/§5.4, Rev. 12).

Diese Suite bildet die Akzeptanzkriterien 1:1 ab. Kein Netz und kein Modell: der
NER-Dienst wird über `httpx.MockTransport` gefahren (Test-Disziplin), der Dienst selbst
über einen injizierten Fake-Engine getestet.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sluice.audit import AuditLog
from sluice.errors import ModeUnavailableError
from sluice.guard import guarded_egress
from sluice.modes import EgressPayload, select_mode
from sluice.modes.pii_ner import PiiNerMode
from sluice.modes.pii_regex import PiiRegexMode
from sluice.ner import NerConfig, NerIdentityError, NerUnavailableError
from sluice.ner.client import NerClient
from sluice.policy import Profile, parse_profiles

MODEL = "fastino/gliner2-privacy-filter-PII-multi"
REVISION = "a1b2c3d4e5f6"

# „Richard Cochius" ist für die Regex-Stufe unsichtbar (freier Name ohne feste Form) —
# genau die Lücke, die die NER-Stufe schließt (§5.3).
TEXT = "Richard Cochius, IBAN DE89 3704 0044 0532 0130 00, Acme GmbH in Köln"
NAME_START, NAME_END = 0, 15
ORG_START, ORG_END = TEXT.index("Acme GmbH"), TEXT.index("Acme GmbH") + len("Acme GmbH")


def ner_transport(
    spans: list[dict[str, Any]],
    *,
    info_overrides: dict[str, Any] | None = None,
    detect_status: int = 200,
    fail: Exception | None = None,
    record: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """Fake-NER-Dienst. `fail` simuliert Ausfall/Timeout, `record` protokolliert Requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail is not None:
            raise fail
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "model": MODEL,
                    "revision": REVISION,
                    "precision": "fp32",
                    "labels": ["person", "organization", "address", "location"],
                    "backend": "gliner-torch/cpu",
                    "score_floor": 0.05,
                    **(info_overrides or {}),
                },
            )
        if request.url.path.endswith("/detect"):
            if record is not None:
                record.append(json.loads(request.content))
            if detect_status != 200:
                return httpx.Response(detect_status, text="kaputt")
            return httpx.Response(200, json={"spans": spans})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_mode(
    spans: list[dict[str, Any]] | None = None,
    *,
    threshold: float = 0.30,
    detector_profile: str = "pii_de",
    **transport_kwargs: Any,
) -> PiiNerMode:
    default_spans = [
        {"start": NAME_START, "end": NAME_END, "label": "person", "score": 0.91},
        {"start": ORG_START, "end": ORG_END, "label": "organization", "score": 0.77},
    ]
    config = NerConfig(
        url="https://ner.invalid",
        threshold=threshold,
        model_repo=MODEL,
        model_revision=REVISION,
        model_precision="fp32",
        threshold_declared=True,
    )
    transport = ner_transport(
        default_spans if spans is None else spans, **transport_kwargs
    )
    client = NerClient(config, http_client=httpx.AsyncClient(transport=transport))
    return PiiNerMode(
        detector_profile=detector_profile, ner_config=config, client=client
    )


# ---------- Akzeptanzkriterium 1: additiv, nicht alternativ (§5.3) ----------


async def test_pii_ner_erkennt_alles_was_pii_regex_erkennt_plus_ner() -> None:
    """Die Vereinigungsmenge — das zentrale Versprechen des Modus."""
    regex_only = PiiRegexMode(detector_profile="pii_de")
    regex_spans = await regex_only.spans_for(TEXT)
    union_spans = await make_mode().spans_for(TEXT)

    regex_ranges = {(s.start, s.end) for s in regex_spans}
    union_ranges = {(s.start, s.end) for s in union_spans}

    assert regex_ranges, "Die Regex-Stufe muss hier überhaupt etwas finden"
    assert regex_ranges <= union_ranges, "pii_ner muss alles von pii_regex enthalten"
    assert len(union_ranges) > len(regex_ranges), "und zusätzlich NER-Spans"

    labels = {s.label for s in union_spans}
    assert "[IBAN]" in labels  # Regex-Stufe
    assert "[PERSON]" in labels  # NER-Stufe
    assert "[ORGANISATION]" in labels


async def test_pii_ner_ist_unterklasse_von_pii_regex() -> None:
    """Strukturell, nicht per Testfall: die Regex-Stufe kann gar nicht wegfallen."""
    assert issubclass(PiiNerMode, PiiRegexMode)


async def test_regex_hat_vorrang_bei_ueberlappung() -> None:
    """Überlappt ein NER-Span eine validierte IBAN, gewinnt die Regex-Erkennung (§5.3)."""
    iban_start = TEXT.index("DE89")
    mode = make_mode(
        [{"start": iban_start, "end": iban_start + 10, "label": "person", "score": 0.99}]
    )
    spans = await mode.spans_for(TEXT)
    overlapping = [s for s in spans if s.start <= iban_start < s.end]
    assert len(overlapping) == 1
    assert overlapping[0].label == "[IBAN]"
    assert overlapping[0].source == "regex"


async def test_redaktion_ersetzt_beide_stufen() -> None:
    sanitized = await make_mode().forward(EgressPayload(raw_text=TEXT), None)
    assert "Richard Cochius" not in sanitized.text
    assert "DE89" not in sanitized.text
    assert "Acme GmbH" not in sanitized.text
    assert "[PERSON]" in sanitized.text and "[IBAN]" in sanitized.text


# ---------- Akzeptanzkriterium 2: Determinismus (§5.4) ----------


async def test_identische_eingabe_liefert_identische_spans_ueber_laeufe() -> None:
    mode = make_mode()
    first = await mode.spans_for(TEXT)
    second = await mode.spans_for(TEXT)
    third = await mode.spans_for(TEXT)
    assert first == second == third


async def test_determinismus_ueber_frische_instanzen() -> None:
    """Steht für den Prozessneustart: neue Instanz, neuer Cache, gleiches Ergebnis."""
    first = await make_mode().spans_for(TEXT)
    second = await make_mode().spans_for(TEXT)
    assert first == second


async def test_cache_spart_den_zweiten_dienstaufruf() -> None:
    record: list[dict[str, Any]] = []
    mode = make_mode(record=record)
    await mode.spans_for(TEXT)
    await mode.spans_for(TEXT)
    assert len(record) == 1, "Der Inhalts-Hash-Cache muss den zweiten Aufruf einsparen"


async def test_cache_schluessel_enthaelt_den_schwellwert() -> None:
    """Sonst überlebte ein alter Wert eine Schwellwert-Änderung — der Cache würde die
    Anonymisierungs-Identität aushebeln, die er stabilisieren soll."""
    from sluice.ner.client import content_key

    low = NerConfig(url="http://x", threshold=0.30)
    high = NerConfig(url="http://x", threshold=0.80)
    assert content_key(TEXT, low) != content_key(TEXT, high)


async def test_kein_dynamisches_batching() -> None:
    record: list[dict[str, Any]] = []
    await make_mode(record=record).spans_for(TEXT)
    assert record[0]["batch_size"] == 1


# ---------- Akzeptanzkriterium 3: Recall vor Precision (§5.4) ----------


async def test_schwellwert_filtert_und_ist_konfigurierbar() -> None:
    spans = [
        {"start": NAME_START, "end": NAME_END, "label": "person", "score": 0.42},
        {"start": ORG_START, "end": ORG_END, "label": "organization", "score": 0.18},
    ]
    # Recall-orientiert niedrig: beide Spans überleben.
    low = await make_mode(spans, threshold=0.15).spans_for(TEXT)
    assert len([s for s in low if s.source == "ner"]) == 2
    # Höher kalibriert: der schwache Span fällt weg.
    high = await make_mode(spans, threshold=0.40).spans_for(TEXT)
    assert len([s for s in high if s.source == "ner"]) == 1


async def test_dienst_darf_nicht_schaerfer_vorfiltern_als_das_profil() -> None:
    """Sonst wäre der profilverankerte Schwellwert wirkungslos (§5.4, fail-closed)."""
    mode = make_mode(threshold=0.20, info_overrides={"score_floor": 0.50})
    with pytest.raises(NerIdentityError):
        await mode.spans_for(TEXT)


# ---------- Akzeptanzkriterium 4: Fail-closed (§5.3) ----------


async def test_dienst_nicht_erreichbar_blockiert() -> None:
    mode = make_mode(fail=httpx.ConnectError("connection refused"))
    with pytest.raises(NerUnavailableError):
        await mode.spans_for(TEXT)


async def test_timeout_zaehlt_als_ausfall() -> None:
    mode = make_mode(fail=httpx.ReadTimeout("zu langsam"))
    with pytest.raises(NerUnavailableError):
        await mode.spans_for(TEXT)


async def test_http_fehler_blockiert() -> None:
    mode = make_mode(detect_status=500)
    with pytest.raises(NerUnavailableError):
        await mode.spans_for(TEXT)


async def test_fehlende_url_blockiert_statt_zu_ueberspringen() -> None:
    mode = PiiNerMode(detector_profile="pii_de", ner_config=NerConfig(url=None))
    with pytest.raises(NerUnavailableError):
        await mode.spans_for(TEXT)


async def test_abweichende_modellidentitaet_blockiert() -> None:
    mode = make_mode(info_overrides={"revision": "eine-ganz-andere-revision"})
    with pytest.raises(NerIdentityError):
        await mode.spans_for(TEXT)


async def test_ner_fehler_ist_ein_modus_ausfall() -> None:
    """Der Guard greift generisch am Merkmal, nicht am Namen `pii_ner` (§3)."""
    assert issubclass(NerUnavailableError, ModeUnavailableError)


async def test_guard_blockiert_bei_ausfall_mit_eigenem_fehlertyp() -> None:
    """Kein stiller Rückfall auf die Regex-Stufe — und der Fehler ist unterscheidbar."""
    profile = Profile(
        name="p", mode="pii_ner", allowed_purposes=("x",), detector_profile="pii_de"
    )
    mode = make_mode(fail=httpx.ConnectError("weg"))
    audit = AuditLog(level="metadata")

    outcome = await guarded_egress(
        profile=profile,
        purpose="x",
        payload=EgressPayload(raw_text=TEXT),
        mode=mode,
        audit=audit,
    )

    assert outcome.released is False
    assert outcome.error_type == "mode_unavailable"
    assert outcome.sanitized_text is None
    # Nichts von der Regex-Stufe darf als Teilergebnis durchsickern.
    assert "[IBAN]" not in (outcome.sanitized_text or "")
    # Und der Ausfall steht im Audit (§6).
    assert audit.entries[-1].released is False
    assert "nicht verfügbar" in audit.entries[-1].reason


# ---------- Akzeptanzkriterium 3 (Identität): §5.4 ----------


def test_identitaet_traegt_modellversion_und_schwellwert() -> None:
    profile = Profile(
        name="p",
        mode="pii_ner",
        allowed_purposes=("x",),
        detector_profile="pii_de",
        ner=NerConfig(
            url="https://ner.invalid",
            threshold=0.27,
            model_repo=MODEL,
            model_revision=REVISION,
            model_precision="fp32",
            threshold_declared=True,
        ),
    )
    identity = profile.anonymization_identity().as_dict()
    assert identity["mode"] == "pii_ner"
    assert identity["ner"]["model_repo"] == MODEL
    assert identity["ner"]["model_revision"] == REVISION
    assert identity["ner"]["threshold"] == 0.27
    assert identity["ner"]["model_precision"] == "fp32"
    assert identity["ner"]["batch_size"] == 1
    assert identity["ner"]["threshold_calibrated"] is True


def test_identitaets_digest_aendert_sich_mit_schwellwert_und_revision() -> None:
    """Ein stiller Modell- oder Schwellwert-Wechsel wird dadurch im Audit sichtbar."""
    base = Profile(
        name="p",
        mode="pii_ner",
        detector_profile="pii_de",
        ner=NerConfig(model_repo=MODEL, model_revision=REVISION, threshold=0.30),
    )
    other_threshold = Profile(
        name="p",
        mode="pii_ner",
        detector_profile="pii_de",
        ner=NerConfig(model_repo=MODEL, model_revision=REVISION, threshold=0.31),
    )
    other_revision = Profile(
        name="p",
        mode="pii_ner",
        detector_profile="pii_de",
        ner=NerConfig(model_repo=MODEL, model_revision="ffffff", threshold=0.30),
    )
    d = base.anonymization_identity().digest()
    assert d != other_threshold.anonymization_identity().digest()
    assert d != other_revision.anonymization_identity().digest()
    # Stabil über Aufrufe hinweg — sonst wäre er als Fingerabdruck wertlos.
    assert d == base.anonymization_identity().digest()


def test_unkalibrierter_schwellwert_ist_als_solcher_ausgewiesen() -> None:
    """Ehrlichkeit statt stiller Default: ein geerbter Wert heißt unkalibriert."""
    profile = Profile(name="p", mode="pii_ner", ner=NerConfig(url="http://x"))
    assert profile.anonymization_identity().as_dict()["ner"]["threshold_calibrated"] is False


# ---------- Profil-Verankerung (§4) ----------


def test_profil_toml_verankert_die_ner_konfiguration() -> None:
    profiles = parse_profiles(
        f"""
        [profile."prismclaw"]
        mode               = "pii_ner"
        egress_enabled     = true
        allowed_purposes   = ["chat"]
        provider_allowlist = ["claude"]
        detector_profile   = "pii_de"
          [profile."prismclaw".ner]
          url             = "http://127.0.0.1:17900"
          threshold       = 0.28
          labels          = ["person", "organization", "address", "location"]
          timeout_seconds = 4.0
          model_repo      = "{MODEL}"
          model_revision  = "{REVISION}"
          model_precision = "fp32"
        """
    )
    profile = profiles["prismclaw"]
    assert profile.mode == "pii_ner"
    assert profile.ner is not None
    assert profile.ner.threshold == 0.28
    assert profile.ner.timeout_seconds == 4.0
    assert profile.ner.model_revision == REVISION
    assert profile.ner.threshold_declared is True
    # Und der Schalter zieht daraus den passenden Modus (§3).
    assert select_mode(profile).name == "pii_ner"


def test_unsinniger_schwellwert_faellt_beim_laden_auf() -> None:
    with pytest.raises(ValueError, match="threshold"):
        parse_profiles(
            """
            [profile."x"]
            mode = "pii_ner"
              [profile."x".ner]
              threshold = 1.4
            """
        )


# ---------- Transport-Schranke bei entferntem NER-Dienst (§5.3, Rev. 12) ----------
#
# Der NER-Dienst ist die EINZIGE Komponente, die unsanitisierten Rohtext sieht. Liegt er
# auf einem anderen Host, trägt dieser Hop mehr Personenbezug als der spätere
# Provider-Aufruf — und der geht über TLS. Klartext-HTTP über das Netz ist deshalb nur
# nach ausdrücklicher Erklärung erlaubt.


def test_loopback_http_ist_frei(monkeypatch: pytest.MonkeyPatch) -> None:
    from sluice.ner.client import resolve_url

    monkeypatch.delenv("SLUICE_NER_ALLOW_PLAINTEXT_REMOTE", raising=False)
    for url in ("http://127.0.0.1:17900", "http://localhost:17900", "http://[::1]:17900"):
        assert resolve_url(NerConfig(url=url)) == url


def test_entfernter_dienst_ueber_https_ist_frei(monkeypatch: pytest.MonkeyPatch) -> None:
    from sluice.ner.client import resolve_url

    monkeypatch.delenv("SLUICE_NER_ALLOW_PLAINTEXT_REMOTE", raising=False)
    url = "https://192.168.101.166:17900"
    assert resolve_url(NerConfig(url=url)) == url


def test_entfernter_dienst_ueber_klartext_http_blockiert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sluice.ner.client import resolve_url

    monkeypatch.delenv("SLUICE_NER_ALLOW_PLAINTEXT_REMOTE", raising=False)
    with pytest.raises(NerUnavailableError, match="Rohtext"):
        resolve_url(NerConfig(url="http://192.168.101.166:17900"))


def test_klartext_transport_nach_ausdruecklichem_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vergessen heißt zu, nicht offen — wie bei fail-open-Modi (§4.1, Rev. 11)."""
    from sluice.ner.client import resolve_url

    monkeypatch.setenv("SLUICE_NER_ALLOW_PLAINTEXT_REMOTE", "1")
    url = "http://192.168.101.166:17900"
    assert resolve_url(NerConfig(url=url)) == url


async def test_blockierter_transport_ist_ein_modus_ausfall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Der Guard behandelt das wie jeden anderen NER-Ausfall: blockieren, 503, kein Rückfall."""
    monkeypatch.delenv("SLUICE_NER_ALLOW_PLAINTEXT_REMOTE", raising=False)
    from sluice.modes.pii_ner import PiiNerMode

    profile = Profile(
        name="remote", mode="pii_ner", allowed_purposes=("x",), detector_profile="pii_de"
    )
    mode = PiiNerMode(
        detector_profile="pii_de",
        ner_config=NerConfig(url="http://192.168.101.166:17900"),
    )
    outcome = await guarded_egress(
        profile=profile,
        purpose="x",
        payload=EgressPayload(raw_text=TEXT),
        mode=mode,
        audit=AuditLog(level="metadata"),
    )
    assert outcome.released is False
    assert outcome.error_type == "mode_unavailable"
