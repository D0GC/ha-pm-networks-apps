"""Lage-Kontext für die Regel-Engine und den Lagebericht an die KI.

Pfade siehe Entwurf D1. Alle Werte dürfen None sein.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import web

from .. import analytics as an
from .. import schedule as sch
from ..config import Room
from ..ha_client import HAError

_LOGGER = logging.getLogger(__name__)

WOCHE_TTL = 900
HEIZ_MIN_TEMP = 18.0  # ab dieser Blocktemperatur gilt ein Block als Heizblock (plan.stunden_woche, plan.nacht)
WETTER_TTL = 1800
REGEN_ZUSTAENDE = {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail"}
TAG_KURZ = {
    "monday": "mo",
    "tuesday": "di",
    "wednesday": "mi",
    "thursday": "do",
    "friday": "fr",
    "saturday": "sa",
    "sunday": "so",
}

# Alle Pfade des Lage-Kontexts (Entwurf D1); dient Tests und Dokumentation.
RAUM_PFADE = (
    "raum.slug",
    "raum.name",
    "raum.nassraum",
    "raum.schlafraum",
    "raum.modus",
    "raum.preset",
    "raum.grund",
    "raum.hvac_action",
    "raum.ist",
    "raum.soll",
    "raum.zeitplan_temperatur",
    "raum.feuchte",
    "raum.schimmel",
    "raum.co2",
    "raum.luftqualitaet",
    "raum.fenster_offen",
    "raum.lueften_empfohlen",
    "raum.overlay_aktiv",
    "raum.overlay_dauerhaft",
    "raum.boost_aktiv",
    "raum.soll_ist_abstand",
    "raum.woche.heizstunden",
    "raum.woche.feuchte_mittel",
    "raum.woche.feuchte_max",
    "raum.woche.feuchte_stunden_70",
    "raum.woche.feuchte_stunden_80",
    "raum.woche.schimmel_max",
    "raum.woche.schimmel_stunden_70",
    "raum.woche.schimmel_stunden_80",
    "raum.woche.co2_max",
    "raum.woche.co2_stunden_1000",
    "raum.woche.co2_stunden_1400",
    "raum.woche.fenster_anzahl",
    "raum.woche.fenster_offen_stunden",
    "raum.woche.fenster_laengste_min",
    "raum.woche.lueftung_bewertung",
    "raum.woche.lueftung_anzahl",
    "raum.plan.max_temp",
    "raum.plan.min_temp",
    "raum.plan.stunden_woche",
    "raum.plan.nacht",
    "raum.plan.leer",
    "raum.plan.tage_ohne_block",
)
GLOBAL_PFADE = (
    "aussen.temperatur",
    "aussen.feuchte",
    "aussen.tagesmittel_gestern",
    "wetter.min_morgen",
    "wetter.max_morgen",
    "wetter.min_3tage",
    "wetter.regen_morgen",
    "zeit.monat",
    "zeit.stunde",
    "zeit.wochentag",
    "heizperiode.aktiv",
    "heizperiode.modus",
    "integration.aktiv",
    "integration.gesperrt",
    "anwesenheit.jemand_zuhause",
    "anwesenheit.anzahl_zuhause",
)


def _bool_state(v: Any) -> bool | None:
    if v == "on":
        return True
    if v == "off":
        return False
    return None


def _r1(v: Any) -> float | None:
    f = an.to_float(v)
    return round(f, 1) if f is not None else None


# ------------------------------------------------------------------ Anwesenheit


def anwesenheit(by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """person.* und input_boolean.*_ist_zuhause zusammenführen.

    Personen mit unbekanntem Zustand (unknown/unavailable) ohne zugeordneten
    ``input_boolean.*_ist_zuhause`` (z. B. ``person.dashboard``) werden nicht berücksichtigt.
    """
    personen: dict[str, dict[str, Any]] = {}
    zugeordnet: set[str] = set()
    for eid, st in sorted(by_id.items()):
        if not eid.startswith("person."):
            continue
        key = eid.split(".", 1)[1]
        state = st.get("state")
        zuhause = True if state == "home" else (None if state in an.UNAVAILABLE else False)
        name = (st.get("attributes") or {}).get("friendly_name") or key.replace("_", " ").title()
        personen[key] = {"name": str(name)[:60], "zuhause": zuhause}
    for eid, st in sorted(by_id.items()):
        if not (eid.startswith("input_boolean.") and eid.endswith("_ist_zuhause")):
            continue
        key = eid.split(".", 1)[1][: -len("_ist_zuhause")]
        val = _bool_state(st.get("state"))
        match = next((k for k in personen if k == key or k.startswith(key + "_")), None)
        if match is None:
            personen[key] = {"name": key.replace("_", " ").title(), "zuhause": val}
            zugeordnet.add(key)
        else:
            zugeordnet.add(match)
            if val is True or personen[match]["zuhause"] is None:
                personen[match]["zuhause"] = val if val is not None else personen[match]["zuhause"]
    personen = {k: p for k, p in personen.items() if k in zugeordnet or p["zuhause"] is not None}
    bekannt = [p for p in personen.values() if p["zuhause"] is not None]
    zusammen = {
        "jemand_zuhause": any(p["zuhause"] for p in bekannt) if bekannt else None,
        "anzahl_zuhause": sum(1 for p in bekannt if p["zuhause"]) if bekannt else None,
    }
    return zusammen, list(personen.values())


# ------------------------------------------------------------------ Wetter


def wetter_auswerten(forecast: list[dict[str, Any]], heute: date, tz: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tage: dict[date, dict[str, Any]] = {}
    for f in forecast or []:
        if not isinstance(f, dict):
            continue
        try:
            dt = datetime.fromisoformat(str(f.get("datetime")).replace("Z", "+00:00"))
        except ValueError:
            continue
        d = dt.astimezone(tz).date() if dt.tzinfo else dt.date()
        tage.setdefault(d, f)
    morgen = tage.get(heute + timedelta(days=1)) or {}
    naechste = [tage[d] for d in (heute + timedelta(days=i) for i in (1, 2, 3)) if d in tage]
    mins = [v for v in (an.to_float(f.get("templow", f.get("temperature"))) for f in naechste) if v is not None]
    regen = None
    if morgen:
        mm = an.to_float(morgen.get("precipitation"))
        prob = an.to_float(morgen.get("precipitation_probability"))
        regen = bool(
            (mm is not None and mm >= 0.5) or (prob is not None and prob >= 60) or morgen.get("condition") in REGEN_ZUSTAENDE
        )
    werte = {
        "min_morgen": _r1(morgen.get("templow")),
        "max_morgen": _r1(morgen.get("temperature")),
        "min_3tage": round(min(mins), 1) if mins else None,
        "regen_morgen": regen,
    }
    liste = []
    for d in sorted(tage)[:6]:
        if d < heute or len(liste) >= 3:
            continue
        f = tage[d]
        liste.append(
            {
                "datum": d.isoformat(),
                "min": _r1(f.get("templow")),
                "max": _r1(f.get("temperature")),
                "niederschlag_mm": _r1(f.get("precipitation")),
                "niederschlag_wahrscheinlichkeit": _r1(f.get("precipitation_probability")),
                "zustand": str(f.get("condition"))[:30] if f.get("condition") else None,
            }
        )
    return werte, liste


# ------------------------------------------------------------------ Heizplan


def plan_kennzahlen(tage: dict[str, list[dict[str, Any]]] | None) -> dict[str, Any]:
    if tage is None:
        return {"max_temp": None, "min_temp": None, "stunden_woche": None, "nacht": None, "leer": None, "tage_ohne_block": None}
    bloecke = [b for day in sch.DAYS for b in tage.get(day) or []]
    temps = [b["temp"] for b in bloecke if b.get("temp") is not None]
    # Heizstunden und Nachtheizen zählen nur Blöcke ab HEIZ_MIN_TEMP; abgesenkte Blöcke
    # (z. B. 17 °C in der Nacht) sind gewollt. Blöcke ohne Temperatur zählen als Heizblock.
    heizend = [b for b in bloecke if b.get("temp") is None or b["temp"] >= HEIZ_MIN_TEMP]
    return {
        "max_temp": max(temps) if temps else None,
        "min_temp": min(temps) if temps else None,
        "stunden_woche": round(sum(b["end"] - b["start"] for b in heizend) / 60, 1),
        "nacht": any(b["start"] <= 60 and b["end"] >= 240 for b in heizend),
        "leer": not bloecke,
        "tage_ohne_block": sum(1 for day in sch.DAYS if not tage.get(day)),
    }


def plan_kompakt(tage: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Gleiche Tage zusammenfassen: {"mo,di,mi,do,fr": ["06:00-08:30 21 °C", ...]}."""
    gruppen: dict[tuple[str, ...], list[str]] = {}
    for day in sch.DAYS:
        eintraege = tuple(
            f"{sch.format_hm(b['start'])}-{sch.format_hm(b['end'])}"
            + (f" {sch._temp_out(b['temp'])} °C" if b.get("temp") is not None else "")
            for b in tage.get(day) or []
        )
        gruppen.setdefault(eintraege, []).append(TAG_KURZ[day])
    return {",".join(days): list(eintraege) or ["kein Block"] for eintraege, days in gruppen.items()}


# ------------------------------------------------------------------ Wochenwerte


def woche_aus_analyse(a: dict[str, Any] | None) -> dict[str, Any]:
    a = a or {}
    feuchte = a.get("feuchte") or {}
    schimmel = a.get("schimmel") or {}
    co2 = a.get("co2") or {}
    fenster = a.get("fenster") or {}
    lueftung = a.get("lueftung") or {}
    return {
        "heizstunden": a.get("heizstunden"),
        "feuchte_mittel": feuchte.get("mittel"),
        "feuchte_max": _r1(feuchte.get("max")),
        "feuchte_stunden_70": feuchte.get("stunden_70"),
        "feuchte_stunden_80": feuchte.get("stunden_80"),
        "schimmel_max": _r1(schimmel.get("max")),
        "schimmel_stunden_70": schimmel.get("stunden_70"),
        "schimmel_stunden_80": schimmel.get("stunden_80"),
        "co2_max": _r1(co2.get("max")),
        "co2_stunden_1000": co2.get("stunden_1000"),
        "co2_stunden_1400": co2.get("stunden_1400"),
        "fenster_anzahl": fenster.get("anzahl"),
        "fenster_offen_stunden": fenster.get("offen_stunden"),
        "fenster_laengste_min": (fenster.get("laengste") or {}).get("dauer_min"),
        "lueftung_bewertung": lueftung.get("bewertung"),
        "lueftung_anzahl": lueftung.get("anzahl"),
    }


def raum_kontext(
    room: Room,
    by_id: dict[str, dict[str, Any]],
    adapter: Any,
    woche: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """``adapter``: Adapter der Betriebsart (liefert den Raumzustand wie GET /api/steuerung)."""
    z = adapter.raum_zustand(room, by_id)
    cl = by_id.get(room.climate or "")
    overlay = z["overlay_bis"]
    boost = z["boost_bis"]
    lq = (by_id.get(room.luftqualitaet or "") or {}).get("state")
    ist, soll = z["ist"], z["soll"]
    slug = room.raum
    return {
        "slug": slug,
        "name": room.name,
        "nassraum": "bad" in slug or "dusch" in slug,
        "schlafraum": "schlaf" in slug,
        "modus": z["modus"] if z["modus"] in ("auto", "heat", "off") else None,
        "preset": z["preset"],
        "grund": z["grund"],
        "hvac_action": z["hvac_action"],
        "ist": ist,
        "soll": soll,
        "zeitplan_temperatur": z["zeitplan_temperatur"],
        "feuchte": z["feuchte"],
        "schimmel": an.to_float((by_id.get(room.schimmel or "") or {}).get("state")),
        "co2": an.to_float((by_id.get(room.co2 or "") or {}).get("state")),
        "luftqualitaet": lq if lq in ("gut", "mittel", "schlecht") else None,
        "fenster_offen": z["fenster_offen"],
        "lueften_empfohlen": _bool_state((by_id.get(room.lueften or "") or {}).get("state")),
        "overlay_aktiv": bool(overlay) if cl else None,
        "overlay_dauerhaft": overlay == "dauerhaft" if cl else None,
        "boost_aktiv": bool(boost) if cl else None,
        "soll_ist_abstand": round(soll - ist, 1) if soll is not None and ist is not None else None,
        "woche": woche,
        "plan": plan,
    }


# ------------------------------------------------------------------ Dienst


class LageDienst:
    """Baut den Lage-Kontext aus HA-Daten (``ks`` = KlimaStudio)."""

    def __init__(self, ks: Any) -> None:
        self.ks = ks
        self._woche: dict[str, tuple[float, dict[str, Any]]] = {}
        self._wetter: tuple[float, str, list[dict[str, Any]]] | None = None

    async def _woche_fuer(self, room: Room) -> dict[str, Any]:
        cached = self._woche.get(room.raum)
        if cached and time.monotonic() - cached[0] < WOCHE_TTL:
            return cached[1]
        try:
            a = await self.ks.data.room_analysis(room, "7d", detail=False)
        except HAError as err:
            _LOGGER.info("Wochenwerte %s nicht verfügbar: %s", room.raum, err)
            a = None
        woche = woche_aus_analyse(a)
        self._woche[room.raum] = (time.monotonic(), woche)
        return woche

    async def _forecast(self, by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        eid = self.ks.opts.wetter_entitaet
        if eid not in by_id:
            return []
        if self._wetter and self._wetter[1] == eid and time.monotonic() - self._wetter[0] < WETTER_TTL:
            return self._wetter[2]
        forecast: list[dict[str, Any]] = []
        try:
            resp = await self.ks.client.call_service(
                "weather", "get_forecasts", {"entity_id": eid, "type": "daily"}, return_response=True
            )
            raw = ((resp or {}).get(eid) or {}).get("forecast") if isinstance(resp, dict) else None
            forecast = [f for f in raw or [] if isinstance(f, dict)][:10]
        except HAError as err:
            _LOGGER.info("Wettervorhersage %s nicht verfügbar: %s", eid, err)
            return []
        self._wetter = (time.monotonic(), eid, forecast)
        return forecast

    async def _plaene(self, rooms: list[Room]) -> dict[str, dict[str, Any]]:
        if not any(r.schedule for r in rooms):
            return {}
        try:
            items = {i.get("id"): i for i in await self.ks.client.schedule_list()}
        except HAError as err:
            _LOGGER.info("Heizpläne nicht lesbar: %s", err)
            return {}
        out: dict[str, dict[str, Any]] = {}
        for room in rooms:
            if not room.schedule:
                continue
            try:
                sid = await self.ks._schedule_storage_id(room.schedule)
            except (HAError, web.HTTPException):
                sid = room.schedule.split(".", 1)[1]
            item = items.get(sid)
            if item is not None:
                out[room.raum] = sch.ha_to_ui(item)
        return out

    async def erstellen(self) -> dict[str, Any]:
        """Rückgabe: ``{"global": ctx, "raeume": [ctx], "extra": {...}}``."""
        ks = self.ks
        states = await ks.client.get_states()
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        rooms = await ks.rooms()
        hp = await ks.heizperiode.status(states)
        tz = ks.tz
        now = datetime.now(tz)
        forecast = await self._forecast(by_id)
        wetter, wetter_tage = wetter_auswerten(forecast, now.date(), tz)
        plaene = await self._plaene(rooms)
        integ = ks.adapter.lage_integration(by_id)
        anw, personen = anwesenheit(by_id)
        mittel = [t["mittel"] for t in hp["tagesmittel"]]
        hp_aktiv = hp["aktiv"] if hp["aktiv"] is not None else hp["entscheidung"]["aktiv"]
        global_ctx: dict[str, Any] = {
            "raum": None,
            "aussen": {
                "temperatur": _r1((by_id.get(ks.opts.aussentemperatur) or {}).get("state")),
                "feuchte": _r1((by_id.get(ks.opts.aussenfeuchte) or {}).get("state")),
                "tagesmittel_gestern": mittel[-1] if mittel else None,
            },
            "wetter": wetter,
            "zeit": {"monat": now.month, "stunde": now.hour, "wochentag": now.weekday()},
            "heizperiode": {"aktiv": hp_aktiv, "modus": hp["modus"]},
            "integration": {"aktiv": integ.get("aktiv"), "gesperrt": integ.get("gesperrt")},
            "anwesenheit": anw,
        }
        raeume = []
        for room in rooms:
            woche = await self._woche_fuer(room)
            plan = plan_kennzahlen(plaene.get(room.raum))
            raeume.append({**global_ctx, "raum": raum_kontext(room, by_id, ks.adapter, woche, plan)})
        return {
            "global": global_ctx,
            "raeume": raeume,
            "extra": {
                "stand": now.isoformat(timespec="minutes"),
                "heizperiode": hp,
                "integration": integ,
                "wetter_tage": wetter_tage,
                "plaene": {slug: plan_kompakt(t) for slug, t in plaene.items()},
                "personen": personen,
                "empfehlungen": ks.adapter.empfehlungen(by_id),
            },
        }


def lagebericht(lage: dict[str, Any], tipps: list[dict[str, Any]], mit_anwesenheit: bool) -> dict[str, Any]:
    """Kompakter Lagebericht für die KI (ohne Zeitreihen, Tokens, IPs oder Geräte-IDs)."""
    g = lage["global"]
    extra = lage["extra"]
    hp = extra["heizperiode"]
    raeume = []
    for ctx in lage["raeume"]:
        r = dict(ctx["raum"])
        eintrag: dict[str, Any] = {"raum": r.pop("slug"), "name": r.pop("name")}
        eintrag["aktuell"] = {k: v for k, v in r.items() if k not in ("woche", "plan")}
        eintrag["woche"] = r["woche"]
        eintrag["plan"] = r["plan"]
        eintrag["heizplan"] = extra["plaene"].get(eintrag["raum"])
        raeume.append(eintrag)
    bericht: dict[str, Any] = {
        "stand": extra["stand"],
        "zeit": g["zeit"],
        "aussen": g["aussen"],
        "wetter": {**g["wetter"], "tage": extra["wetter_tage"]},
        "heizperiode": {
            "aktiv": g["heizperiode"]["aktiv"],
            "modus": hp.get("modus"),
            "heizgrenze": hp.get("heizgrenze"),
            "hysterese": hp.get("hysterese"),
            "entscheidung": (hp.get("entscheidung") or {}).get("grund"),
            "tagesmittel": hp.get("tagesmittel"),
        },
        "integration": {k: extra["integration"].get(k) for k in ("aktiv", "gesperrt", "sperre_grund", "sperre_wirkung")},
        "raeume": raeume,
        "lokale_tipps": [{"titel": t["titel"], "raum": t["raum"], "prioritaet": t["prioritaet"]} for t in tipps],
        "empfehlungen_integration": [
            str(e.get("text"))[:200] for e in extra["empfehlungen"] if isinstance(e, dict) and e.get("text")
        ][:10],
    }
    if mit_anwesenheit:
        bericht["anwesenheit"] = g["anwesenheit"]
        bericht["personen"] = [
            {"name": p["name"], "zuhause": "ja" if p["zuhause"] else ("nein" if p["zuhause"] is False else "unbekannt")}
            for p in extra["personen"]
        ]
    return bericht
