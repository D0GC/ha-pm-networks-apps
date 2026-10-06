"""App-Optionen und automatische Raumerkennung."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("PMKS_DATA_DIR", "/data"))

WEEKDAY_NAMES = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]

UMLAUT_NAMES = {"kuche": "Küche", "buro": "Büro", "flur": "Flur", "bad": "Bad"}

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# Zulässige Domäne je Override-Feld (Heizpläne ausschließlich schedule.*)
OVERRIDE_DOMAINS: dict[str, tuple[str, ...]] = {
    "climate": ("climate.",),
    "schedule": ("schedule.",),
    "feuchte": ("sensor.",),
    "schimmel": ("sensor.",),
    "luftqualitaet": ("sensor.",),
    "co2": ("sensor.",),
    "lueften": ("binary_sensor.",),
    "fenster": ("binary_sensor.",),
}

# Zulässige Domäne je Entitäts-Option (Freigabe: wie in der Integration pm_heizung erlaubt)
ENTITY_OPTION_DOMAINS: dict[str, tuple[str, ...]] = {
    "heizperiode_entitaet": ("input_boolean.", "binary_sensor.", "schedule."),
    "coach_ki_entitaet": ("ai_task.",),
    "wetter_entitaet": ("weather.",),
}

# Betriebsart: pm_networks = Integration PM Klima steuert, generisch = Standard-climate-Entitäten
BETRIEBSARTEN = ("pm_networks", "generisch")
SOMMER_AKTIONEN = ("plan_pausieren_und_aus", "plan_pausieren")
ABSENK_GRENZEN = (5.0, 25.0)


@dataclass
class Options:
    raeume: list[dict[str, Any]] = field(default_factory=list)
    praefix_klima: str = "climate.pm_"
    praefix_heizplan: str = "schedule.heizplan_"
    bericht_tag: str = "sonntag"
    bericht_uhrzeit: str = "18:00"
    bericht_notify: list[str] = field(default_factory=list)
    log_level: str = "info"
    empfehlung: str = "sensor.pm_klima_empfehlung"
    aussentemperatur: str = "sensor.aussentemperatur"
    aussenfeuchte: str = "sensor.aussenluftfeuchte"
    heizperiode_entitaet: str = "input_boolean.pm_heizperiode"
    coach_ki_entitaet: str = "ai_task.claude_ai_task"
    coach_anwesenheit: bool = True
    wetter_entitaet: str = "weather.dwd_zuhause"
    betriebsart: str = "pm_networks"
    # Nur im Modus generisch wirksam (ab Phase „Steuern generisch“)
    plan_anwenden: bool = True
    sommer_aktion: str = "plan_pausieren_und_aus"
    absenktemperatur: float = 17.0
    # Zugang für PM Panel Studio (Wandpanel): leer = nur Ingress
    panel_schluessel: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Options:
        path = path or DATA_DIR / "options.json"
        raw: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _LOGGER.warning("%s fehlt, verwende Standardwerte", path)
        except (OSError, ValueError) as err:
            _LOGGER.error("%s nicht lesbar: %s", path, err)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Options:
        opts = cls()
        overrides = raw.get("raeume_override")
        if not isinstance(overrides, list):
            overrides = raw.get("raeume") if isinstance(raw.get("raeume"), list) else []
        opts.raeume = [r for r in overrides if isinstance(r, dict) and r.get("raum")]
        for key in ("praefix_klima", "praefix_heizplan", "empfehlung", "aussentemperatur", "aussenfeuchte"):
            if isinstance(raw.get(key), str) and raw[key].strip():
                setattr(opts, key, raw[key].strip())
        tag = str(raw.get("bericht_tag", opts.bericht_tag)).lower()
        opts.bericht_tag = tag if tag in WEEKDAY_NAMES else "sonntag"
        uhr = str(raw.get("bericht_uhrzeit", opts.bericht_uhrzeit))
        opts.bericht_uhrzeit = uhr if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", uhr) else "18:00"
        notify = raw.get("bericht_notify") or []
        if isinstance(notify, str):
            notify = [notify]
        opts.bericht_notify = [normalize_notify(n) for n in notify if isinstance(n, str) and n.strip()]
        for key, domains in ENTITY_OPTION_DOMAINS.items():
            val = raw.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            val = val.strip().lower() if isinstance(val, str) else val
            if isinstance(val, str) and ENTITY_RE.match(val) and val.startswith(domains):
                setattr(opts, key, val)
            else:
                _LOGGER.warning("Option %s: %r ist nicht zulässig, verwende %s", key, val, getattr(opts, key))
        anw = raw.get("coach_anwesenheit")
        if isinstance(anw, bool):
            opts.coach_anwesenheit = anw
        elif anw is not None:
            _LOGGER.warning("Option coach_anwesenheit: %r ist kein Wahrheitswert, verwende true", anw)
        for key, erlaubt in (("betriebsart", BETRIEBSARTEN), ("sommer_aktion", SOMMER_AKTIONEN)):
            val = raw.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            val = val.strip().lower() if isinstance(val, str) else val
            if val in erlaubt:
                setattr(opts, key, val)
            else:
                _LOGGER.warning("Option %s: %r ist nicht zulässig, verwende %s", key, val, getattr(opts, key))
        plan = raw.get("plan_anwenden")
        if isinstance(plan, bool):
            opts.plan_anwenden = plan
        elif plan is not None:
            _LOGGER.warning("Option plan_anwenden: %r ist kein Wahrheitswert, verwende true", plan)
        absenk = raw.get("absenktemperatur")
        if absenk is not None:
            wert = _absenk(absenk)
            if wert is None:
                _LOGGER.warning(
                    "Option absenktemperatur: %r ist nicht zulässig (%g bis %g °C in 0,5er-Schritten), verwende %s",
                    absenk,
                    *ABSENK_GRENZEN,
                    opts.absenktemperatur,
                )
            else:
                opts.absenktemperatur = wert
        schl = raw.get("panel_schluessel")
        if isinstance(schl, str) and schl.strip():
            if len(schl.strip()) >= 16:
                opts.panel_schluessel = schl.strip()
            else:
                _LOGGER.warning("Option panel_schluessel: zu kurz (mindestens 16 Zeichen), Panel-Zugang bleibt aus")
        lvl = str(raw.get("log_level", "info")).lower()
        opts.log_level = lvl if lvl in ("debug", "info", "warning", "error") else "info"
        return opts

    @property
    def bericht_weekday(self) -> int:
        return WEEKDAY_NAMES.index(self.bericht_tag)

    @property
    def bericht_hm(self) -> tuple[int, int]:
        hour, minute = self.bericht_uhrzeit.split(":")
        return int(hour), int(minute)


def _absenk(value: Any) -> float | None:
    """Absenktemperatur: Zahl im zulässigen Bereich, Vielfaches von 0,5 °C."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    if not ABSENK_GRENZEN[0] <= value <= ABSENK_GRENZEN[1] or abs(value * 2 - round(value * 2)) > 1e-9:
        return None
    return round(float(value) * 2) / 2


def normalize_notify(value: str) -> str:
    """'notify.mobile_app_x' oder 'mobile_app_x' -> 'mobile_app_x'."""
    value = value.strip()
    if value.startswith("notify."):
        value = value[len("notify.") :]
    return value


@dataclass
class Room:
    raum: str
    name: str
    climate: str | None = None
    schedule: str | None = None
    feuchte: str | None = None
    schimmel: str | None = None
    luftqualitaet: str | None = None
    lueften: str | None = None
    fenster: str | None = None
    co2: str | None = None
    # Raumtemperatursensor (nur Betriebsart generisch, Bereichszuordnung); fehlt in to_dict, wenn leer
    temperatur: str | None = None
    # Absenktemperatur des Raums (Override ``absenk``, nur Betriebsart generisch); fehlt in to_dict, wenn leer
    absenk: float | None = None
    # Weitere Thermostate im Bereich (nur Betriebsart generisch, nur angezeigt); fehlt in to_dict, wenn leer
    climate_weitere: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("temperatur", "absenk"):
            if d[key] is None:
                del d[key]
        if not d["climate_weitere"]:
            del d["climate_weitere"]
        return d

    def entities(self) -> list[str]:
        return [
            e
            for e in (
                self.climate,
                self.schedule,
                self.feuchte,
                self.schimmel,
                self.luftqualitaet,
                self.lueften,
                self.fenster,
                self.co2,
                self.temperatur,
            )
            if e
        ]


def _name_for(slug: str, friendly: str | None) -> str:
    if friendly:
        for suffix in (" Heizung", " Thermostat", " Klima"):
            if friendly.endswith(suffix):
                return friendly[: -len(suffix)].strip()
        return friendly
    return UMLAUT_NAMES.get(slug, slug.replace("_", " ").title())


def discover_rooms(states: list[dict[str, Any]], opts: Options) -> list[Room]:
    """Räume aus Entitäten ableiten und mit Overrides aus den Optionen zusammenführen."""
    by_id = {s["entity_id"]: s for s in states if "entity_id" in s}

    def exists(eid: str | None) -> bool:
        return bool(eid) and eid in by_id

    def dc(eid: str) -> str | None:
        return (by_id[eid].get("attributes") or {}).get("device_class")

    humidity_sensors = [e for e in by_id if e.startswith("sensor.") and dc(e) == "humidity"]
    co2_sensors = [e for e in by_id if e.startswith("sensor.") and dc(e) == "carbon_dioxide"]
    window_sensors = [e for e in by_id if e.startswith("binary_sensor.") and dc(e) in ("window", "door", "opening")]

    rooms: dict[str, Room] = {}
    prefix = opts.praefix_klima
    for eid, st in by_id.items():
        if not eid.startswith(prefix) or not eid.startswith("climate."):
            continue
        slug = eid[len(prefix) :]
        if not slug:
            continue
        attrs = st.get("attributes") or {}
        room = Room(raum=slug, name=_name_for(slug, attrs.get("friendly_name")), climate=eid)
        sched = f"{opts.praefix_heizplan}{slug}"
        room.schedule = sched if sched.startswith("schedule.") and exists(sched) else None
        for attr_name, pattern in (
            ("schimmel", "sensor.pm_{s}_schimmelrisiko"),
            ("luftqualitaet", "sensor.pm_{s}_luftqualitaet"),
            ("lueften", "binary_sensor.pm_{s}_lueften_empfohlen"),
            ("fenster", "binary_sensor.pm_{s}_fenster_offen"),
        ):
            cand = pattern.format(s=slug)
            if exists(cand):
                setattr(room, attr_name, cand)

        # Feuchte: Sensor mit Raumnamen, bevorzugt der Wert, den das Thermostat meldet.
        cands = [e for e in humidity_sensors if slug in e.split(".", 1)[1] and not e.startswith("sensor.pm_")]
        cur = _num(attrs.get("current_humidity"))
        if cur is None and room.luftqualitaet:
            cur = _num((by_id[room.luftqualitaet].get("attributes") or {}).get("feuchte_innen"))
        room.feuchte = _pick_by_value(cands, by_id, cur)

        # Fenster: Sammelsensor der Integration, sonst Fenster-/Türsensor mit Raumnamen.
        if not room.fenster:
            wins = [e for e in window_sensors if slug in e.split(".", 1)[1]]
            room.fenster = sorted(wins)[0] if wins else None

        # CO2: Sensor mit Raumnamen, sonst Abgleich mit dem co2-Attribut der Luftqualität.
        named = [e for e in co2_sensors if slug in e.split(".", 1)[1]]
        if named:
            room.co2 = sorted(named)[0]
        elif room.luftqualitaet:
            ref = _num((by_id[room.luftqualitaet].get("attributes") or {}).get("co2"))
            if ref is not None:
                room.co2 = _pick_by_value(co2_sensors, by_id, ref, require_match=True)
        rooms[slug] = room

    return overrides_anwenden(rooms, opts)


def overrides_anwenden(rooms: dict[str, Room], opts: Options) -> list[Room]:
    """Overrides aus den Optionen (``raeume_override``) anwenden und nach Optionen-Reihenfolge sortieren."""
    for ov in opts.raeume:
        slug = str(ov.get("raum", "")).strip().lower()
        if not slug:
            continue
        if ov.get("ausblenden"):
            rooms.pop(slug, None)
            continue
        room = rooms.get(slug) or Room(raum=slug, name=_name_for(slug, None))
        for key in ("name", "climate", "schedule", "feuchte", "schimmel", "luftqualitaet", "lueften", "fenster", "co2"):
            val = ov.get(key)
            if isinstance(val, str) and val.strip():
                val = val.strip()
                if key != "name" and (not ENTITY_RE.match(val) or not val.startswith(OVERRIDE_DOMAINS[key])):
                    _LOGGER.warning("Raum %s: %s ist für %s nicht zulässig", slug, val, key)
                    continue
                setattr(room, key, val)
        if ov.get("absenk") is not None:
            wert = _absenk(ov["absenk"])
            if wert is None:
                _LOGGER.warning(
                    "Raum %s: absenk %r ist nicht zulässig (%g bis %g °C in 0,5er-Schritten)", slug, ov["absenk"], *ABSENK_GRENZEN
                )
            else:
                room.absenk = wert
        rooms[slug] = room

    order = {r["raum"]: i for i, r in enumerate(opts.raeume)}
    return sorted(rooms.values(), key=lambda r: (order.get(r.raum, 999), r.raum))


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_by_value(cands: list[str], by_id: dict[str, Any], ref: float | None, require_match: bool = False) -> str | None:
    if not cands:
        return None
    if ref is not None:
        for eid in sorted(cands):
            val = _num(by_id[eid].get("state"))
            if val is not None and abs(val - ref) < 0.05:
                return eid
    if require_match:
        return None
    return sorted(cands)[0]
