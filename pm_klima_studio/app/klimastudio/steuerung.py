"""Raumsteuerung über die Integration PM Klima.

Die App ruft ausschließlich Dienste ``pm_heizung.*`` bzw. ``climate.*`` auf
Entitäten ``climate.pm_*`` (Präfix aus den Optionen) auf, nie ein
Thermostat-Backend direkt.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from aiohttp import web

from . import analytics as an
from .config import Options, Room

HAUPTSCHALTER = "switch.pm_heizung_aktiv"
SPERRE_SENSOR = "sensor.pm_heizung_abwesenheitsphase"
AKTIONEN = ("overlay", "temperatur", "boost", "zurueck", "modus")
MODI = ("auto", "heat", "off")
MODUS_TEXT = {"auto": "Automatik (Zeitplan)", "heat": "Hand", "off": "Aus"}
RAUM_RE = re.compile(r"^[a-z0-9_]{1,64}$")
OVERLAY_TEMP = (5.0, 30.0)
OVERLAY_DAUER = (0, 1440)
BOOST_DAUER = (5, 240)


def json_fehler(cls: type[web.HTTPException], fehler: str, code: str) -> web.HTTPException:
    """HTTP-Fehler mit JSON-Körper ``{"fehler", "code"}``."""
    return cls(text=json.dumps({"fehler": fehler, "code": code}, ensure_ascii=False), content_type="application/json")


def pruefe_raum_slug(slug: str) -> str:
    if not RAUM_RE.match(slug or ""):
        raise json_fehler(web.HTTPBadRequest, "Ungültige Raumbezeichnung.", "ungueltig")
    return slug


def _fmt_temp(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}".replace(".", ",")


def _temp_out(v: float) -> int | float:
    return int(v) if float(v).is_integer() else float(v)


def _ganzzahl(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _temperatur(body: dict[str, Any], lo: float, hi: float) -> float:
    v = body.get("temperatur")
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise json_fehler(web.HTTPBadRequest, "temperatur muss eine Zahl sein.", "ungueltig")
    if not lo <= v <= hi:
        raise json_fehler(
            web.HTTPBadRequest, f"temperatur muss zwischen {_fmt_temp(lo)} und {_fmt_temp(hi)} °C liegen.", "ungueltig"
        )
    if abs(v * 2 - round(v * 2)) > 1e-9:
        raise json_fehler(web.HTTPBadRequest, "temperatur muss ein Vielfaches von 0,5 °C sein.", "ungueltig")
    return round(float(v) * 2) / 2


def pruefe_befehl(body: dict[str, Any], attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Body validieren. Rückgabe: ``{"aktion", "domain", "service", "daten", "text"}`` (ohne entity_id)."""
    attrs = attrs or {}
    aktion = body.get("aktion")
    if aktion not in AKTIONEN:
        raise json_fehler(web.HTTPBadRequest, "aktion muss overlay, temperatur, boost, zurueck oder modus sein.", "ungueltig")
    erlaubt = {
        "overlay": {"aktion", "temperatur", "dauer"},
        "temperatur": {"aktion", "temperatur"},
        "boost": {"aktion", "dauer"},
        "zurueck": {"aktion"},
        "modus": {"aktion", "modus"},
    }[aktion]
    extra = set(body) - erlaubt
    if extra:
        raise json_fehler(web.HTTPBadRequest, f"Unbekannte Felder: {', '.join(sorted(extra))}.", "ungueltig")
    if aktion == "overlay":
        temp = _temperatur(body, *OVERLAY_TEMP)
        daten: dict[str, Any] = {"temperatur": _temp_out(temp)}
        dauer = body.get("dauer")
        if dauer is None:
            text = f"Temperatur {_fmt_temp(temp)} °C bis zum nächsten Planwechsel"
        else:
            d = _ganzzahl(dauer)
            if d is None or not OVERLAY_DAUER[0] <= d <= OVERLAY_DAUER[1]:
                raise json_fehler(
                    web.HTTPBadRequest, "dauer muss eine ganze Zahl von 0 bis 1440 Minuten oder leer sein.", "ungueltig"
                )
            daten["dauer"] = d
            text = f"Temperatur {_fmt_temp(temp)} °C " + ("dauerhaft" if d == 0 else f"für {d} Minuten")
        return {"aktion": aktion, "domain": "pm_heizung", "service": "set_overlay", "daten": daten, "text": text}
    if aktion == "temperatur":
        lo = an.to_float(attrs.get("min_temp")) or OVERLAY_TEMP[0]
        hi = an.to_float(attrs.get("max_temp")) or OVERLAY_TEMP[1]
        temp = _temperatur(body, max(lo, OVERLAY_TEMP[0]), min(hi, OVERLAY_TEMP[1]))
        return {
            "aktion": aktion,
            "domain": "climate",
            "service": "set_temperature",
            "daten": {"temperature": _temp_out(temp)},
            "text": f"Handwert {_fmt_temp(temp)} °C",
        }
    if aktion == "boost":
        d = _ganzzahl(body.get("dauer"))
        if d is None or not BOOST_DAUER[0] <= d <= BOOST_DAUER[1] or d % 5:
            raise json_fehler(web.HTTPBadRequest, "dauer muss zwischen 5 und 240 Minuten liegen (in 5er-Schritten).", "ungueltig")
        return {
            "aktion": aktion,
            "domain": "pm_heizung",
            "service": "boost",
            "daten": {"dauer": d},
            "text": f"Boost für {d} Minuten",
        }
    if aktion == "zurueck":
        return {"aktion": aktion, "domain": "pm_heizung", "service": "clear_overlay", "daten": {}, "text": "Zurück zum Plan"}
    modus = body.get("modus")
    if modus not in MODI:
        raise json_fehler(web.HTTPBadRequest, "modus muss auto, heat oder off sein.", "ungueltig")
    return {
        "aktion": aktion,
        "domain": "climate",
        "service": "set_hvac_mode",
        "daten": {"hvac_mode": modus},
        "text": f"Modus {MODUS_TEXT[modus]}",
    }


def pruefe_modus(befehl: dict[str, Any], modus: Any) -> None:
    """Overlay nur in ``auto``, Handwert nur in ``heat`` (sonst 409 code=modus)."""
    if befehl["aktion"] == "overlay" and modus != "auto":
        raise json_fehler(
            web.HTTPConflict,
            "Eine zeitweise Temperatur ist nur im Modus Automatik möglich. Stellen Sie den Raum zuerst auf Automatik.",
            "modus",
        )
    if befehl["aktion"] == "temperatur" and modus != "heat":
        raise json_fehler(
            web.HTTPConflict, "Ein Handwert ist nur im Modus Hand möglich. Stellen Sie den Raum zuerst auf Hand.", "modus"
        )


def steuerbar(room: Room, opts: Options) -> bool:
    return bool(room.climate) and room.climate.startswith("climate.") and room.climate.startswith(opts.praefix_klima)


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v in ("on", "true", "True"):
        return True
    if v in ("off", "false", "False"):
        return False
    return None


def raum_zustand(room: Room, by_id: dict[str, dict[str, Any]], opts: Options) -> dict[str, Any]:
    cl = by_id.get(room.climate or "") or {}
    attrs = cl.get("attributes") or {}
    feuchte = an.to_float(attrs.get("current_humidity"))
    if feuchte is None and room.feuchte:
        feuchte = an.to_float((by_id.get(room.feuchte) or {}).get("state"))
    fenster = _bool(attrs.get("fenster_offen"))
    if fenster is None and room.fenster and room.fenster in by_id:
        fenster = _bool(by_id[room.fenster].get("state"))
    return {
        "name": room.name,
        "climate": room.climate,
        "modus": cl.get("state"),
        "preset": attrs.get("preset_mode"),
        "grund": attrs.get("grund"),
        "anzeige": attrs.get("anzeige"),
        "hvac_action": attrs.get("hvac_action"),
        "ist": an.to_float(attrs.get("current_temperature")),
        "soll": an.to_float(attrs.get("temperature")),
        "feuchte": feuchte,
        "zeitplan_temperatur": an.to_float(attrs.get("zeitplan_temperatur")),
        "naechster_wechsel": attrs.get("naechster_wechsel"),
        "naechster_wechsel_grund": attrs.get("naechster_wechsel_grund"),
        "overlay_bis": attrs.get("overlay_bis"),
        "boost_bis": attrs.get("boost_bis"),
        "fenster_offen": fenster,
        "min_temp": an.to_float(attrs.get("min_temp")),
        "max_temp": an.to_float(attrs.get("max_temp")),
        "schritt": an.to_float(attrs.get("target_temp_step")) or 0.5,
        "steuerbar": steuerbar(room, opts) and bool(cl) and cl.get("state") not in (None, "unavailable", "unknown"),
    }


def integration_status(by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    haupt = by_id.get(HAUPTSCHALTER)
    sperre = by_id.get(SPERRE_SENSOR)
    attrs = (sperre or {}).get("attributes") or {}
    gesperrt = attrs.get("gesperrt")
    return {
        "aktiv": _bool(haupt.get("state")) if haupt else None,
        "gesperrt": gesperrt if isinstance(gesperrt, bool) else None,
        "sperre_grund": attrs.get("sperre_grund"),
        "sperre_wirkung": attrs.get("sperre_wirkung"),
        "beschreibung": attrs.get("beschreibung"),
    }
