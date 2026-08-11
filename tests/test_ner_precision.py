"""ONNX-Präzision: deklariert muss geladen entsprechen (Spec §5.4, Rev. 12).

Die Lücke, die diese Suite schließt, fiel bei der Messung am 2026-08-11 auf der
Sluice-VM auf: `SLUICE_NER_PRECISION` wird frei deklariert und geht **unverändert** in
die Anonymisierungs-Identität ein, während `SLUICE_NER_ONNX_FILE` bestimmt, was der
Dienst tatsächlich lädt. Zwischen beiden gab es keine Verbindung — beide Läufe wurden
als `precision=fp32` protokolliert, obwohl der zweite `model_quint8.onnx` fuhr.

Dass das keine Formalie ist, zeigt dieselbe Messung: dasselbe Repo lieferte über
`model.onnx` **89** Spans bei 4.000 Zeichen, über `model_quint8.onnx` **113**. Die Datei
entscheidet über das Ergebnis. Eine Identität, die `fp32` behauptet, während uint8
rechnet, ist deshalb keine ungenaue Angabe, sondern eine falsche — und damit genau das,
was die Identität verhindern soll.

Kein Modell und kein Netz: geprüft werden die reinen Funktionen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sluice.ner.engine import precision_from_onnx_file, verify_onnx_precision


# ---- Ableitung aus dem Exportnamen ---------------------------------------------------


@pytest.mark.parametrize(
    ("dateiname", "erwartet"),
    [
        ("onnx/model.onnx", "fp32"),
        ("onnx/model_fp16.onnx", "fp16"),
        ("onnx/model_float16.onnx", "fp16"),
        ("onnx/model_quint8.onnx", "uint8"),
        ("onnx/model_uint8.onnx", "uint8"),
        ("onnx/model_int8.onnx", "int8"),
        ("ONNX/MODEL_QUINT8.ONNX", "uint8"),
    ],
)
def test_praezision_aus_dem_exportnamen(dateiname: str, erwartet: str) -> None:
    assert precision_from_onnx_file(dateiname) == erwartet


def test_quint8_gewinnt_gegen_int8() -> None:
    """Reihenfolge der Marker: der kürzere darf den längeren nicht überdecken."""
    assert precision_from_onnx_file("model_quint8.onnx") == "uint8"


def test_ohne_marker_gilt_fp32() -> None:
    assert precision_from_onnx_file("onnx/irgendwas.onnx") == "fp32"


# ---- Namensprüfung gegen die Deklaration ---------------------------------------------


def test_uebereinstimmung_geht_durch() -> None:
    verify_onnx_precision("fp32", "onnx/model.onnx")
    verify_onnx_precision("uint8", "onnx/model_quint8.onnx")


def test_deklariert_fp32_geladen_uint8_blockiert() -> None:
    """Der Fall aus der VM-Messung: Identität behauptete fp32, gerechnet wurde uint8."""
    with pytest.raises(RuntimeError, match="Präzision widersprüchlich"):
        verify_onnx_precision("fp32", "onnx/model_quint8.onnx")


def test_deklariert_uint8_geladen_fp32_blockiert() -> None:
    """Auch die Gegenrichtung ist eine Falschaussage — nicht nur die 'gefährlichere'."""
    with pytest.raises(RuntimeError, match="Präzision widersprüchlich"):
        verify_onnx_precision("uint8", "onnx/model.onnx")


def test_leere_deklaration_gilt_als_fp32() -> None:
    verify_onnx_precision("", "onnx/model.onnx")
    with pytest.raises(RuntimeError):
        verify_onnx_precision("", "onnx/model_quint8.onnx")


# ---- Inhaltsprüfung: der Graph statt des Dateinamens ---------------------------------


def _fake_onnx(tmp_path: Path, *, quantisiert: bool, name: str) -> str:
    """Minimaler Stellvertreter — die Prüfung sucht Operatornamen, keinen gültigen Graph."""
    pfad = tmp_path / name
    inhalt = b"\x08\x07onnx-graph-bytes" * 100
    if quantisiert:
        inhalt += b"QuantizeLinear" + b"\x00" * 50
    pfad.write_bytes(inhalt)
    return str(pfad)


def test_falsch_benannte_datei_wird_am_graph_erkannt(tmp_path: Path) -> None:
    """Der Dateiname ist eine Behauptung, der Graph nicht.

    Eine quantisierte Datei, die `model.onnx` heißt, kommt durch die Namensprüfung —
    und würde als fp32 ins Audit gehen. Die Inhaltsprüfung fängt sie.
    """
    pfad = _fake_onnx(tmp_path, quantisiert=True, name="model.onnx")
    with pytest.raises(RuntimeError, match="Graph"):
        verify_onnx_precision("fp32", "onnx/model.onnx", local_path=pfad)


def test_als_quantisiert_benannte_fp32_datei_wird_erkannt(tmp_path: Path) -> None:
    pfad = _fake_onnx(tmp_path, quantisiert=False, name="model_quint8.onnx")
    with pytest.raises(RuntimeError, match="Graph"):
        verify_onnx_precision("uint8", "onnx/model_quint8.onnx", local_path=pfad)


def test_stimmiger_graph_geht_durch(tmp_path: Path) -> None:
    fp32 = _fake_onnx(tmp_path, quantisiert=False, name="model.onnx")
    verify_onnx_precision("fp32", "onnx/model.onnx", local_path=fp32)

    quant = _fake_onnx(tmp_path, quantisiert=True, name="model_quint8.onnx")
    verify_onnx_precision("uint8", "onnx/model_quint8.onnx", local_path=quant)


def test_operatorname_auf_blockgrenze_wird_gefunden(tmp_path: Path) -> None:
    """Stückweises Lesen darf den Marker nicht zwischen zwei Blöcken verlieren."""
    pfad = tmp_path / "model.onnx"
    block = 1 << 20
    marker = b"QuantizeLinear"
    # Marker genau über die Blockgrenze legen.
    fuell = b"x" * (block - len(marker) // 2)
    pfad.write_bytes(fuell + marker + b"y" * 1000)
    with pytest.raises(RuntimeError, match="Graph"):
        verify_onnx_precision("fp32", "onnx/model.onnx", local_path=str(pfad))


def test_unlesbare_datei_faellt_auf_die_namenspruefung_zurueck(tmp_path: Path) -> None:
    """Kein Grund zu blockieren: die Namensprüfung hat bereits zugestimmt."""
    verify_onnx_precision(
        "fp32", "onnx/model.onnx", local_path=str(tmp_path / "gibtsnicht.onnx")
    )
