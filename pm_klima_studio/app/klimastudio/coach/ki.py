"""KI-Coach über ``ai_task.generate_data``.

Die Antwort der KI wird ausschließlich als Daten behandelt: robust geparst,
gegen das erwartete Schema geprüft und bereinigt. Maßnahmen werden nie
automatisch ausgeführt.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from ..ha_client import HAError
from .engine import MASSNAHMEN

KI_TIMEOUT = 180
MAX_TIPPS = 8
MAX_TEXT = 600
MAX_TITEL = 120
MAX_ROH = 20000

STEUERZEICHEN_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
ZAUN_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)\s*```", re.DOTALL)

PROMPT = """Sie sind der Klima-Coach für ein Zuhause mit der Heizungsintegration PM Klima.
PM Klima steuert die Heizkörperthermostate raumweise nach Heizplänen (Zeitplänen), mit Fensterabschaltung,
zeitweisen Temperaturen (Overlay), Boost und einer Sperre außerhalb der Heizperiode.

Aufgabe: Geben Sie konkrete, datengestützte Tipps zum Heizen, Lüften und Energiesparen für dieses Zuhause.
Regeln:
- Schreiben Sie auf Deutsch in der Sie-Form, ruhig und sachlich, ohne Ausrufezeichen und ohne Emoji.
- Stützen Sie jeden Tipp auf Werte aus dem Lagebericht und nennen Sie diese Werte in der Begründung.
- Erfinden Sie nichts, was nicht in den Daten steht. Fehlende Werte (null) bedeuten: unbekannt.
- Die lokalen Tipps (Feld lokale_tipps) kennt der Nutzer bereits. Berücksichtigen Sie sie, wiederholen Sie sie nicht.
- Höchstens 8 Tipps, nach Wichtigkeit sortiert. Priorität 1 = dringend, 2 = empfehlenswert, 3 = Hinweis.
- raum ist das Kürzel aus dem Lagebericht (Feld raeume[].raum) oder null für das ganze Zuhause.
- massnahme ist optional: typ heizplan (Heizplan des Raums anpassen), steuerung (Raumsteuerung, optional
  temperatur in °C und dauer in Minuten), lueften oder info. Maßnahmen werden nur vorgeschlagen, nie automatisch ausgeführt.

Antworten Sie ausschließlich mit einem JSON-Objekt ohne weiteren Text, genau in dieser Form:
{"zusammenfassung": "2 bis 4 Sätze zur Gesamtlage",
 "tipps": [{"titel": "kurz", "text": "Empfehlung", "begruendung": "warum, mit Werten", "prioritaet": 1,
            "raum": "kuerzel oder null", "massnahme": {"typ": "info"}}]}

Lagebericht (JSON):
"""


def prompt(bericht: dict[str, Any]) -> str:
    return PROMPT + json.dumps(bericht, ensure_ascii=False, separators=(",", ":"))


def bereinige_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    text = STEUERZEICHEN_RE.sub("", value).replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse_antwort(data: Any) -> dict[str, Any]:
    """KI-Antwort (String oder dict) in ein dict wandeln; ValueError bei Fehler."""
    if isinstance(data, dict):
        return data
    if not isinstance(data, str) or not data.strip():
        raise ValueError("Die KI hat keine Antwort geliefert.")
    text = data.strip()
    zaun = ZAUN_RE.search(text)
    if zaun:
        text = zaun.group(1)
    start, ende = text.find("{"), text.rfind("}")
    if start < 0 or ende <= start:
        raise ValueError("Die Antwort der KI enthält kein JSON-Objekt.")
    try:
        obj = json.loads(text[start : ende + 1])
    except ValueError as err:
        raise ValueError(f"Die Antwort der KI ist kein gültiges JSON ({err.msg}).") from err
    if not isinstance(obj, dict):
        raise ValueError("Die Antwort der KI ist kein JSON-Objekt.")
    return obj


def _massnahme(m: Any) -> dict[str, Any] | None:
    if not isinstance(m, dict) or m.get("typ") not in MASSNAHMEN:
        return None
    out: dict[str, Any] = {"typ": m["typ"]}
    t = m.get("temperatur")
    if isinstance(t, int | float) and not isinstance(t, bool) and math.isfinite(t) and 5 <= t <= 30:
        out["temperatur"] = round(float(t) * 2) / 2
    d = m.get("dauer")
    if isinstance(d, float) and math.isfinite(d) and d.is_integer():
        d = int(d)
    if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 1440:
        out["dauer"] = d
    return out


def _raum(value: Any, raeume: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in raeume:
        return v
    for slug, name in raeume.items():
        if name.lower() == v:
            return slug
    return None


def bereinige(obj: dict[str, Any], raeume: dict[str, str], lauf_id: str) -> dict[str, Any]:
    """Schema prüfen und bereinigen. ``raeume``: slug -> Name. ValueError, wenn nichts Brauchbares enthalten ist."""
    zus = obj.get("zusammenfassung")
    roh_tipps = obj.get("tipps")
    if not isinstance(zus, str) and not isinstance(roh_tipps, list):
        raise ValueError("Die Antwort der KI entspricht nicht dem erwarteten Schema.")
    tipps = []
    for t in roh_tipps if isinstance(roh_tipps, list) else []:
        if len(tipps) >= MAX_TIPPS:
            break
        if not isinstance(t, dict):
            continue
        titel = bereinige_text(t.get("titel"), MAX_TITEL)
        text = bereinige_text(t.get("text"))
        if not titel and not text:
            continue
        prio = t.get("prioritaet")
        prio = prio if prio in (1, 2, 3) and not isinstance(prio, bool) else 2
        raum = _raum(t.get("raum"), raeume)
        tipps.append(
            {
                "id": f"ki_{lauf_id}_{len(tipps) + 1}:{raum or 'global'}",
                "regel": "ki",
                "raum": raum,
                "raum_name": raeume.get(raum) if raum else None,
                "thema": "ki",
                "prioritaet": int(prio),
                "titel": titel or text[:MAX_TITEL],
                "text": text,
                "begruendung": bereinige_text(t.get("begruendung")),
                "massnahme": _massnahme(t.get("massnahme")),
            }
        )
    return {"zusammenfassung": bereinige_text(zus), "tipps": tipps}


def antwort_daten(resp: Any) -> Any:
    """``service_response`` von ai_task.generate_data -> ``data``."""
    if isinstance(resp, dict):
        return resp.get("data")
    return resp


def fehlertext(err: HAError) -> str:
    """Fehler beim Aufruf der KI für Nutzer lesbar, ohne technische Präfixe."""
    if err.meldung:
        return f"Der KI-Dienst meldet: {bereinige_text(err.meldung, 400)}"
    if err.code == "timeout":
        return f"Der KI-Dienst hat nicht innerhalb von {KI_TIMEOUT} Sekunden geantwortet."
    return "Der KI-Dienst ist nicht erreichbar. Bitte prüfen Sie die Verbindung zu Home Assistant."
