"""Egress-Policy — Profil-Schema und Profil-Gate (Spec §4).

Das Profil ist die vollständige, auditierbare Form der Egress-Erlaubnis eines
Konsumenten. Default-Deny (§4.3): kein Profil → nichts raus, kein Vererben fremder
Profile. Souveränes Profil (§4.2): `egress_enabled=false` → Guard lässt **nichts**
durch, egal welcher Modus — der Riegel greift *vor* der Modus-Auswahl.

Rev. 9: Das Profil wählt einen **Modus** aus der Registry (Spec §3, `mode`); das alte
Feld `strategy` bleibt Parse-Alias und Lese-Property. Fehlt `mode`, gilt der **sichere
Default `strict`** (§4.3), nie `passthrough`. `allowed_modes` (§4.1) begrenzt optional,
welche Modi ein Request wählen darf (Default: alle erlaubt).

Herkunft: Tempers egress/policy.py, erweitert um das maschinenlesbare
TOML-Profil-Schema aus Spec §4.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sluice.detectors import UnknownDetectorProfile, merge_detector_profiles
from sluice.ner import (
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_LABELS,
    DEFAULT_MAX_CHARS_PER_CHUNK,
    DEFAULT_THRESHOLD,
    DEFAULT_TIMEOUT_SECONDS,
    NerConfig,
)

if TYPE_CHECKING:
    from sluice.identity import AnonymizationIdentity

DEFAULT_MODE = "strict"  # sicherer Auslieferungs-Default (Rev. 9, §4.3)
VALID_STORAGE = ("memory",)  # persistent ist post-v1 (Spec §8/§10)


@dataclass(frozen=True)
class ReversibleConfig:
    """Mapping-Lebenszyklus des pseudonymisierenden Modus (Spec §8)."""

    scope: str = "session"
    ttl_seconds: int = 3600
    storage: str = "memory"


@dataclass(frozen=True)
class Profile:
    """Ein Konsumenten-Profil — der Schalter, maschinenlesbar (Spec §4)."""

    name: str
    mode: str = DEFAULT_MODE  # Registry-Modus (§3); Default `strict` (Rev. 9, safety first, §4.3)
    egress_enabled: bool = True
    allowed_purposes: tuple[str, ...] = ()
    provider_allowlist: tuple[str, ...] = ()
    allowed_modes: tuple[str, ...] = ()  # leer = alle Modi erlaubt (§4.1, Rev. 9)
    detector_profile: str = "infra"
    dictionary_terms: tuple[str, ...] = ()  # konsument-deklarierte Literale (§5.1, Rev. 10)
    reversible: ReversibleConfig | None = None
    ner: NerConfig | None = None  # NER-Stufe + Anonymisierungs-Identität (§5.3/§5.4, Rev. 12)

    @property
    def strategy(self) -> str:
        """Rückwärtskompatibler Lese-Alias auf `mode` (Rev. 9)."""
        return self.mode

    def anonymization_identity(self) -> "AnonymizationIdentity":
        """Die profilverankerte Anonymisierungs-Identität (§5.4, Rev. 12).

        Verankert an derselben Stelle wie der Modus-Schalter selbst: das Profil
        entscheidet *ob* sanitisiert wird und legt damit auch fest *womit genau*.
        """
        from sluice.identity import build_identity  # lazy: vermeidet Import-Zyklus

        return build_identity(
            mode=self.mode,
            detector_profile=self.detector_profile,
            dictionary_terms=self.dictionary_terms,
            ner_config=self.ner,
        )


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str


def check_egress_allowed(profile: Profile | None, purpose: str) -> EgressDecision:
    """Profil-Gate — läuft immer zuerst (Spec §2, Invariante 1).

    Prüft, ob ein ausgehender Vorgang für dieses Profil überhaupt erlaubt ist,
    *bevor* irgendein Modus läuft.
    """
    if profile is None:
        return EgressDecision(
            allowed=False,
            reason="Default-Deny: kein Profil deklariert → nichts raus (§4.3).",
        )

    if not profile.egress_enabled:
        return EgressDecision(
            allowed=False,
            reason="Souveränes/air-gapped Profil: Egress technisch unterbunden (§4.2).",
        )

    if purpose not in profile.allowed_purposes:
        return EgressDecision(
            allowed=False,
            reason=f"Purpose '{purpose}' im Profil '{profile.name}' nicht erlaubt.",
        )

    return EgressDecision(allowed=True, reason=f"Profil '{profile.name}' erlaubt '{purpose}'.")


def check_provider_allowed(profile: Profile, provider: str) -> EgressDecision:
    """Provider-Allowlist pro Profil (Spec §4.1) — Provider divergieren in Retention/Training."""
    if provider not in profile.provider_allowlist:
        return EgressDecision(
            allowed=False,
            reason=f"Provider '{provider}' nicht in der Allowlist von '{profile.name}' (§4.1).",
        )
    return EgressDecision(allowed=True, reason=f"Provider '{provider}' erlaubt.")


def check_mode_allowed(
    profile: Profile, mode: str, *, enforce_verifier: bool = True
) -> EgressDecision:
    """Modus-Allowlist pro Profil (Spec §4.1, Rev. 9; fail-closed für fail-open-Modi Rev. 11).

    Zwei Schranken, beide fail-closed:

    1. **Explizite Allowlist:** Ist `allowed_modes` gesetzt, muss `mode` darin stehen —
       ein Betreiber sperrt so *jeden* nicht gelisteten Modus gezielt.
    2. **Fail-open-Opt-in (Rev. 11):** Ein Modus *ohne* Verifier (`enforce_verifier=False`,
       heute nur `passthrough`, §2.1) ist **nur** erlaubt, wenn er *ausdrücklich* in
       `allowed_modes` steht. Eine leere/fehlende Allowlist sperrt ihn — „Vergessen = zu".
       Verifizierende Modi bleiben per leerer Allowlist frei (sie leaken nicht, §5).

    `enforce_verifier` liefert der Guard aus der gewählten Modus-Instanz (§3), damit die
    Regel generisch am Modus-Merkmal greift, nicht am Namen `passthrough`.
    """
    if profile.allowed_modes and mode not in profile.allowed_modes:
        return EgressDecision(
            allowed=False,
            reason=f"Modus '{mode}' nicht in allowed_modes von '{profile.name}' (§4.1).",
        )
    if not enforce_verifier and mode not in profile.allowed_modes:
        return EgressDecision(
            allowed=False,
            reason=(
                f"Fail-open-Modus '{mode}' (ohne Verifier) braucht explizites "
                f"allowed_modes-Opt-in in Profil '{profile.name}' (§4.1/§4.3, Rev. 11)."
            ),
        )
    return EgressDecision(allowed=True, reason=f"Modus '{mode}' erlaubt.")


def parse_profiles(toml_text: str) -> dict[str, Profile]:
    """Parst das Profil-Schema aus Spec §4 (TOML). Validiert fail-closed beim Laden."""
    from sluice.modes import is_registered_mode  # lazy: vermeidet Import-Zyklus

    data = tomllib.loads(toml_text)
    profiles: dict[str, Profile] = {}
    for name, raw in data.get("profile", {}).items():
        # `mode` ist kanonisch (Rev. 9); `strategy` bleibt Alias. Fehlt beides → `strict` (§4.3).
        mode = raw.get("mode", raw.get("strategy", DEFAULT_MODE))
        if not is_registered_mode(mode):
            raise ValueError(f"Profil '{name}': unbekannter Modus '{mode}' (§3).")

        allowed_modes = tuple(raw.get("allowed_modes", ()))
        for m in allowed_modes:
            if not is_registered_mode(m):
                raise ValueError(f"Profil '{name}': allowed_modes nennt unbekannten Modus '{m}' (§3).")

        reversible: ReversibleConfig | None = None
        if mode == "pseudonymizing":
            rev = raw.get("reversible", {})
            storage = rev.get("storage", "memory")
            if storage not in VALID_STORAGE:
                raise ValueError(
                    f"Profil '{name}': storage '{storage}' nicht unterstützt (v1: memory, §8)."
                )
            reversible = ReversibleConfig(
                scope=rev.get("scope", "session"),
                ttl_seconds=int(rev.get("ttl_seconds", 3600)),
                storage=storage,
            )

        profiles[name] = Profile(
            name=name,
            mode=mode,
            egress_enabled=bool(raw.get("egress_enabled", True)),
            allowed_purposes=tuple(raw.get("allowed_purposes", ())),
            provider_allowlist=tuple(raw.get("provider_allowlist", ())),
            allowed_modes=allowed_modes,
            detector_profile=_parse_detector_profile(name, raw),
            dictionary_terms=tuple(str(t) for t in raw.get("dictionary_terms", ())),
            reversible=reversible,
            ner=_parse_ner(name, raw),
        )
    return profiles


def _parse_detector_profile(profile_name: str, raw: dict) -> str:
    """Parst `detector_profile` — ein Name **oder** eine Liste (§5.1, Rev. 13).

    Ein Profil braucht regelmäßig beides: die Muster seiner Domäne *und* die deutschen
    PII-Muster. `media` allein kennt keine IBAN, `pii_de` allein keine NAS-Pfade — wer
    sich entscheiden muss, tauscht Schutz in der eigenen Domäne gegen Schutz in einer
    fremden. Mehrere Namen werden deshalb zur **Vereinigungsmenge** zusammengelegt und
    unter einem kanonischen Namen (`media+pii_de`) geführt.

    Nach außen bleibt es *ein* Name: Verifier, Span-Erkennung und die
    Anonymisierungs-Identität arbeiten unverändert weiter, und im Audit steht die
    Zusammensetzung ablesbar statt als Liste.

    **Unbekannte Namen sind ab Rev. 13 ein Ladefehler.** Bis dahin lud ein Tippfehler
    klaglos durch und der Verifier blockte erst später mit „unbekanntes Detektor-Profil":
    fail-closed zwar, aber als Fehlerbild irreführend — im Betrieb sucht man dann einen
    Defekt statt eines Zeichendrehers. Der Dienst startet jetzt gar nicht erst.
    """
    declared = raw.get("detector_profile", "infra")
    if isinstance(declared, str):
        names: list[str] = [declared]
    elif isinstance(declared, (list, tuple)):
        if not all(isinstance(x, str) for x in declared):
            raise ValueError(
                f"Profil '{profile_name}': detector_profile-Liste darf nur Strings enthalten."
            )
        names = list(declared)
    else:
        raise ValueError(
            f"Profil '{profile_name}': detector_profile muss ein Name oder eine Liste von "
            f"Namen sein, ist {type(declared).__name__}."
        )

    try:
        return merge_detector_profiles(names)
    except UnknownDetectorProfile as exc:
        raise ValueError(f"Profil '{profile_name}': {exc}") from exc


def _parse_ner(profile_name: str, raw: dict) -> NerConfig | None:
    """Parst `[profile.<name>.ner]` (§4/§5.4, Rev. 12).

    Wird **immer** geparst, wenn der Block existiert — nicht nur bei `mode = "pii_ner"`.
    Ein Request darf den Modus wechseln (§7.2), und eine erst dann fehlende NER-Config
    wäre ein Konfigurationsfehler mitten im Egress-Pfad statt beim Laden.

    Der `threshold` ist der Punkt, an dem dieses Schema von der üblichen Kalibrierung
    abweicht: er wird auf **Recall** optimiert, nicht auf F1 (§5.4). Deshalb ist er ein
    versionierter Konfigurationswert und keine Code-Konstante — und deshalb hält
    `threshold_declared` fest, ob das Profil ihn wirklich gesetzt hat. Ein geerbter
    Default heißt: unkalibriert, und die Anonymisierungs-Identität weist das aus.
    """
    block = raw.get("ner")
    if not isinstance(block, dict):
        return None

    threshold = block.get("threshold")
    if threshold is not None and not 0.0 <= float(threshold) <= 1.0:
        raise ValueError(
            f"Profil '{profile_name}': ner.threshold muss in [0,1] liegen, ist {threshold}."
        )
    timeout = float(block.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if timeout <= 0:
        raise ValueError(
            f"Profil '{profile_name}': ner.timeout_seconds muss > 0 sein (Timeout zählt "
            f"als Ausfall, §5.3)."
        )

    # Zerlegung langer Texte (§5.3). Beides muss > 0 sein: eine Stückgröße von 0 wäre
    # keine Zerlegung, und ohne Überlappung würde jede Entität an einer Schnittstelle
    # zerschnitten und damit in beiden Stücken verfehlt — ein stilles Recall-Loch genau
    # an den Stellen, die die Zerlegung erst nötig gemacht haben.
    max_chars = int(block.get("max_chars_per_chunk", DEFAULT_MAX_CHARS_PER_CHUNK))
    overlap = int(block.get("chunk_overlap_chars", DEFAULT_CHUNK_OVERLAP_CHARS))
    if max_chars <= 0:
        raise ValueError(
            f"Profil '{profile_name}': ner.max_chars_per_chunk muss > 0 sein — ohne "
            f"Stückgröße würde das Modell lange Texte still kürzen (§5.3)."
        )
    if not 0 < overlap < max_chars:
        raise ValueError(
            f"Profil '{profile_name}': ner.chunk_overlap_chars muss zwischen 1 und "
            f"max_chars_per_chunk ({max_chars}) liegen, ist {overlap}. Ohne Überlappung "
            f"wird jede Entität an einer Schnittstelle verfehlt (§5.3)."
        )

    return NerConfig(
        url=block.get("url"),
        threshold=float(threshold) if threshold is not None else DEFAULT_THRESHOLD,
        labels=tuple(str(x) for x in block.get("labels", DEFAULT_LABELS)),
        timeout_seconds=timeout,
        model_repo=str(block.get("model_repo", "")),
        model_revision=str(block.get("model_revision", "")),
        model_precision=str(block.get("model_precision", "")),
        cache_size=int(block.get("cache_size", 1024)),
        threshold_declared=threshold is not None,
        max_chars_per_chunk=max_chars,
        chunk_overlap_chars=overlap,
    )


def load_profiles(path: str | Path) -> dict[str, Profile]:
    """Lädt Profile aus einer TOML-Datei (Spec §4)."""
    return parse_profiles(Path(path).read_text(encoding="utf-8"))
