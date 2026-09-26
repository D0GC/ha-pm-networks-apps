"""App-Optionen und automatische Raumerkennung."""

from __future__ import annotations

import json
import logging
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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

    # Overrides aus den Optionen
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
