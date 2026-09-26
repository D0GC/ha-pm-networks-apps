"""Transformation, Validierung und Diff für HA-`schedule`-Helfer.

HA-Format (Speicherform, siehe homeassistant/components/schedule/__init__.py,
STORAGE_TIME_RANGE_SCHEMA)::

    {"id": "heizplan_wohnzimmer", "name": "...", "icon": "mdi:...",
     "monday": [{"from": "06:00:00", "to": "08:30:00", "data": {"temperatur": 21}}],
     ...}

UI-Format (Minuten, Sekunden optional)::

    {"monday": [{"start": 360, "end": 510, "temp": 21.0, "extra": {}}], ...}

``end`` = 1440 entspricht "24:00:00". Sekunden ungleich null (z. B. "06:00:30")
bleiben über ``start_s``/``end_s`` erhalten.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from typing import Any

DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DAY_LABELS_DE: dict[str, str] = {
    "monday": "Montag",
    "tuesday": "Dienstag",
    "wednesday": "Mittwoch",
    "thursday": "Donnerstag",
    "friday": "Freitag",
    "saturday": "Samstag",
    "sunday": "Sonntag",
}
WEEKDAYS = DAYS[:5]
WEEKEND = DAYS[5:]

TEMP_KEY = "temperatur"
TEMP_MIN = 5.0
TEMP_MAX = 25.0
TEMP_STEP = 0.5
DAY_MINUTES = 1440


class ScheduleError(ValueError):
    """Ungültiger Heizplan."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# --------------------------------------------------------------------------- Zeit


def parse_time(value: str) -> int:
    """'HH:MM[:SS]' -> Minuten seit Mitternacht (24:00 -> 1440), ohne Sekunden."""
    return parse_time_s(value)[0]


def parse_time_s(value: str) -> tuple[int, int]:
    """'HH:MM[:SS]' -> (Minuten, Sekunden)."""
    if not isinstance(value, str):
        raise ValueError(f"Ungültige Uhrzeit: {value!r}")
    parts = value.strip().split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"Ungültige Uhrzeit: {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as err:
        raise ValueError(f"Ungültige Uhrzeit: {value!r}") from err
    if hour == 24 and minute == 0 and second == 0:
        return DAY_MINUTES, 0
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f"Ungültige Uhrzeit: {value!r}")
    return hour * 60 + minute, second


def format_time(minutes: int, seconds: int = 0) -> str:
    """Minuten (+ Sekunden) -> 'HH:MM:SS' im HA-Speicherformat (1440 -> '24:00:00')."""
    if minutes == DAY_MINUTES:
        return "24:00:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}:{seconds:02d}"


def _secs(block: dict[str, Any], key: str) -> int:
    """Position eines Blockrands in Sekunden seit Mitternacht."""
    return int(block[key]) * 60 + int(block.get(f"{key}_s") or 0)


def format_hm(minutes: int) -> str:
    """Minuten -> 'HH:MM' für Anzeige."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _norm_temp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value) * 2) / 2
    except (TypeError, ValueError):
        return None


def _temp_out(value: float) -> int | float:
    """21.0 -> 21, 21.5 -> 21.5 (entspricht der bisherigen Speicherform)."""
    return int(value) if float(value).is_integer() else float(value)


# ------------------------------------------------------------------ HA <-> UI


def ha_to_ui(item: dict[str, Any]) -> dict[str, Any]:
    """HA-Speicherform eines Zeitplans in UI-Blöcke wandeln."""
    days: dict[str, list[dict[str, Any]]] = {}
    for day in DAYS:
        blocks = []
        for rng in item.get(day) or []:
            data = dict(rng.get("data") or {})
            raw_temp = data.pop(TEMP_KEY, None)
            start, start_s = parse_time_s(str(rng["from"]))
            end, end_s = parse_time_s(str(rng["to"]))
            block: dict[str, Any] = {"start": start, "end": end, "temp": _norm_temp(raw_temp), "extra": data}
            if start_s:
                block["start_s"] = start_s
            if end_s:
                block["end_s"] = end_s
            blocks.append(block)
        blocks.sort(key=lambda b: b["start"])
        days[day] = blocks
    return days


def ui_to_ha_days(days: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """UI-Blöcke (validiert) in HA-Tageslisten wandeln."""
    out: dict[str, list[dict[str, Any]]] = {}
    for day in DAYS:
        ranges = []
        for block in sorted(days.get(day) or [], key=lambda b: _secs(b, "start")):
            data: dict[str, Any] = dict(block.get("extra") or {})
            temp = _norm_temp(block.get("temp"))
            if temp is not None:
                data[TEMP_KEY] = _temp_out(temp)
            rng: dict[str, Any] = {
                "from": format_time(int(block["start"]), int(block.get("start_s") or 0)),
                "to": format_time(int(block["end"]), int(block.get("end_s") or 0)),
            }
            if data:
                rng["data"] = data
            ranges.append(rng)
        out[day] = ranges
    return out


def build_update_message(item: dict[str, Any], days: dict[str, Any]) -> dict[str, Any]:
    """Nachricht für `schedule/update` erzeugen (ohne `id`).

    `ScheduleStorageCollection._update_data` ersetzt das komplette Element
    (``{CONF_ID: item[CONF_ID]} | update_data``). Name und Icon müssen daher
    unverändert mitgeschickt werden, sonst gehen sie verloren.
    """
    msg: dict[str, Any] = {
        "type": "schedule/update",
        "schedule_id": item["id"],
        "name": item["name"],
    }
    if item.get("icon"):
        msg["icon"] = item["icon"]
    msg.update(ui_to_ha_days(days))
    return msg


# ------------------------------------------------------------------ Validierung


class EmptyScheduleError(ScheduleError):
    """Alle sieben Tage leer, ohne ausdrückliche Bestätigung."""


def validate_days(days: Any, allow_empty: bool = False) -> dict[str, list[dict[str, Any]]]:
    """UI-Tage prüfen und normalisiert zurückgeben. Wirft ScheduleError.

    Alle sieben Tage müssen enthalten sein (fehlende Tage würde HA als leer speichern).
    Ein vollständig leerer Plan ist nur mit ``allow_empty`` erlaubt.
    """
    errors: list[str] = []
    if not isinstance(days, dict):
        raise ScheduleError(["Zeitplan muss ein Objekt mit Wochentagen sein."])
    unknown = set(days) - set(DAYS)
    if unknown:
        errors.append(f"Unbekannte Tage: {', '.join(sorted(unknown))}")
    missing = [DAY_LABELS_DE[d] for d in DAYS if d not in days]
    if missing:
        errors.append(f"Es fehlen Tage: {', '.join(missing)}.")
    result: dict[str, list[dict[str, Any]]] = {}
    for day in DAYS:
        label = DAY_LABELS_DE[day]
        raw = days.get(day)
        if raw is None:
            continue
        if not isinstance(raw, list):
            errors.append(f"{label}: Blockliste erwartet.")
            continue
        blocks: list[dict[str, Any]] = []
        for idx, blk in enumerate(raw, 1):
            if not isinstance(blk, dict):
                errors.append(f"{label}, Block {idx}: ungültig.")
                continue
            try:
                start = int(blk.get("start"))
                end = int(blk.get("end"))
            except (TypeError, ValueError):
                errors.append(f"{label}, Block {idx}: Beginn/Ende fehlen.")
                continue
            try:
                start_s = int(blk.get("start_s") or 0)
                end_s = int(blk.get("end_s") or 0)
            except (TypeError, ValueError):
                start_s = end_s = -1
            if not (0 <= start_s < 60 and 0 <= end_s < 60) or (end == DAY_MINUTES and end_s):
                errors.append(f"{label}, Block {idx}: Sekundenangabe ungültig.")
                start_s = end_s = 0
            if start < 0 or end > DAY_MINUTES:
                errors.append(f"{label}, Block {idx}: außerhalb 00:00–24:00.")
            if start * 60 + start_s >= end * 60 + end_s:
                errors.append(
                    f"{label}, Block {idx}: Beginn {format_hm(max(start, 0))} liegt nicht vor "
                    f"Ende {format_hm(min(max(end, 0), DAY_MINUTES))}."
                )
            temp_raw = blk.get("temp")
            temp = None
            if temp_raw is not None:
                try:
                    tval = float(temp_raw)
                except (TypeError, ValueError):
                    errors.append(f"{label}, Block {idx}: Temperatur ungültig.")
                    tval = None
                if tval is not None:
                    if not TEMP_MIN <= tval <= TEMP_MAX:
                        errors.append(f"{label}, Block {idx}: Temperatur {tval:g} °C außerhalb {TEMP_MIN:g}–{TEMP_MAX:g} °C.")
                    elif abs(tval * 2 - round(tval * 2)) > 1e-9:
                        errors.append(f"{label}, Block {idx}: Temperatur nur in 0,5-°C-Schritten.")
                    temp = round(tval * 2) / 2
            extra = blk.get("extra") or {}
            if not isinstance(extra, dict) or any(not isinstance(v, (bool, str, int, float)) for v in extra.values()):
                errors.append(f"{label}, Block {idx}: Zusatzdaten ungültig.")
                extra = {}
            block = {"start": start, "end": end, "temp": temp, "extra": dict(extra)}
            if start_s:
                block["start_s"] = start_s
            if end_s:
                block["end_s"] = end_s
            blocks.append(block)
        blocks.sort(key=lambda b: _secs(b, "start"))
        for prev, cur in pairwise(blocks):
            if _secs(cur, "start") < _secs(prev, "end"):
                errors.append(
                    f"{label}: Blöcke {format_hm(prev['start'])}–{format_hm(prev['end'])} und "
                    f"{format_hm(cur['start'])}–{format_hm(cur['end'])} überlappen."
                )
        result[day] = blocks
    if errors:
        raise ScheduleError(errors)
    if not allow_empty and not any(result.values()):
        raise EmptyScheduleError(["Der Heizplan enthält an keinem Tag einen Block. Bitte ausdrücklich bestätigen."])
    return result


# ------------------------------------------------------------------ Revision


def revision(item: dict[str, Any]) -> str:
    """Stabile Prüfsumme über Name, Icon und alle Tage (Optimistic Locking)."""
    canon = {
        "name": item.get("name"),
        "icon": item.get("icon"),
        **{d: ui_to_ha_days(ha_to_ui(item))[d] for d in DAYS},
    }
    raw = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ Diff


def _edge(block: dict[str, Any], key: str) -> str:
    sec = int(block.get(f"{key}_s") or 0)
    return format_hm(int(block[key])) + (f":{sec:02d}" if sec else "")


def _blk_label(block: dict[str, Any]) -> str:
    temp = block.get("temp")
    temp_s = f"{temp:g} °C".replace(".", ",") if temp is not None else "ohne Temperatur"
    return f"{_edge(block, 'start')}–{_edge(block, 'end')} ({temp_s})"


def diff_days(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Unterschiede je Tag. Ergebnis: Liste von {day, label, changes:[{op, text}]}."""
    result = []
    for day in DAYS:
        o = sorted(old.get(day) or [], key=lambda b: _secs(b, "start"))
        n = sorted(new.get(day) or [], key=lambda b: _secs(b, "start"))
        key = lambda b: (_secs(b, "start"), _secs(b, "end"), b.get("temp"), json.dumps(b.get("extra") or {}, sort_keys=True))  # noqa: E731
        o_keys = [key(b) for b in o]
        n_keys = [key(b) for b in n]
        if o_keys == n_keys:
            continue
        changes: list[dict[str, str]] = []
        o_rest = [b for b in o if key(b) not in n_keys]
        n_rest = [b for b in n if key(b) not in o_keys]
        # Gleiche Zeitspanne, andere Temperatur -> "geändert"
        for nb in list(n_rest):
            match = next((ob for ob in o_rest if key(ob)[:2] == key(nb)[:2]), None)
            if match is None:
                match = next(
                    (ob for ob in o_rest if _secs(ob, "start") < _secs(nb, "end") and _secs(nb, "start") < _secs(ob, "end")),
                    None,
                )
            if match is not None:
                changes.append({"op": "change", "text": f"{_blk_label(match)} wird zu {_blk_label(nb)}"})
                o_rest.remove(match)
                n_rest.remove(nb)
        changes.extend({"op": "remove", "text": f"entfernt: {_blk_label(b)}"} for b in o_rest)
        changes.extend({"op": "add", "text": f"neu: {_blk_label(b)}"} for b in n_rest)
        result.append({"day": day, "label": DAY_LABELS_DE[day], "changes": changes})
    return result


# ------------------------------------------------------------------ Tagesaktionen


def copy_day(days: dict[str, Any], source: str, targets: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Blöcke eines Tages auf andere Tage kopieren (tiefe Kopie)."""
    if source not in DAYS:
        raise ValueError(f"Unbekannter Tag: {source}")
    out = {d: [dict(b, extra=dict(b.get("extra") or {})) for b in days.get(d) or []] for d in DAYS}
    for tgt in targets:
        if tgt not in DAYS:
            raise ValueError(f"Unbekannter Tag: {tgt}")
        if tgt == source:
            continue
        out[tgt] = [dict(b, extra=dict(b.get("extra") or {})) for b in days.get(source) or []]
    return out
