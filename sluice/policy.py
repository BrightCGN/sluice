"""Egress-Policy — Profil-Schema und Profil-Gate (Spec §4).

Das Profil ist die vollständige, auditierbare Form der Egress-Erlaubnis eines
Konsumenten. Default-Deny (§4.3): kein Profil → nichts raus, kein Vererben fremder
Profile. Souveränes Profil (§4.2): `egress_enabled=false` → Guard lässt **nichts**
durch, egal welche Strategie — der Riegel greift *vor* der Strategie-Auswahl.

Herkunft: Tempers egress/policy.py, erweitert um das maschinenlesbare
TOML-Profil-Schema aus Spec §4.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

VALID_STRATEGIES = ("generalizing", "pseudonymizing")
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
    strategy: str = "generalizing"  # Default; pseudonymizing nur per explizitem Opt-in (§2)
    egress_enabled: bool = True
    allowed_purposes: tuple[str, ...] = ()
    provider_allowlist: tuple[str, ...] = ()
    detector_profile: str = "infra"
    reversible: ReversibleConfig | None = None


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str


def check_egress_allowed(profile: Profile | None, purpose: str) -> EgressDecision:
    """Profil-Gate — läuft immer zuerst (Spec §2, Invariante 1).

    Prüft, ob ein ausgehender Vorgang für dieses Profil überhaupt erlaubt ist,
    *bevor* irgendeine Strategie läuft.
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


def parse_profiles(toml_text: str) -> dict[str, Profile]:
    """Parst das Profil-Schema aus Spec §4 (TOML). Validiert fail-closed beim Laden."""
    data = tomllib.loads(toml_text)
    profiles: dict[str, Profile] = {}
    for name, raw in data.get("profile", {}).items():
        strategy = raw.get("strategy", "generalizing")
        if strategy not in VALID_STRATEGIES:
            raise ValueError(f"Profil '{name}': unbekannte Strategie '{strategy}'.")

        reversible: ReversibleConfig | None = None
        if strategy == "pseudonymizing":
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
            strategy=strategy,
            egress_enabled=bool(raw.get("egress_enabled", True)),
            allowed_purposes=tuple(raw.get("allowed_purposes", ())),
            provider_allowlist=tuple(raw.get("provider_allowlist", ())),
            detector_profile=raw.get("detector_profile", "infra"),
            reversible=reversible,
        )
    return profiles


def load_profiles(path: str | Path) -> dict[str, Profile]:
    """Lädt Profile aus einer TOML-Datei (Spec §4)."""
    return parse_profiles(Path(path).read_text(encoding="utf-8"))
