"""Betriebsart ``generisch``: Standard-climate-Entitäten ohne die Integration PM Klima.

Räume ergeben sich aus den Bereichen (Areas) von Home Assistant: je Bereich ein Raum mit dem
ersten Thermostat (sortiert) als Raumthermostat, Sensoren des Bereichs nach ``device_class``.
Thermostate ohne Bereich bilden einen eigenen Raum (Kürzel aus der object_id).

Stand Phase 1 (Gerüst): Räume erkennen und Zustand lesen. Aktionen und die Wirkung der
Heizperiode antworten mit HTTP 409 ``nicht_verfuegbar``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import analytics as an
from ..coach import ki
from ..config import Room, _name_for, _pick_by_value, overrides_anwenden
from ..ha_client import HAError
from . import FAEHIGKEITEN, Adapter, nicht_verfuegbar

_LOGGER = logging.getLogger(__name__)

# ClimateEntityFeature (homeassistant/components/climate/const.py)
TARGET_TEMPERATURE = 1
TARGET_TEMPERATURE_RANGE = 2
PRESET_MODE = 16
TURN_OFF = 128
TURN_ON = 256

NICHT_VERFUEGBAR = ("unavailable", "unknown")
FENSTER_KLASSEN = ("window", "door", "opening")
SLUG_UNGUELTIG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(text: str) -> str:
    slug = SLUG_UNGUELTIG_RE.sub("_", str(text).lower()).strip("_")
    return slug[:64] or "raum"


def _bool(v: Any) -> bool | None:
    if v == "on":
        return True
    if v == "off":
        return False
    return None


def _features(attrs: dict[str, Any]) -> int | None:
    v = attrs.get("supported_features")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return int(v)


def _liste(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _registry_eintrag(e: dict[str, Any]) -> dict[str, Any]:
    """Eintrag aus ``list`` oder ``list_for_display`` (bereits umgesetzt) vereinheitlichen."""
    return {
        "entity_id": e.get("entity_id"),
        "area_id": e.get("area_id") or None,
        "device_id": e.get("device_id") or None,
        "hidden": bool(e.get("hidden") or e.get("hidden_by")),
    }


class GenerischAdapter(Adapter):
    betriebsart = "generisch"
    braucht_registries = True
    heizperiode_wirksam = False  # Phase 3: interne Heizperiode mit Plananwendung

    # ------------------------------------------------------------- Räume

    async def registries_laden(self) -> dict[str, list[dict[str, Any]]]:
        """Registrierungen lesen; fehlt eine, werden Räume ohne sie (aus der object_id) gebildet."""
        out: dict[str, list[dict[str, Any]]] = {"areas": [], "entities": [], "devices": []}
        try:
            out["areas"] = await self.client.area_registry_list()
        except HAError as err:
            _LOGGER.warning("Bereiche nicht lesbar, Räume ohne Bereichszuordnung: %s", err)
        try:
            out["entities"] = await self.client.entity_registry_list_for_display()
        except HAError as err:
            _LOGGER.debug("entity_registry/list_for_display nicht möglich (%s), nutze list", err)
            try:
                out["entities"] = await self.client.entity_registry_list()
            except HAError as err2:
                _LOGGER.warning("Entitätsregistrierung nicht lesbar: %s", err2)
        try:
            out["devices"] = await self.client.device_registry_list()
        except HAError as err:
            _LOGGER.warning("Geräteregistrierung nicht lesbar: %s", err)
        return out

    def raeume_erkennen(self, states: list[dict[str, Any]], registries: dict[str, list[dict[str, Any]]]) -> list[Room]:
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        areas = {a["area_id"]: a for a in registries.get("areas") or [] if isinstance(a, dict) and a.get("area_id")}
        devices = {d["id"]: d for d in registries.get("devices") or [] if isinstance(d, dict) and d.get("id")}
        ents = {
            e["entity_id"]: _registry_eintrag(e)
            for e in registries.get("entities") or []
            if isinstance(e, dict) and e.get("entity_id")
        }

        def bereich(eid: str) -> str | None:
            """Bereich der Entität, sonst Bereich ihres Geräts."""
            e = ents.get(eid) or {}
            if e.get("area_id"):
                return e["area_id"]
            return (devices.get(e.get("device_id") or "") or {}).get("area_id") or None

        def dc(eid: str) -> str | None:
            return (by_id[eid].get("attributes") or {}).get("device_class")

        je_bereich: dict[str, list[str]] = {}
        for eid in sorted(by_id):
            if eid.startswith(("sensor.", "binary_sensor.")):
                b = bereich(eid)
                if b:
                    je_bereich.setdefault(b, []).append(eid)

        rooms: dict[str, Room] = {}
        raum_bereich: dict[str, str | None] = {}
        # Thermostate mit Bereich zuerst: Bereichsräume behalten ihr Kürzel bei Namensgleichheit
        klima = sorted(
            (bereich(e) is None, e) for e in by_id if e.startswith("climate.") and not (ents.get(e) or {}).get("hidden")
        )
        for _, eid in klima:
            attrs = by_id[eid].get("attributes") or {}
            b = bereich(eid)
            if b:
                slug = _slug(b)
                name = str((areas.get(b) or {}).get("name") or "") or _name_for(slug, None)
            else:
                slug = _slug(eid.split(".", 1)[1])
                name = _name_for(slug, attrs.get("friendly_name"))
            if slug in rooms:
                if b is not None and raum_bereich.get(slug) == b:
                    _LOGGER.info(
                        "Bereich %s hat mehrere Thermostate, Raumthermostat ist %s (%s ignoriert; änderbar über raeume_override)",
                        b,
                        rooms[slug].climate,
                        eid,
                    )
                    continue
                n = 2
                while f"{slug}_{n}" in rooms:
                    n += 1
                slug = f"{slug}_{n}"
            rooms[slug] = Room(raum=slug, name=name, climate=eid)
            raum_bereich[slug] = b

        humidity = [e for e in by_id if e.startswith("sensor.") and dc(e) == "humidity"]
        co2 = [e for e in by_id if e.startswith("sensor.") and dc(e) == "carbon_dioxide"]
        fenster = [e for e in by_id if e.startswith("binary_sensor.") and dc(e) in FENSTER_KLASSEN]

        def auswahl(kandidaten: list[str], slug: str, b: str | None) -> list[str]:
            """Sensoren des Bereichs, sonst (Rückfall wie bisher) Sensoren mit dem Raumkürzel im Namen,
            die keinem anderen Bereich zugeordnet sind."""
            im_bereich = je_bereich.get(b or "", [])
            eigen = [e for e in kandidaten if e in im_bereich]
            if eigen:
                return eigen
            return [e for e in kandidaten if slug in e.split(".", 1)[1] and not bereich(e)]

        for slug, room in rooms.items():
            attrs = (by_id.get(room.climate or "") or {}).get("attributes") or {}
            b = raum_bereich.get(slug)
            room.feuchte = _pick_by_value(auswahl(humidity, slug, b), by_id, an.to_float(attrs.get("current_humidity")))
            cands = sorted(auswahl(co2, slug, b))
            room.co2 = cands[0] if cands else None
            cands = sorted(auswahl(fenster, slug, b))
            room.fenster = cands[0] if cands else None
            temps = sorted(e for e in je_bereich.get(b or "", []) if e.startswith("sensor.") and dc(e) == "temperature")
            room.temperatur = temps[0] if temps else None
            sched = f"{self.opts.praefix_heizplan}{slug}"
            room.schedule = sched if sched.startswith("schedule.") and sched in by_id else None

        return overrides_anwenden(rooms, self.opts)

    def raum_faehigkeiten(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cl = by_id.get(room.climate or "")
        attrs = (cl or {}).get("attributes") or {}
        modi = _liste(attrs.get("hvac_modes"))
        presets = _liste(attrs.get("preset_modes"))
        features = _features(attrs)
        if features is not None:
            ziel = bool(features & TARGET_TEMPERATURE)
            bereich = bool(features & TARGET_TEMPERATURE_RANGE) and not ziel
        else:
            ziel = "temperature" in attrs
            bereich = False
        # Nur target_temp_low/high (z. B. im Modus heat_cool): Bereichs-Thermostat
        if attrs.get("temperature") is None and (
            attrs.get("target_temp_low") is not None or attrs.get("target_temp_high") is not None
        ):
            bereich = True
        grund: str | None = None
        if not room.climate:
            grund = "Kein Thermostat zugeordnet."
        elif cl is None:
            grund = f"{room.climate} ist nicht vorhanden."
        elif cl.get("state") in (None, *NICHT_VERFUEGBAR):
            grund = f"{room.climate} ist derzeit nicht verfügbar."
        elif bereich:
            grund = "Thermostate mit Temperaturbereich werden in dieser Version nur angezeigt, nicht gesteuert."
        elif not ziel:
            grund = "Das Thermostat unterstützt keine Solltemperatur."
        return {
            "steuerbar": grund is None,
            "grund": grund,
            "modi": modi,
            "presets": presets,
            "boost": "boost" in presets,
            "aus": "off" in modi or bool((features or 0) & TURN_OFF),
            "bereich": bereich,
        }

    def raum_zustand(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cl = by_id.get(room.climate or "") or {}
        attrs = cl.get("attributes") or {}
        f = self.raum_faehigkeiten(room, by_id)
        ist = an.to_float(attrs.get("current_temperature"))
        if ist is None and room.temperatur:
            ist = an.to_float((by_id.get(room.temperatur) or {}).get("state"))
        feuchte = an.to_float(attrs.get("current_humidity"))
        if feuchte is None and room.feuchte:
            feuchte = an.to_float((by_id.get(room.feuchte) or {}).get("state"))
        fenster = _bool((by_id.get(room.fenster) or {}).get("state")) if room.fenster else None
        low, high = an.to_float(attrs.get("target_temp_low")), an.to_float(attrs.get("target_temp_high"))
        return {
            "name": room.name,
            "climate": room.climate,
            "modus": cl.get("state"),
            "preset": attrs.get("preset_mode"),
            "grund": None,
            "anzeige": None,
            "hvac_action": attrs.get("hvac_action"),
            "ist": ist,
            "soll": an.to_float(attrs.get("temperature")),
            "feuchte": feuchte,
            "zeitplan_temperatur": None,
            "naechster_wechsel": None,
            "naechster_wechsel_grund": None,
            "overlay_bis": None,
            "boost_bis": None,
            "fenster_offen": fenster,
            "min_temp": an.to_float(attrs.get("min_temp")),
            "max_temp": an.to_float(attrs.get("max_temp")),
            "schritt": an.to_float(attrs.get("target_temp_step")) or 0.5,
            "steuerbar": f["steuerbar"],
            "steuerbar_grund": f["grund"],
            "soll_bereich": {"min": low, "max": high} if f["bereich"] else None,
            "faehigkeiten": {k: f[k] for k in ("modi", "presets", "boost", "aus")},
        }

    # ------------------------------------------------------------- Steuerung

    def pruefe_steuerbar(self, room: Room) -> None:
        raise nicht_verfuegbar()

    async def aktion(self, room: Room, body: dict[str, Any]) -> dict[str, Any]:
        raise nicht_verfuegbar()

    # ------------------------------------------------------------- Integration / Fähigkeiten

    def faehigkeiten(self) -> dict[str, bool]:
        # plan_anwendung folgt mit der Steuerung (opts.plan_anwenden), Schimmel-Schätzung mit 1.3.0
        return dict.fromkeys(FAEHIGKEITEN, False)

    # ------------------------------------------------------------- Heizperiode

    async def heizperiode_anwenden(self, aktiv: bool, entitaet: str) -> None:
        raise nicht_verfuegbar()

    # ------------------------------------------------------------- Coach

    def ki_prompt(self, bericht: dict[str, Any]) -> str:
        return ki.prompt(bericht, ki.PROMPT_GENERISCH)
