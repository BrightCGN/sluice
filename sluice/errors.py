"""Fehler-Basis ohne Abhängigkeiten (Spec §3/§5.3, Rev. 12).

Ein bewusst leeres Blatt-Modul: `sluice.modes` und `sluice.ner` brauchen beide denselben
Fehlertyp, importieren aber gegenseitig (Modus baut auf Client, Profil trägt NER-Config).
Der gemeinsame Typ liegt deshalb hier, wo nichts hineinzeigt.
"""

from __future__ import annotations


class SluiceError(Exception):
    """Basis aller Sluice-Fehler."""


class ModeUnavailableError(SluiceError):
    """Ein Modus kann seine Zusage gerade nicht einlösen — z. B. weil ein Dienst fehlt.

    Der Guard behandelt das **fail-closed** (§5.3): die Anfrage wird blockiert und der
    Fehler als *eigener Typ* geführt, nicht als Policy-Ablehnung. Der Unterschied ist
    betrieblich wichtig — „dein Profil erlaubt das nicht" und „der Erkennungsdienst ist
    weg" verlangen völlig verschiedene Reaktionen, und ein Ausfall darf nicht als
    normale Ablehnung im Rauschen untergehen.

    Was hier ausdrücklich **nicht** passiert: ein Rückfall auf eine schwächere Stufe.
    Ein Chokepoint, der bei Ausfall durchlässiger wird, ist kein Chokepoint.
    """


__all__ = ["ModeUnavailableError", "SluiceError"]
