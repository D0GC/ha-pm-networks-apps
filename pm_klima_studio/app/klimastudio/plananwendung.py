"""Plananwendung (nur Betriebsart ``generisch`` mit Option ``plan_anwenden``).

Ohne PM Klima stellt Klima Studio die Thermostate selbst nach den Heizplänen ein. Ein
Hintergrund-Task prüft minütlich jeden Raum im App-Modus ``plan``:

* Soll = Blocktemperatur des Heizplans (``temperatur`` im Block; Attribut der schedule-Entität,
  sonst aus ``schedule/list`` wie im Heizplan-Editor), außerhalb der Blöcke die Absenktemperatur
  (Override ``absenk`` des Raums, sonst Option ``absenktemperatur``). Ohne Heizplan kein Soll.
* Geschrieben wird nur, wenn sich das Soll gegenüber dem zuletzt von der App geschriebenen Wert
  ändert, nicht bei Overlay, Boost, Hand, Aus oder Sommer-Pause, nicht bei ausgeschaltetem oder
  nicht verfügbarem Thermostat (dann 5 Minuten Pause) und nicht erhöhend bei offenem Fenster.
* Weicht der Sollwert am Thermostat vom zuletzt geschriebenen ab (Handänderung am Gerät), gilt er
  als Overlay bis zum nächsten Planwechsel.
* Abgelaufene Overlays und Boosts kehren zum Plan-Soll bzw. zum vorherigen Wert/Preset zurück.

Jede Schreibaktion wird als Ereignis ``plan`` protokolliert. Zustand und Schreibbefehle liegen im
Adapter (``adapter.generisch``); dieses Modul enthält die Planauswertung und den Ablauf.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from . import analytics as an
from .config import Room
from .ha_client import HAClient, HAError
from .schedule import DAY_MINUTES, DAYS, parse_time_s

if TYPE_CHECKING:
    from .adapter.generisch import GenerischAdapter

_LOGGER = logging.getLogger(__name__)

INTERVALL = 60  # Sekunden zwischen zwei Durchläufen
BACKOFF = timedelta(minutes=5)  # Pause nach nicht verfügbarem Thermostat oder Schreibfehler
KARENZ = timedelta(minutes=5)  # nach eigenem Schreiben: Zeit für die Rückmeldung des Thermostats
PLAN_TTL = timedelta(minutes=2)  # Zwischenspeicher für schedule/list
BEREIT_WARTEN = 300
NICHT_VERFUEGBAR = ("unavailable", "unknown")
TOLERANZ = 0.01


@dataclass
class PlanInfo:
    """Heizplan eines Raums zu einem Zeitpunkt."""

    soll: float | None  # Plan-Sollwert (Block oder Absenkung); None = unbekannt/kein Plan
    im_block: bool | None
    wechsel: datetime | None  # nächster Planwechsel (zeitzonenbehaftet)


def zeit(value: Any) -> datetime | None:
    """ISO-Zeitpunkt lesen (ohne Zeitzone: UTC); sonst None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _sekunden(value: Any) -> int | None:
    try:
        minuten, sekunden = parse_time_s(str(value))
    except ValueError:
        return None
    return minuten * 60 + sekunden


def block_jetzt(item: dict[str, Any], lokal: datetime) -> tuple[bool, float | None]:
    """Liegt ``lokal`` in einem Block des Zeitplans? Rückgabe ``(im_block, temperatur)``."""
    jetzt_s = lokal.hour * 3600 + lokal.minute * 60 + lokal.second
    for rng in item.get(DAYS[lokal.weekday()]) or []:
        von, bis = _sekunden(rng.get("from")), _sekunden(rng.get("to"))
        if von is None or bis is None:
            continue
        if von <= jetzt_s < bis:
            return True, an.to_float((rng.get("data") or {}).get("temperatur"))
    return False, None


def naechster_wechsel(item: dict[str, Any], jetzt: datetime, tz: ZoneInfo) -> datetime | None:
    """Nächster Blockbeginn oder Blockende nach ``jetzt`` (innerhalb von acht Tagen)."""
    heute = jetzt.astimezone(tz).date()
    kandidaten: list[datetime] = []
    for d in range(9):
        tag: date = heute + timedelta(days=d)
        for rng in item.get(DAYS[tag.weekday()]) or []:
            for key in ("from", "to"):
                s = _sekunden(rng.get(key))
                if s is None:
                    continue
                if s >= DAY_MINUTES * 60:
                    t = datetime.combine(tag + timedelta(days=1), time(0), tzinfo=tz)
                else:
                    t = datetime.combine(tag, time(s // 3600, s // 60 % 60, s % 60), tzinfo=tz)
                if t > jetzt:
                    kandidaten.append(t)
        if kandidaten:
            return min(kandidaten)
    return None


def absenk_fuer(room: Room, standard: float) -> float:
    return room.absenk if room.absenk is not None else standard


def plan_info(
    room: Room,
    by_id: dict[str, dict[str, Any]],
    item: dict[str, Any] | None,
    jetzt: datetime,
    tz: ZoneInfo,
    absenk: float,
) -> PlanInfo:
    """Plan-Soll eines Raums: Zustand on/off der schedule-Entität bestimmt Block oder Absenkung,
    die Blocktemperatur kommt aus ihrem Attribut ``temperatur`` bzw. aus dem Block in ``schedule/list``."""
    if not room.schedule:
        return PlanInfo(None, None, None)
    st = by_id.get(room.schedule) or {}
    attrs = st.get("attributes") or {}
    ib, it = block_jetzt(item, jetzt.astimezone(tz)) if item else (None, None)
    zustand = st.get("state")
    wechsel: datetime | None = None
    if zustand in ("on", "off"):
        im_block: bool | None = zustand == "on"
        temp = an.to_float(attrs.get("temperatur")) if im_block else None
        if im_block and temp is None and ib:
            temp = it
        wechsel = zeit(attrs.get("next_event"))
    else:
        im_block, temp = ib, it
    if (wechsel is None or wechsel <= jetzt) and item:
        wechsel = naechster_wechsel(item, jetzt, tz)
    soll = None if im_block is None else (temp if im_block else absenk_fuer(room, absenk))
    return PlanInfo(soll, im_block, wechsel)


class PlanQuelle:
    """Heizpläne (``schedule/list``) mit Zuordnung Entität -> Speicher-ID, kurz zwischengespeichert."""

    def __init__(self, client: HAClient, uhr: Callable[[], datetime]) -> None:
        self.client = client
        self.uhr = uhr
        self._items: dict[str, dict[str, Any]] | None = None
        self._stand: datetime | None = None
        self._ids: dict[str, str | None] = {}

    def invalidieren(self) -> None:
        self._items = None

    async def items(self, rooms: list[Room]) -> dict[str, dict[str, Any]]:
        """entity_id der schedule-Entität -> Zeitplan (nur Räume mit Heizplan). Fehler: leer."""
        jetzt = self.uhr()
        if self._items is None or self._stand is None or not self._stand <= jetzt < self._stand + PLAN_TTL:
            try:
                self._items = {str(i.get("id")): i for i in await self.client.schedule_list() if isinstance(i, dict)}
                self._stand = jetzt
            except HAError as err:
                _LOGGER.info("Heizpläne nicht lesbar, verwende den Zustand der schedule-Entitäten: %s", err)
                return {}
        out: dict[str, dict[str, Any]] = {}
        for room in rooms:
            eid = room.schedule
            if not eid:
                continue
            if eid not in self._ids:
                try:
                    entry = await self.client.entity_registry_get(eid)
                    self._ids[eid] = str(entry["unique_id"]) if entry.get("unique_id") else None
                except (HAError, KeyError) as err:
                    _LOGGER.debug("Registrierung von %s nicht lesbar: %s", eid, err)
                    continue
            sid = self._ids.get(eid)
            item = self._items.get(sid or "")
            if item is not None:
                out[eid] = item
        return out


class Plananwendung:
    """Hintergrund-Task: Heizpläne auf die Thermostate der Räume im App-Modus ``plan`` anwenden."""

    def __init__(self, adapter: GenerischAdapter) -> None:
        self.adapter = adapter
        self._task: asyncio.Task | None = None
        self._backoff: dict[str, datetime] = {}
        self.letzter_lauf: datetime | None = None

    # ------------------------------------------------------------- Hintergrund

    @property
    def laeuft(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, bereit: Callable[[], bool] | None = None) -> None:
        if not self.laeuft:
            _LOGGER.info("Plananwendung gestartet (Betriebsart generisch)")
            self._task = asyncio.create_task(self._run(bereit or (lambda: True)))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self, bereit: Callable[[], bool]) -> None:
        gewartet = 0
        while not bereit() and gewartet < BEREIT_WARTEN:  # Zeitzone von HA abwarten
            await asyncio.sleep(5)
            gewartet += 5
        while True:
            try:
                await self.takt()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Plananwendung fehlgeschlagen")
            await asyncio.sleep(INTERVALL)

    # ------------------------------------------------------------- Durchlauf

    async def takt(self, erzwingen: bool = False) -> None:
        """Alle Räume einmal prüfen."""
        ad = self.adapter
        states = await ad.client.get_states()
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        rooms = await ad.raeume_aktuell(states)
        await ad.vorbereiten(rooms, by_id)
        jetzt = ad.uhr()
        async with ad.lock:
            for room in rooms:
                try:
                    await self.raum(room, by_id, jetzt, erzwingen)
                except HAError as err:
                    _LOGGER.warning("Plananwendung %s: %s (neuer Versuch in %s Minuten)", room.raum, err, BACKOFF.seconds // 60)
                    self._backoff[room.raum] = jetzt + BACKOFF
        self.letzter_lauf = jetzt

    async def takt_raum(self, room: Room) -> None:
        """Einen Raum sofort anwenden (z. B. nach dem Wechsel in den Modus Plan)."""
        ad = self.adapter
        by_id = {s["entity_id"]: s for s in await ad.client.get_states() if "entity_id" in s}
        await ad.vorbereiten([room], by_id)
        async with ad.lock:
            await self.raum(room, by_id, ad.uhr(), erzwingen=True)

    async def raum(self, room: Room, by_id: dict[str, dict[str, Any]], jetzt: datetime, erzwingen: bool = False) -> None:
        """Einen Raum prüfen (Aufrufer hält ``adapter.lock``)."""
        ad = self.adapter
        if not room.climate:
            return
        cl = by_id.get(room.climate)
        if cl is None or cl.get("state") in (None, *NICHT_VERFUEGBAR):
            if room.raum not in self._backoff or self._backoff[room.raum] <= jetzt:
                _LOGGER.info("%s nicht verfügbar, Plananwendung pausiert %s Minuten", room.climate, BACKOFF.seconds // 60)
            self._backoff[room.raum] = jetzt + BACKOFF
            return
        if not erzwingen and self._backoff.get(room.raum, jetzt) > jetzt:
            return
        self._backoff.pop(room.raum, None)
        if not ad.raum_faehigkeiten(room, by_id)["steuerbar"]:
            return
        a = ad.app(room.raum)
        alt = dict(a)
        try:
            await self._raum(room, cl, a, jetzt, erzwingen, by_id)
        finally:
            if a != alt:
                await ad.app_speichern(room.raum, a)

    async def _raum(
        self,
        room: Room,
        cl: dict[str, Any],
        a: dict[str, Any],
        jetzt: datetime,
        erzwingen: bool,
        by_id: dict[str, dict[str, Any]],
    ) -> None:
        ad = self.adapter
        info = ad.plan_info_cache(room.raum)
        cl = {**cl, "state": await ad.sommer_abgleich(room, a, cl)}
        # Abgelaufene Timer: zurück zum Plan-Soll bzw. zum vorherigen Wert/Preset
        for feld in ("boost_bis", "overlay_bis"):
            bis = zeit(a.get(feld))
            if bis is not None and bis <= jetzt:
                await ad.rueckkehr(room, a, cl, info, "Zeit abgelaufen")
                return
        if a["modus"] != "plan" or ad.pause or a.get("overlay_bis") or a.get("boost_bis"):
            return
        if cl.get("state") == "off":  # im Plan-Modus nie einschalten
            return
        if info is None:
            return
        attrs = cl.get("attributes") or {}
        ist_soll = an.to_float(attrs.get("temperature"))
        geschrieben = a.get("geschrieben")
        # Handänderung am Thermostat: gilt bis zum nächsten Planwechsel
        if not erzwingen and geschrieben is not None and ist_soll is not None and abs(ist_soll - geschrieben) > TOLERANZ:
            seit = zeit(a.get("geschrieben_zeit"))
            if seit is not None and jetzt - seit < KARENZ:
                return
            a["overlay_bis"] = iso(info.wechsel) if info.wechsel else "dauerhaft"
            a["overlay_temp"] = ist_soll
            a["vor_temp"] = geschrieben
            a["geschrieben"] = ist_soll
            bis = ad.zeit_text(info.wechsel)
            await ad.protokoll(
                room,
                f"Handänderung am Thermostat auf {ad.fmt(ist_soll)} °C erkannt, gilt {bis}",
                {"temperatur": ist_soll, "bis": a["overlay_bis"], "anlass": "handaenderung"},
            )
            return
        if info.soll is None:
            return
        soll = ad.runden(info.soll, attrs)
        if soll > (ist_soll if ist_soll is not None else soll) and ad.fenster_offen(room, by_id):
            return  # bei offenem Fenster nicht erhöhen
        if ist_soll is not None and abs(ist_soll - soll) <= TOLERANZ:
            if geschrieben is None or abs(geschrieben - soll) > TOLERANZ:
                a["geschrieben"] = soll
                a["geschrieben_zeit"] = iso(jetzt)
            return
        if not erzwingen and geschrieben is not None and abs(geschrieben - soll) <= TOLERANZ:
            return
        text = f"Heizplan {ad.fmt(soll)} °C" if info.im_block else f"Absenkung {ad.fmt(soll)} °C"
        await ad.soll_schreiben(room, a, soll, attrs, text)
