"""Reine Auswertungsfunktionen (ohne I/O, ohne Pandas).

Zeitreihen sind Listen von ``(ts, wert)`` mit ``ts`` in Sekunden (UTC-Epoche),
aufsteigend sortiert. Ein Wert gilt bis zum nächsten Eintrag (Treppenfunktion),
``None`` bedeutet "unbekannt/nicht verfügbar".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

Series = list[tuple[float, Any]]

UNAVAILABLE = {"unavailable", "unknown", "", None}


# ------------------------------------------------------------------ Parsing


def _ts_of(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_history_rows(rows: Iterable[dict[str, Any]]) -> list[tuple[float, str, dict[str, Any]]]:
    """Kompakte WS-Historie (``s``/``a``/``lu``/``lc``) oder REST-Historie
    (``state``/``attributes``/``last_updated``/``last_changed``) vereinheitlichen.

    Attribute, die bei ``minimal_response`` fehlen, werden vom Vorgänger übernommen.
    """
    out: list[tuple[float, str, dict[str, Any]]] = []
    last_attrs: dict[str, Any] = {}
    for row in rows:
        if "s" in row or "lu" in row:
            state = row.get("s")
            attrs = row.get("a")
            ts = _ts_of(row.get("lu")) or _ts_of(row.get("lc"))
        else:
            state = row.get("state")
            attrs = row.get("attributes")
            ts = _ts_of(row.get("last_updated")) or _ts_of(row.get("last_changed"))
        if ts is None:
            continue
        if attrs is None:
            attrs = last_attrs
        else:
            last_attrs = attrs
        out.append((ts, state, attrs))
    out.sort(key=lambda r: r[0])
    return out


def to_float(value: Any) -> float | None:
    if value in UNAVAILABLE:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def numeric_series(records: list[tuple[float, str, dict[str, Any]]]) -> Series:
    """Zustand als Zahl; nicht numerisch -> None."""
    return dedupe([(ts, to_float(state)) for ts, state, _ in records])


def attr_series(records: list[tuple[float, str, dict[str, Any]]], key: str, numeric: bool = True) -> Series:
    series: Series = []
    for ts, state, attrs in records:
        if state in ("unavailable", "unknown"):
            series.append((ts, None))
            continue
        val = attrs.get(key) if attrs else None
        series.append((ts, to_float(val) if numeric else val))
    return dedupe(series)


def state_series(records: list[tuple[float, str, dict[str, Any]]]) -> Series:
    return dedupe([(ts, None if state in UNAVAILABLE else state) for ts, state, _ in records])


def dedupe(series: Series) -> Series:
    """Aufeinanderfolgende gleiche Werte zusammenfassen (spart Speicher)."""
    out: Series = []
    for ts, val in series:
        if out and out[-1][1] == val:
            continue
        if out and out[-1][0] == ts:
            out[-1] = (ts, val)
            if len(out) > 1 and out[-2][1] == val:
                out.pop()
            continue
        out.append((ts, val))
    return out


def merge_series(parts: Iterable[Series]) -> Series:
    merged: Series = []
    for part in parts:
        for ts, val in part:
            if merged and ts < merged[-1][0]:
                continue
            merged.append((ts, val))
    return dedupe(merged)


# ------------------------------------------------------------------ Intervalle


def value_at(series: Series, ts: float) -> Any:
    """Wert zum Zeitpunkt ``ts`` (letzter Eintrag <= ts)."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else None


def intervals_where(series: Series, predicate: Callable[[Any], bool], start: float, end: float) -> list[tuple[float, float]]:
    """Zeitintervalle innerhalb [start, end], in denen ``predicate(wert)`` gilt."""
    result: list[tuple[float, float]] = []
    if end <= start or not series:
        return result
    for idx, (ts, val) in enumerate(series):
        seg_start = max(ts, start)
        seg_end = min(series[idx + 1][0] if idx + 1 < len(series) else end, end)
        if seg_end <= seg_start:
            continue
        if val is not None and predicate(val):
            if result and abs(result[-1][1] - seg_start) < 1e-6:
                result[-1] = (result[-1][0], seg_end)
            else:
                result.append((seg_start, seg_end))
    return result


def duration_where(series: Series, predicate: Callable[[Any], bool], start: float, end: float) -> float:
    return sum(e - s for s, e in intervals_where(series, predicate, start, end))


def heating_seconds(hvac_action: Series, start: float, end: float) -> float:
    """Dauer mit ``hvac_action == 'heating'``."""
    return duration_where(hvac_action, lambda v: v == "heating", start, end)


def window_open_intervals(window: Series, start: float, end: float) -> list[tuple[float, float]]:
    return intervals_where(window, lambda v: v == "on", start, end)


def hours_above(series: Series, threshold: float, start: float, end: float) -> float:
    return duration_where(series, lambda v: v >= threshold, start, end) / 3600


# ------------------------------------------------------------------ Spitzen


def episodes_above(
    series: Series, threshold: float, start: float, end: float, max_series: Series | None = None
) -> list[dict[str, float]]:
    """Zusammenhängende Phasen über ``threshold`` mit Dauer und Maximum."""
    eps = []
    source_max = max_series if max_series is not None else series
    for s, e in intervals_where(series, lambda v: v > threshold, start, end):
        peak = max(
            (v for ts, v in source_max if s <= ts < e and v is not None),
            default=value_at(source_max, s),
        )
        start_val = value_at(source_max, s)
        if start_val is not None and (peak is None or start_val > peak):
            peak = start_val
        eps.append({"start": s, "end": e, "dauer_min": round((e - s) / 60, 1), "max": peak})
    return eps


def co2_peaks(series: Series, start: float, end: float, max_series: Series | None = None) -> dict[str, Any]:
    over1000 = episodes_above(series, 1000, start, end, max_series)
    over1400 = episodes_above(series, 1400, start, end, max_series)
    values = [v for ts, v in (max_series or series) if v is not None and start <= ts <= end]
    first = value_at(max_series or series, start)
    if first is not None:
        values.append(first)
    return {
        "max": max(values) if values else None,
        "spitzen_1000": len(over1000),
        "spitzen_1400": len(over1400),
        "stunden_1000": round(sum(e["end"] - e["start"] for e in over1000) / 3600, 2),
        "stunden_1400": round(sum(e["end"] - e["start"] for e in over1400) / 3600, 2),
        "episoden": over1400[:20] if over1400 else over1000[:20],
    }


# ------------------------------------------------------------------ Lüftung


def ventilation_success(
    openings: list[tuple[float, float]],
    humidity: Series | None,
    co2: Series | None,
    window_s: float = 1800,
) -> dict[str, Any]:
    """Feuchte-/CO2-Abfall in den ``window_s`` Sekunden nach jeder Fensteröffnung.

    Abfall = Wert beim Öffnen minus Minimum im Fenster [t0, t0 + window_s].
    """
    events = []
    for t0, t_close in openings:
        ev: dict[str, Any] = {"start": t0, "dauer_min": round((t_close - t0) / 60, 1)}
        for key, series in (("feuchte", humidity), ("co2", co2)):
            if not series:
                continue
            v0 = value_at(series, t0)
            if v0 is None:
                continue
            within = [v for ts, v in series if t0 < ts <= t0 + window_s and v is not None]
            vmin = min([v0, *within])
            ev[f"{key}_start"] = v0
            ev[f"{key}_abfall"] = round(v0 - vmin, 1)
        events.append(ev)

    def _avg(key: str) -> float | None:
        vals = [e[key] for e in events if key in e]
        return round(sum(vals) / len(vals), 1) if vals else None

    hum = _avg("feuchte_abfall")
    co2_avg = _avg("co2_abfall")
    if not events:
        bewertung = "keine Daten"
    elif (hum is not None and hum >= 5) or (co2_avg is not None and co2_avg >= 300):
        bewertung = "wirksam"
    elif (hum is not None and hum >= 2) or (co2_avg is not None and co2_avg >= 100):
        bewertung = "mäßig"
    else:
        bewertung = "gering"
    return {
        "anzahl": len(events),
        "feuchte_abfall_mittel": hum,
        "co2_abfall_mittel": co2_avg,
        "bewertung": bewertung,
        "ereignisse": events[-30:],
    }


# ------------------------------------------------------------------ Diagramm


def downsample(series: Series, start: float, end: float, max_points: int = 300) -> list[list[Any]]:
    """Auf höchstens ``max_points`` Stützstellen reduzieren (Mittelwert je Eimer).

    Liefert ``[[ts_ms, wert], ...]``; Lücken (None) bleiben als None erhalten.
    """
    if not series or end <= start:
        return []
    in_range: Series = []
    first = value_at(series, start)
    if first is not None or series[0][0] <= start:
        in_range.append((start, first))
    in_range.extend((ts, v) for ts, v in series if start < ts <= end)
    if len(in_range) <= max_points:
        pts = [[int(ts * 1000), _r(v)] for ts, v in in_range]
    else:
        bucket = (end - start) / max_points
        buckets: dict[int, list[float]] = {}
        gaps: set[int] = set()
        for ts, v in in_range:
            idx = min(int((ts - start) / bucket), max_points - 1)
            if v is None:
                gaps.add(idx)
            else:
                buckets.setdefault(idx, []).append(float(v))
        pts = []
        for idx in range(max_points):
            if idx in buckets:
                vals = buckets[idx]
                pts.append([int((start + (idx + 0.5) * bucket) * 1000), _r(sum(vals) / len(vals))])
            elif idx in gaps:
                pts.append([int((start + (idx + 0.5) * bucket) * 1000), None])
    last = value_at(series, end)
    if pts and last is not None and pts[-1][0] < int(end * 1000):
        pts.append([int(end * 1000), _r(last)])
    return pts


def _r(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    return value


def stats_to_series(rows: list[dict[str, Any]], key: str = "mean") -> Series:
    """Zeilen aus `recorder/statistics_during_period` (start in ms) -> Serie."""
    out: Series = []
    for row in rows:
        start = row.get("start")
        if start is None:
            continue
        ts = float(start) / 1000 if float(start) > 1e11 else float(start)
        out.append((ts, to_float(row.get(key))))
    out.sort(key=lambda p: p[0])
    return out
