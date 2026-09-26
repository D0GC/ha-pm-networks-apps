"""Datenbeschaffung (Historie/Statistik) mit begrenztem Cache und Raumauswertung."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from . import analytics as an
from .config import Room
from .ha_client import HAClient, HAError

_LOGGER = logging.getLogger(__name__)

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
HISTORY_LIMIT = timedelta(days=10)  # Standard-Aufbewahrung des Recorders
CHUNK = timedelta(hours=24)
MAX_POINTS = 300


class HistoryCache:
    """LRU-Cache für kompakte, tagesweise Serien.

    Abgeschlossene Tage werden bis zu ``max_entries`` gehalten, der laufende
    Tag nur ``ttl`` Sekunden.
    """

    def __init__(self, max_entries: int = 800, ttl: float = 300) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._data: OrderedDict[tuple, tuple[float, bool, Any]] = OrderedDict()

    def get(self, key: tuple) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        stored, final, value = item
        if not final and time.monotonic() - stored > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: tuple, value: Any, final: bool) -> None:
        self._data[key] = (time.monotonic(), final, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def _floor_chunk(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0) - timedelta(hours=ts.hour)


class DataService:
    """Liest Historien aus HA und berechnet die Auswertungen je Raum."""

    def __init__(self, client: HAClient, cache: HistoryCache | None = None) -> None:
        self.client = client
        self.cache = cache or HistoryCache()
        self._sem = asyncio.Semaphore(2)

    # ------------------------------------------------------------- Rohdaten

    async def _chunk(self, kind: str, entity_id: str, c_start: datetime, c_end: datetime, now: datetime) -> dict[str, an.Series]:
        key = (kind, entity_id, c_start.timestamp(), c_end.timestamp())
        fetch_end = min(c_end, now)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        async with self._sem:
            if kind == "climate":
                raw = await self.client.history_during_period(
                    [entity_id],
                    c_start,
                    fetch_end,
                    minimal_response=False,
                    no_attributes=False,
                    significant_changes_only=False,
                )
                recs = an.parse_history_rows(raw.get(entity_id, []))
                value = {
                    "hvac_action": an.attr_series(recs, "hvac_action", numeric=False),
                    "soll": an.attr_series(recs, "temperature"),
                    "ist": an.attr_series(recs, "current_temperature"),
                    "state": an.state_series(recs),
                }
            else:
                raw = await self.client.history_during_period(
                    [entity_id],
                    c_start,
                    fetch_end,
                    minimal_response=True,
                    no_attributes=True,
                )
                recs = an.parse_history_rows(raw.get(entity_id, []))
                value = {"state": an.state_series(recs) if kind == "binary" else an.numeric_series(recs)}
            del raw, recs
        self.cache.put(key, value, final=c_end <= now - timedelta(minutes=5))
        return value

    async def history(self, kind: str, entity_id: str, start: datetime, end: datetime) -> dict[str, an.Series]:
        """Historie tageweise holen (begrenzt Nachrichten- und Speichergröße)."""
        now = datetime.now(UTC)
        parts: dict[str, list[an.Series]] = {}
        c_start = _floor_chunk(start)
        while c_start < end:
            c_end = c_start + CHUNK
            chunk = await self._chunk(kind, entity_id, c_start, c_end, now)
            for key, series in chunk.items():
                parts.setdefault(key, []).append(series)
            c_start = c_end
        return {k: an.merge_series(v) for k, v in parts.items()}

    async def stats(self, entity_id: str, start: datetime, end: datetime) -> dict[str, an.Series]:
        key = ("stats", entity_id, _floor_chunk(start).timestamp(), round(end.timestamp() / 3600))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        async with self._sem:
            raw = await self.client.statistics_during_period([entity_id], start, end, "hour", ["mean", "max"])
        rows = raw.get(entity_id, [])
        value = {"mean": an.stats_to_series(rows, "mean"), "max": an.stats_to_series(rows, "max")}
        self.cache.put(key, value, final=False)
        return value

    async def numeric(self, entity_id: str, start: datetime, end: datetime) -> tuple[an.Series, an.Series | None, str]:
        """Numerische Serie; für lange Zeiträume aus der Langzeitstatistik.

        Rückgabe: (mittelwert_serie, maximum_serie|None, quelle)
        """
        if end - start > HISTORY_LIMIT:
            try:
                st = await self.stats(entity_id, start, end)
                if st["mean"]:
                    return st["mean"], st["max"], "statistik"
            except HAError as err:
                _LOGGER.info("Statistik für %s nicht verfügbar: %s", entity_id, err)
            start = end - HISTORY_LIMIT
        hist = await self.history("sensor", entity_id, start, end)
        return hist["state"], None, "historie"

    # ------------------------------------------------------------- Auswertung

    async def room_analysis(self, room: Room, range_key: str, end: datetime | None = None, detail: bool = True) -> dict[str, Any]:
        end = end or datetime.now(UTC)
        start = end - RANGES[range_key]
        s_ts, e_ts = start.timestamp(), end.timestamp()
        hist_start = max(start, end - HISTORY_LIMIT)
        out: dict[str, Any] = {
            "raum": room.raum,
            "name": room.name,
            "zeitraum": {"start": int(s_ts * 1000), "ende": int(e_ts * 1000), "bereich": range_key},
            "hinweise": [],
        }
        if hist_start > start:
            out["hinweise"].append(
                "Heizstunden, Soll/Ist und Fensterzeiten reichen nur so weit wie die Recorder-Historie "
                f"(ab {hist_start.astimezone().strftime('%d.%m.')})."
            )

        async def safe(coro, label):
            try:
                return await coro
            except HAError as err:
                out["hinweise"].append(f"{label}: {err}")
                return None

        # Heizung
        if room.climate:
            clim = await safe(self.history("climate", room.climate, hist_start, end), "Heizung")
            if clim:
                secs = an.heating_seconds(clim["hvac_action"], hist_start.timestamp(), e_ts)
                out["heizstunden"] = round(secs / 3600, 2)
                if detail:
                    out["soll_ist"] = {
                        "soll": an.downsample(clim["soll"], hist_start.timestamp(), e_ts, MAX_POINTS),
                        "ist": an.downsample(clim["ist"], hist_start.timestamp(), e_ts, MAX_POINTS),
                        "heizt": [
                            [int(s * 1000), int(e * 1000)]
                            for s, e in an.intervals_where(
                                clim["hvac_action"], lambda v: v == "heating", hist_start.timestamp(), e_ts
                            )
                        ][-400:],
                    }

        # Fenster
        openings: list[tuple[float, float]] = []
        if room.fenster:
            win = await safe(self.history("binary", room.fenster, hist_start, end), "Fenster")
            if win:
                openings = an.window_open_intervals(win["state"], hist_start.timestamp(), e_ts)
                longest = max(openings, key=lambda iv: iv[1] - iv[0], default=None)
                out["fenster"] = {
                    "anzahl": len(openings),
                    "offen_stunden": round(sum(e - s for s, e in openings) / 3600, 2),
                    "laengste": (
                        {
                            "start": int(longest[0] * 1000),
                            "ende": int(longest[1] * 1000),
                            "dauer_min": round((longest[1] - longest[0]) / 60),
                        }
                        if longest
                        else None
                    ),
                    "intervalle": [[int(s * 1000), int(e * 1000)] for s, e in openings][-200:],
                }

        # Feuchte
        hum = None
        if room.feuchte:
            res = await safe(self.numeric(room.feuchte, start, end), "Feuchte")
            if res:
                hum, _, src = res
                vals = [v for _, v in hum if v is not None]
                out["feuchte"] = {
                    "quelle": src,
                    "mittel": round(sum(vals) / len(vals), 1) if vals else None,
                    "max": max(vals) if vals else None,
                    "stunden_70": round(an.hours_above(hum, 70, s_ts, e_ts), 2),
                    "stunden_80": round(an.hours_above(hum, 80, s_ts, e_ts), 2),
                }
                if detail:
                    out["feuchte"]["verlauf"] = an.downsample(hum, s_ts, e_ts, MAX_POINTS)

        # Schimmelrisiko
        if room.schimmel:
            res = await safe(self.numeric(room.schimmel, start, end), "Schimmelrisiko")
            if res:
                sch, sch_max, src = res
                vals = [v for _, v in (sch_max or sch) if v is not None]
                out["schimmel"] = {
                    "quelle": src,
                    "max": max(vals) if vals else None,
                    "stunden_70": round(an.hours_above(sch, 70, s_ts, e_ts), 2),
                    "stunden_80": round(an.hours_above(sch, 80, s_ts, e_ts), 2),
                }
                if detail:
                    out["schimmel"]["verlauf"] = an.downsample(sch, s_ts, e_ts, MAX_POINTS)

        # CO2
        co2 = None
        if room.co2:
            res = await safe(self.numeric(room.co2, start, end), "CO2")
            if res:
                co2, co2_max, src = res
                peaks = an.co2_peaks(co2, s_ts, e_ts, co2_max)
                peaks["episoden"] = [
                    {"start": int(e["start"] * 1000), "ende": int(e["end"] * 1000), "dauer_min": e["dauer_min"], "max": e["max"]}
                    for e in peaks["episoden"]
                ]
                out["co2"] = {"quelle": src, **peaks}
                if detail:
                    out["co2"]["verlauf"] = an.downsample(co2, s_ts, e_ts, MAX_POINTS)

        # Lüftungserfolg (braucht hochaufgelöste Historie)
        if openings and (room.feuchte or room.co2):
            hum_h = hum if out.get("feuchte", {}).get("quelle") == "historie" else None
            co2_h = co2 if out.get("co2", {}).get("quelle") == "historie" else None
            if hum_h is None and room.feuchte:
                hum_h = (await safe(self.history("sensor", room.feuchte, hist_start, end), "Feuchte") or {}).get("state")
            if co2_h is None and room.co2:
                co2_h = (await safe(self.history("sensor", room.co2, hist_start, end), "CO2") or {}).get("state")
            vs = an.ventilation_success(openings, hum_h, co2_h)
            vs["ereignisse"] = [
                {**{k: v for k, v in e.items() if k != "start"}, "start": int(e["start"] * 1000)} for e in vs["ereignisse"]
            ]
            out["lueftung"] = vs
        return out

    async def current(self, rooms: list[Room], allgemein: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Aktuelle Werte aller Räume aus /api/states."""
        allgemein = allgemein or {}
        states = {s["entity_id"]: s for s in await self.client.get_states()}
        result = {}
        for room in rooms:
            cl = states.get(room.climate or "", {})
            attrs = cl.get("attributes") or {}
            result[room.raum] = {
                "modus": cl.get("state"),
                "hvac_action": attrs.get("hvac_action"),
                "anzeige": attrs.get("anzeige"),
                "grund": attrs.get("grund"),
                "preset": attrs.get("preset_mode"),
                "ist": attrs.get("current_temperature"),
                "soll": attrs.get("temperature"),
                "feuchte": an.to_float(states.get(room.feuchte or "", {}).get("state")),
                "schimmel": an.to_float(states.get(room.schimmel or "", {}).get("state")),
                "co2": an.to_float(states.get(room.co2 or "", {}).get("state")),
                "luftqualitaet": states.get(room.luftqualitaet or "", {}).get("state"),
                "fenster": states.get(room.fenster or "", {}).get("state"),
                "lueften": states.get(room.lueften or "", {}).get("state"),
            }
        emp = states.get(allgemein.get("empfehlung") or "", {})
        return {
            "raeume": result,
            "empfehlung": emp.get("state"),
            "empfehlungen": (emp.get("attributes") or {}).get("liste") or [],
            "aussen": {
                "temperatur": an.to_float(states.get(allgemein.get("aussentemperatur") or "", {}).get("state")),
                "feuchte": an.to_float(states.get(allgemein.get("aussenfeuchte") or "", {}).get("state")),
            },
        }
