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

    @property
    def strategy(self) -> str:
        """Rückwärtskompatibler Lese-Alias auf `mode` (Rev. 9)."""
        return self.mode


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


def check_mode_allowed(profile: Profile, mode: str) -> EgressDecision:
    """Modus-Allowlist pro Profil (Spec §4.1, Rev. 9).

    Leere `allowed_modes` = alle Modi erlaubt (Default, maximale Freiheit). Sonst muss
    der gewählte Modus explizit gelistet sein — ein Betreiber sperrt so schwache Modi
    (z. B. `passthrough`) gezielt; nicht erlaubt ⇒ fail-closed.
    """
    if profile.allowed_modes and mode not in profile.allowed_modes:
        return EgressDecision(
            allowed=False,
            reason=f"Modus '{mode}' nicht in allowed_modes von '{profile.name}' (§4.1).",
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
            detector_profile=raw.get("detector_profile", "infra"),
            dictionary_terms=tuple(str(t) for t in raw.get("dictionary_terms", ())),
            reversible=reversible,
        )
    return profiles


def load_profiles(path: str | Path) -> dict[str, Profile]:
    """Lädt Profile aus einer TOML-Datei (Spec §4)."""
    return parse_profiles(Path(path).read_text(encoding="utf-8"))
