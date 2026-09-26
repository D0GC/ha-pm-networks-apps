"""Wochenbericht: Inhalt, Texte im Jarvis-Register, Archiv und Zeitsteuerung."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

ARCHIVE_MAX = 12

EINLEITUNGEN: tuple[str, ...] = (
    "Der Wochenbericht zum Raumklima liegt vor.",
    "Hier ist die Zusammenfassung der vergangenen sieben Tage.",
    "Die Klimadaten der Woche sind ausgewertet.",
    "Ich habe die Woche für Sie zusammengefasst, Sir.",
    "Die Auswertung der letzten sieben Tage ist abgeschlossen.",
    "Der Bericht zur vergangenen Woche ist erstellt.",
    "Die Woche in Zahlen, kurz gefasst.",
    "Hier die Übersicht über Heizung, Feuchte und Luftqualität.",
    "Die wöchentliche Klimabilanz liegt bereit.",
    "Ein kurzer Rückblick auf das Raumklima der Woche.",
)

SCHLUSSSAETZE: tuple[str, ...] = (
    "Das wäre alles für diese Woche.",
    "Weitere Details finden Sie in Klima Studio.",
    "Ich melde mich nächste Woche wieder.",
    "Die Einzelheiten stehen in den Auswertungen bereit.",
    "Mehr gibt es derzeit nicht zu berichten.",
    "Die Heizung hat ihren Teil erledigt. Der Rest liegt bei den Fenstern.",
    "Damit ist die Woche dokumentiert.",
    "Bei Bedarf stehen die Verläufe im Detail zur Verfügung.",
    "Ich behalte die Werte weiter im Blick.",
    "Mehr Ordnung hat das Raumklima nicht verlangt.",
)

FEUCHTE_HINWEIS_STUNDEN = 6.0
CO2_HINWEIS_STUNDEN = 5.0
FENSTER_LANG_MIN = 60


def _fmt_num(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "–"
    text = f"{value:.{digits}f}"
    if digits and text.endswith("0" * digits):
        text = text[: -(digits + 1)]
    return text.replace(".", ",")


def _dauer(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 90:
        return f"{minutes} Minuten"
    hours, rest = divmod(minutes, 60)
    h_text = "1 Stunde" if hours == 1 else f"{hours} Stunden"
    return f"{h_text} {rest} Minuten" if rest else h_text


def room_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    """Kennzahlen eines Raums aus ``DataService.room_analysis`` (detail=False)."""
    feuchte = analysis.get("feuchte") or {}
    schimmel = analysis.get("schimmel") or {}
    co2 = analysis.get("co2") or {}
    fenster = analysis.get("fenster") or {}
    lueftung = analysis.get("lueftung") or {}
    laengste = fenster.get("laengste") or None
    return {
        "raum": analysis.get("raum"),
        "name": analysis.get("name"),
        "heizstunden": analysis.get("heizstunden"),
        "feuchte_mittel": feuchte.get("mittel"),
        "feuchte_max": feuchte.get("max"),
        "feuchte_stunden_70": feuchte.get("stunden_70"),
        "feuchte_stunden_80": feuchte.get("stunden_80"),
        "schimmel_max": schimmel.get("max"),
        "schimmel_stunden_70": schimmel.get("stunden_70"),
        "schimmel_stunden_80": schimmel.get("stunden_80"),
        "co2_max": co2.get("max"),
        "co2_spitzen_1000": co2.get("spitzen_1000"),
        "co2_spitzen_1400": co2.get("spitzen_1400"),
        "co2_stunden_1000": co2.get("stunden_1000"),
        "fenster_anzahl": fenster.get("anzahl"),
        "fenster_offen_stunden": fenster.get("offen_stunden"),
        "fenster_laengste": laengste,
        "lueftung_bewertung": lueftung.get("bewertung"),
    }


def find_findings(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Auffällige Räume mit Schweregrad (höher = wichtiger)."""
    out: list[dict[str, Any]] = []
    for r in rooms:
        name = r["name"]
        if (r.get("schimmel_stunden_80") or 0) > 0 or (r.get("schimmel_max") or 0) >= 80:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "schimmel",
                    "stufe": 3,
                    "text": f"{name}: Schimmelrisiko zeitweise über 80 % (Höchstwert {_fmt_num(r.get('schimmel_max'))} %).",
                }
            )
        elif (r.get("schimmel_stunden_70") or 0) >= FEUCHTE_HINWEIS_STUNDEN:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "schimmel",
                    "stufe": 2,
                    "text": f"{name}: Schimmelrisiko {_fmt_num(r['schimmel_stunden_70'])} Stunden über 70 %.",
                }
            )
        if (r.get("feuchte_stunden_80") or 0) > 0:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "feuchte",
                    "stufe": 3,
                    "text": f"{name}: Luftfeuchte {_fmt_num(r['feuchte_stunden_80'])} Stunden über 80 %.",
                }
            )
        elif (r.get("feuchte_stunden_70") or 0) >= FEUCHTE_HINWEIS_STUNDEN:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "feuchte",
                    "stufe": 2,
                    "text": f"{name}: Luftfeuchte {_fmt_num(r['feuchte_stunden_70'])} Stunden über 70 %.",
                }
            )
        if (r.get("co2_spitzen_1400") or 0) > 0:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "co2",
                    "stufe": 2,
                    "text": f"{name}: {r['co2_spitzen_1400']} CO2-Spitzen über 1400 ppm "
                    f"(Höchstwert {_fmt_num(r.get('co2_max'), 0)} ppm).",
                }
            )
        elif (r.get("co2_stunden_1000") or 0) >= CO2_HINWEIS_STUNDEN:
            out.append(
                {
                    "raum": r["raum"],
                    "thema": "co2",
                    "stufe": 1,
                    "text": f"{name}: CO2 {_fmt_num(r['co2_stunden_1000'])} Stunden über 1000 ppm.",
                }
            )
    out.sort(key=lambda f: -f["stufe"])
    return out


def longest_windows(rooms: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    items = [{"raum": r["raum"], "name": r["name"], **r["fenster_laengste"]} for r in rooms if r.get("fenster_laengste")]
    items.sort(key=lambda w: -w["dauer_min"])
    return items[:limit]


def recommendations(rooms: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[str]:
    """1-3 Empfehlungen, zurückhaltend formuliert."""
    recs: list[str] = []
    by_room = {r["raum"]: r for r in rooms}
    seen: set[tuple[str, str]] = set()
    for f in findings:
        key = (f["raum"], "feuchte" if f["thema"] in ("feuchte", "schimmel") else f["thema"])
        if key in seen:
            continue
        seen.add(key)
        name = by_room[f["raum"]]["name"]
        if key[1] == "feuchte":
            recs.append(f"Im Raum {name} empfiehlt sich zweimal täglich kurzes Stoßlüften von etwa zehn Minuten.")
        elif key[1] == "co2":
            recs.append(f"Im Raum {name} wäre häufigeres kurzes Lüften ratsam, vor allem am Abend.")
    for r in rooms:
        lw = r.get("fenster_laengste")
        if lw and lw.get("dauer_min", 0) >= FENSTER_LANG_MIN and (r.get("heizstunden") or 0) > 0:
            recs.append(
                f"Das Fenster im Raum {r['name']} stand bis zu {_dauer(lw['dauer_min'])} offen. "
                "Kürzeres Stoßlüften würde Heizenergie sparen."
            )
    heat = [r for r in rooms if r.get("heizstunden") is not None]
    if len(heat) >= 2:
        avg = sum(r["heizstunden"] for r in heat) / len(heat)
        top = max(heat, key=lambda r: r["heizstunden"])
        if top["heizstunden"] >= 20 and top["heizstunden"] > 1.6 * avg:
            recs.append(
                f"Die Heizzeit im Raum {top['name']} liegt deutlich über den übrigen Räumen. "
                "Ein Blick auf den Heizplan könnte sich lohnen."
            )
    for r in rooms:
        if r.get("lueftung_bewertung") == "gering":
            recs.append(f"Das Lüften im Raum {r['name']} zeigte wenig Wirkung. Querlüften wäre wirksamer.")
    if not recs:
        recs.append("Es empfiehlt sich, den bisherigen Heiz- und Lüftungsrhythmus beizubehalten.")
    unique: list[str] = []
    for rec in recs:
        if rec not in unique:
            unique.append(rec)
    return unique[:3]


def _clean(text: str) -> str:
    """Register-Schutz: keine Ausrufezeichen."""
    return text.replace("!", ".")


def build_report(
    analyses: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    rng: random.Random | None = None,
    anlass: str = "zeitplan",
) -> dict[str, Any]:
    rng = rng or random.Random()
    rooms = [room_summary(a) for a in analyses]
    findings = find_findings(rooms)
    windows = longest_windows(rooms)
    recs = recommendations(rooms, findings)
    intro = rng.choice(EINLEITUNGEN)
    outro = rng.choice(SCHLUSSSAETZE)

    lines = [intro, "", "Heizstunden:"]
    for r in rooms:
        lines.append(
            f"- {r['name']}: {_fmt_num(r['heizstunden'])} h"
            if r.get("heizstunden") is not None
            else f"- {r['name']}: keine Daten"
        )
    lines.append("")
    if findings:
        lines.append("Auffällig:")
        lines.extend(f"- {f['text']}" for f in findings)
    else:
        lines.append("Auffällig: keine Räume. Feuchte, Schimmelrisiko und CO2 blieben im Rahmen.")
    lines.append("")
    if windows:
        lines.append("Längste offene Fenster:")
        for w in windows:
            when = datetime.fromtimestamp(w["start"] / 1000, tz).strftime("%a %d.%m. %H:%M")
            lines.append(f"- {w['name']}: {_dauer(w['dauer_min'])} ab {_wochentag(when)}")
    else:
        lines.append("Längste offene Fenster: keine Öffnungen erfasst.")
    lines.append("")
    lines.append("Empfehlungen:")
    lines.extend(f"- {rec}" for rec in recs)
    lines.extend(["", outro])
    text = _clean("\n".join(lines))

    heiz = [f"{r['name']} {_fmt_num(r['heizstunden'])} h" for r in rooms if r.get("heizstunden") is not None]
    push = [intro, "Heizstunden: " + (", ".join(heiz) if heiz else "keine Daten") + "."]
    if findings:
        push.append("Auffällig: " + " ".join(f["text"] for f in findings[:2]))
    push.append("Empfehlung: " + recs[0])
    push_text = _clean(" ".join(push))

    created = datetime.now(tz)
    return {
        "id": created.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
        "erstellt": created.isoformat(timespec="seconds"),
        "anlass": anlass,
        "zeitraum": {
            "start": start.astimezone(tz).isoformat(timespec="minutes"),
            "ende": end.astimezone(tz).isoformat(timespec="minutes"),
        },
        "einleitung": intro,
        "schluss": outro,
        "raeume": rooms,
        "auffaellig": findings,
        "fenster_laengste": windows,
        "empfehlungen": recs,
        "text": text,
        "push_text": push_text,
        "push": [],
    }


_WOCHENTAGE = {"Mon": "Mo", "Tue": "Di", "Wed": "Mi", "Thu": "Do", "Fri": "Fr", "Sat": "Sa", "Sun": "So"}


def _wochentag(text: str) -> str:
    for en, de in _WOCHENTAGE.items():
        text = text.replace(en, de)
    return text


# ------------------------------------------------------------------ Archiv


class ReportArchive:
    """Letzte ``ARCHIVE_MAX`` Berichte als JSON in /data (keine Tokens)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as err:
            _LOGGER.error("Berichtsarchiv nicht lesbar: %s", err)
            return []
        return data if isinstance(data, list) else []

    def add(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        items = [report, *[r for r in self.load() if r.get("id") != report.get("id")]][:ARCHIVE_MAX]
        self._write(items)
        return items

    def update(self, report: dict[str, Any]) -> None:
        items = [report if r.get("id") == report.get("id") else r for r in self.load()]
        self._write(items)

    def get(self, report_id: str) -> dict[str, Any] | None:
        return next((r for r in self.load() if r.get("id") == report_id), None)

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# ------------------------------------------------------------------ Zeitsteuerung


def next_run(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """Nächster Termin (lokale Wandzeit, DST-sicher über ZoneInfo)."""
    tz = now.tzinfo
    days_ahead = (weekday - now.weekday()) % 7
    day = (now + timedelta(days=days_ahead)).date()
    cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if cand <= now:
        day = day + timedelta(days=7)
        cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return cand


class WeeklyScheduler:
    """Löst den Bericht einmal pro Woche aus. Schläft in Etappen (Uhr-/DST-robust)."""

    def __init__(
        self,
        job: Callable[[], Awaitable[Any]],
        weekday: int,
        hour: int,
        minute: int,
        tz: ZoneInfo,
    ) -> None:
        self.job = job
        self.weekday, self.hour, self.minute = weekday, hour, minute
        self.tz = tz
        self._task: asyncio.Task | None = None
        self.next: datetime | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def set_tz(self, tz: ZoneInfo) -> None:
        """Zeitzone ändern und den nächsten Termin neu berechnen."""
        self.tz = tz
        if self._task is not None:
            self._task.cancel()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            self.next = next_run(datetime.now(self.tz), self.weekday, self.hour, self.minute)
            _LOGGER.info("Nächster Wochenbericht: %s", self.next.isoformat(timespec="minutes"))
            while True:
                remaining = (self.next - datetime.now(self.tz)).total_seconds()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 900))
            try:
                await self.job()
            except Exception:
                _LOGGER.exception("Wochenbericht fehlgeschlagen")
            await asyncio.sleep(61)
