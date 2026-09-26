"""Regel-Engine des Klima-Coachs.

Die Regeln stehen als Daten in ``wissen.json`` (siehe Entwurf D1). Bedingungen
werden ausschließlich interpretiert (keine Codeausführung, kein eval).
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

WISSEN_DATEI = Path(__file__).parent / "wissen.json"

BEREICHE = ("raum", "global")
THEMEN = ("heizen", "lueften", "feuchte", "co2", "heizplan", "energie", "wartung", "sommer")
SAISONS = ("heizperiode", "sommer", "immer")
MASSNAHMEN = ("heizplan", "steuerung", "lueften", "info")
STEUER_AKTIONEN = ("overlay", "temperatur", "boost", "zurueck", "modus")
VERGLEICHE = ("<", "<=", ">", ">=", "==", "!=")
OPERATOREN = (*VERGLEICHE, "in", "vorhanden", "fehlt")
HEIZMONATE = (10, 11, 12, 1, 2, 3, 4)

ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PFAD_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z0-9_]+){0,4}$")
PLATZHALTER_RE = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+){0,4})\}")
MAX_TIEFE = 12
MAX_TEXT = 1000


class RegelFehler(ValueError):
    """Ungültige Regel oder Bedingung."""


# ------------------------------------------------------------------ Werte


def wert(ctx: dict[str, Any], pfad: str) -> Any:
    """Wert eines Punktpfads im Lage-Kontext; fehlend -> None."""
    cur: Any = ctx
    for teil in pfad.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(teil)
    if isinstance(cur, float) and not math.isfinite(cur):
        return None
    return cur


def _ist_zahl(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _gleich(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _ist_zahl(a) and _ist_zahl(b):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


def _vergleich(op: str, a: Any, b: Any) -> bool:
    if op == "==":
        return _gleich(a, b)
    if op == "!=":
        return not _gleich(a, b)
    if not (_ist_zahl(a) and _ist_zahl(b)):
        return False
    a, b = float(a), float(b)
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def bedingung_erfuellt(b: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """Bedingung (bereits validiert) gegen den Kontext auswerten."""
    if "alle" in b:
        return all(bedingung_erfuellt(x, ctx) for x in b["alle"])
    if "eine" in b:
        return any(bedingung_erfuellt(x, ctx) for x in b["eine"])
    if "nicht" in b:
        return not bedingung_erfuellt(b["nicht"], ctx)
    op = b["op"]
    v = wert(ctx, b["wert"])
    if op == "fehlt":
        return v is None
    if v is None:
        return False
    if op == "vorhanden":
        return True
    if op == "in":
        return any(_gleich(v, r) for r in b["ref"])
    if "pfad" in b:
        ref = wert(ctx, b["pfad"])
        if ref is None:
            return False
        plus = b.get("plus")
        if plus is not None:
            if not _ist_zahl(ref):
                return False
            ref = float(ref) + float(plus)
    else:
        ref = b["ref"]
    return _vergleich(op, v, ref)


# ------------------------------------------------------------------ Formatierung


def format_wert(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "ja" if v else "nein"
    if _ist_zahl(v):
        r = round(float(v), 1)
        if r.is_integer():
            return str(int(r))
        return f"{r:.1f}".replace(".", ",")
    if isinstance(v, str):
        return v
    return "–"


def formatiere(text: str, ctx: dict[str, Any]) -> str:
    return PLATZHALTER_RE.sub(lambda m: format_wert(wert(ctx, m.group(1))), text)


# ------------------------------------------------------------------ Validierung


def _pruefe_pfad(p: Any, feld: str) -> None:
    if not isinstance(p, str) or not PFAD_RE.match(p):
        raise RegelFehler(f"{feld}: ungültiger Pfad {p!r}")


def pruefe_bedingung(b: Any, tiefe: int = 0) -> None:
    if tiefe > MAX_TIEFE:
        raise RegelFehler("Bedingung zu tief verschachtelt")
    if not isinstance(b, dict):
        raise RegelFehler("Bedingung muss ein Objekt sein")
    for key in ("alle", "eine"):
        if key in b:
            if len(b) != 1 or not isinstance(b[key], list) or not b[key]:
                raise RegelFehler(f"'{key}' erwartet eine nicht leere Liste")
            for x in b[key]:
                pruefe_bedingung(x, tiefe + 1)
            return
    if "nicht" in b:
        if len(b) != 1:
            raise RegelFehler("'nicht' erwartet genau eine Bedingung")
        pruefe_bedingung(b["nicht"], tiefe + 1)
        return
    _pruefe_pfad(b.get("wert"), "wert")
    op = b.get("op")
    if op not in OPERATOREN:
        raise RegelFehler(f"unbekannter Operator {op!r}")
    erlaubt = {"wert", "op"}
    if op in ("vorhanden", "fehlt"):
        pass
    elif op == "in":
        ref = b.get("ref")
        if not isinstance(ref, list) or not ref or not all(isinstance(r, str | int | float | bool) for r in ref):
            raise RegelFehler("'in' erwartet eine Liste einfacher Werte in 'ref'")
        erlaubt.add("ref")
    elif "pfad" in b:
        _pruefe_pfad(b["pfad"], "pfad")
        erlaubt |= {"pfad", "plus"}
        if "plus" in b and not _ist_zahl(b["plus"]):
            raise RegelFehler("'plus' muss eine Zahl sein")
        if "ref" in b:
            raise RegelFehler("'ref' und 'pfad' schließen sich aus")
    else:
        ref = b.get("ref", ...)
        if ref is ... or not (isinstance(ref, str | bool) or _ist_zahl(ref)):
            raise RegelFehler(f"Operator {op} erwartet 'ref' (Zahl, Text oder Wahrheitswert) oder 'pfad'")
        if isinstance(ref, str | bool) and op not in ("==", "!="):
            raise RegelFehler(f"Operator {op} vergleicht nur Zahlen")
        erlaubt.add("ref")
    extra = set(b) - erlaubt
    if extra:
        raise RegelFehler(f"unbekannte Felder {sorted(extra)}")


def _pruefe_massnahme(m: Any) -> dict[str, Any] | None:
    if m is None:
        return None
    if not isinstance(m, dict) or m.get("typ") not in MASSNAHMEN:
        raise RegelFehler("massnahme.typ ungültig")
    out: dict[str, Any] = {"typ": m["typ"]}
    if "temperatur" in m:
        t = m["temperatur"]
        if not _ist_zahl(t) or not 5 <= t <= 30:
            raise RegelFehler("massnahme.temperatur muss zwischen 5 und 30 liegen")
        out["temperatur"] = round(float(t) * 2) / 2
    if "dauer" in m:
        d = m["dauer"]
        if d is not None and (not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 1440):
            raise RegelFehler("massnahme.dauer muss zwischen 0 und 1440 liegen")
        out["dauer"] = d
    if "aktion" in m:
        if m["aktion"] not in STEUER_AKTIONEN:
            raise RegelFehler("massnahme.aktion ungültig")
        out["aktion"] = m["aktion"]
    if "modus" in m:
        if m["modus"] not in ("auto", "heat", "off"):
            raise RegelFehler("massnahme.modus ungültig")
        out["modus"] = m["modus"]
    return out


def _text(r: dict[str, Any], feld: str, pflicht: bool = True) -> str:
    v = r.get(feld)
    if v is None and not pflicht:
        return ""
    if not isinstance(v, str) or (pflicht and not v.strip()):
        raise RegelFehler(f"{feld} fehlt")
    return v.strip()[:MAX_TEXT]


def pruefe_regel(r: Any) -> dict[str, Any]:
    """Regel validieren und normalisiert zurückgeben."""
    if not isinstance(r, dict):
        raise RegelFehler("Regel muss ein Objekt sein")
    rid = r.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise RegelFehler(f"ungültige id {rid!r}")
    if r.get("bereich") not in BEREICHE:
        raise RegelFehler("bereich ungültig")
    if r.get("thema") not in THEMEN:
        raise RegelFehler("thema ungültig")
    prio = r.get("prioritaet")
    if prio not in (1, 2, 3) or isinstance(prio, bool):
        raise RegelFehler("prioritaet muss 1, 2 oder 3 sein")
    saison = r.get("saison", "immer")
    if saison not in SAISONS:
        raise RegelFehler("saison ungültig")
    monate = r.get("monate")
    if monate is not None and (
        not isinstance(monate, list)
        or not monate
        or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in monate)
    ):
        raise RegelFehler("monate muss eine Liste von 1 bis 12 sein")
    pruefe_bedingung(r.get("wenn"))
    return {
        "id": rid,
        "bereich": r["bereich"],
        "thema": r["thema"],
        "prioritaet": prio,
        "saison": saison,
        "monate": monate,
        "wenn": r["wenn"],
        "titel": _text(r, "titel"),
        "text": _text(r, "text"),
        "begruendung": _text(r, "begruendung", pflicht=False),
        "massnahme": _pruefe_massnahme(r.get("massnahme")),
    }


# ------------------------------------------------------------------ Regelwerk


@dataclass
class Regelwerk:
    version: str = ""
    regeln: list[dict[str, Any]] = field(default_factory=list)
    fehler: list[str] = field(default_factory=list)

    @classmethod
    def laden(cls, pfad: Path | None = None) -> Regelwerk:
        pfad = pfad or WISSEN_DATEI
        try:
            raw = json.loads(pfad.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _LOGGER.warning("Wissensdatenbank %s fehlt, der Klima-Coach hat keine Regeln", pfad)
            return cls(fehler=["Wissensdatenbank fehlt"])
        except (OSError, ValueError) as err:
            _LOGGER.error("Wissensdatenbank %s nicht lesbar: %s", pfad, err)
            return cls(fehler=[f"Wissensdatenbank nicht lesbar: {err}"])
        return cls.aus_daten(raw)

    @classmethod
    def aus_daten(cls, raw: Any) -> Regelwerk:
        if not isinstance(raw, dict) or not isinstance(raw.get("regeln"), list):
            _LOGGER.error("Wissensdatenbank ohne Regelliste")
            return cls(fehler=["Wissensdatenbank ohne Regelliste"])
        werk = cls(version=str(raw.get("version") or ""))
        ids: set[str] = set()
        for i, r in enumerate(raw["regeln"]):
            try:
                regel = pruefe_regel(r)
                if regel["id"] in ids:
                    raise RegelFehler(f"id {regel['id']} doppelt")
            except RegelFehler as err:
                rid = r.get("id") if isinstance(r, dict) else None
                msg = f"Regel {rid or i}: {err}"
                _LOGGER.warning("Wissensdatenbank: %s, übersprungen", msg)
                werk.fehler.append(msg)
                continue
            ids.add(regel["id"])
            werk.regeln.append(regel)
        return werk


def saison_aktiv(ctx: dict[str, Any]) -> bool:
    """Heizperiode laut Kontext; unbekannt -> nach Monat (10–4)."""
    aktiv = wert(ctx, "heizperiode.aktiv")
    if isinstance(aktiv, bool):
        return aktiv
    return wert(ctx, "zeit.monat") in HEIZMONATE


def _regel_passt_zeit(regel: dict[str, Any], ctx: dict[str, Any], heizperiode: bool) -> bool:
    if regel["saison"] == "heizperiode" and not heizperiode:
        return False
    if regel["saison"] == "sommer" and heizperiode:
        return False
    return not (regel["monate"] and wert(ctx, "zeit.monat") not in regel["monate"])


def _tipp(regel: dict[str, Any], ctx: dict[str, Any], raum: dict[str, Any] | None) -> dict[str, Any]:
    slug = raum.get("slug") if raum else None
    massnahme = dict(regel["massnahme"]) if regel["massnahme"] else None
    return {
        "id": f"{regel['id']}:{slug or 'global'}",
        "regel": regel["id"],
        "raum": slug,
        "raum_name": raum.get("name") if raum else None,
        "thema": regel["thema"],
        "prioritaet": regel["prioritaet"],
        "titel": formatiere(regel["titel"], ctx),
        "text": formatiere(regel["text"], ctx),
        "begruendung": formatiere(regel["begruendung"], ctx),
        "massnahme": massnahme,
    }


def auswerten(
    werk: Regelwerk,
    global_ctx: dict[str, Any],
    raum_ctx: list[dict[str, Any]],
    ausgeblendet: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Alle Regeln anwenden.

    ``global_ctx`` enthält alle Pfade außer ``raum.*``; ``raum_ctx`` je Raum den
    vollständigen Kontext (inklusive ``raum``).
    """
    ausgeblendet = ausgeblendet or set()
    heiz = saison_aktiv(global_ctx)
    tipps: list[dict[str, Any]] = []
    for regel in werk.regeln:
        if not _regel_passt_zeit(regel, global_ctx, heiz):
            continue
        kontexte = [global_ctx] if regel["bereich"] == "global" else raum_ctx
        for ctx in kontexte:
            try:
                if not bedingung_erfuellt(regel["wenn"], ctx):
                    continue
            except (KeyError, TypeError, ValueError) as err:  # pragma: no cover - durch Validierung ausgeschlossen
                _LOGGER.warning("Regel %s: %s", regel["id"], err)
                continue
            tipp = _tipp(regel, ctx, ctx.get("raum") if regel["bereich"] == "raum" else None)
            if tipp["id"] not in ausgeblendet:
                tipps.append(tipp)
    tipps.sort(key=lambda t: (t["prioritaet"], t["thema"], t["raum_name"] or "", t["regel"]))
    return tipps
