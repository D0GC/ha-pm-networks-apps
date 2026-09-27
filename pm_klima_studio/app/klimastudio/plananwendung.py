"""Plananwendung (nur Betriebsart ``generisch``).

Ohne PM Klima stellt Klima Studio die Thermostate selbst nach den Heizplänen ein. Ein
Hintergrund-Task läuft im Modus generisch immer (Rück-Timer für Overlay und Boost, Sommer-Abgleich);
die Heizpläne selbst wendet er nur mit der Option ``plan_anwenden`` an, und nur in Räumen im
App-Modus ``plan``, deren Thermostat im Zustand ``heat`` steht:

* Maßgeblich für den Plan ist ``schedule/list`` (wie im Heizplan-Editor): Soll = ``temperatur`` des
  aktuellen Blocks, außerhalb der Blöcke die Absenktemperatur (Override ``absenk`` des Raums, sonst
  Option ``absenktemperatur``). Das Attribut ``temperatur`` der schedule-Entität ist nicht garantiert
  (HA übernimmt Block-Daten erst ab 2024.x als Attribute) und wird nur ergänzend genutzt; ihr Zustand
  on/off und ``next_event`` bestimmen Block und Wechsel, wenn vorhanden.
* Kein Plan-Soll (und keine Absenkung) ohne lesbaren Heizplan, bei einem Plan ohne Blöcke oder wenn
  ein Block keine Temperatur hat; der Grund steht als ``hinweis`` im Raumzustand.
* Geschrieben wird nur, wenn sich das Soll gegenüber dem zuletzt geschriebenen Wert ändert und der
  gerundete Wert vom aktuellen Sollwert des Thermostats abweicht (API-Limits z. B. bei tado), nicht
  bei Overlay, Boost, Hand, Aus oder Sommer-Pause, nicht bei nicht verfügbarem Thermostat (Pause
  mit wachsendem Abstand bis ``BACKOFF_MAX``) und nicht erhöhend bei offenem Fenster.
* Handänderung am Gerät: Erst wenn das Thermostat den geschriebenen Wert übernommen hatte (``gesehen``)
  und danach ein anderer Wert erscheint, gilt dieser als Overlay bis zum nächsten Planwechsel. Steht
  nach der Karenz noch der alte Wert, wird einmal erneut geschrieben, danach das Ereignis „nicht
  übernommen“ protokolliert. Bei offenem Fenster oder aktivem Preset ist eine Abweichung keine Handänderung.
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
BACKOFF = timedelta(minutes=5)  # Pause nach nicht verfügbarem Thermostat oder Schreibfehler (verdoppelt sich)
BACKOFF_MAX = timedelta(minutes=60)
KARENZ = timedelta(minutes=5)  # nach eigenem Schreiben: Zeit für die Rückmeldung des Thermostats
PLAN_TTL = timedelta(minutes=2)  # Zwischenspeicher für schedule/list
BEREIT_WARTEN = 300
NICHT_VERFUEGBAR = ("unavailable", "unknown")
TOLERANZ = 0.01  # Gleichheit gerundeter Sollwerte

HINWEIS_PLAN_NICHT_LESBAR = "Der Heizplan ist nicht lesbar. Klima Studio wendet ihn erst an, wenn er gelesen werden kann."
HINWEIS_PLAN_LEER = "Der Heizplan hat keine Zeitblöcke. Klima Studio wendet ihn nicht an (auch keine Absenkung)."
HINWEIS_PLAN_OHNE_TEMP = (
    "Im Heizplan fehlt in mindestens einem Block die Temperatur. Klima Studio wendet ihn nicht an (auch keine "
    "Absenkung). Tragen Sie im Heizplan-Editor für jeden Block eine Temperatur ein."
)


@dataclass
class PlanInfo:
    """Heizplan eines Raums zu einem Zeitpunkt."""

    soll: float | None  # Plan-Sollwert (Block oder Absenkung); None = unbekannt/kein Plan
    im_block: bool | None
    wechsel: datetime | None  # nächster Planwechsel (zeitzonenbehaftet)
    hinweis: str | None = None  # warum der Plan nicht angewendet wird


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


def bloecke(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [rng for tag in DAYS for rng in item.get(tag) or [] if isinstance(rng, dict)]


def plan_info(
    room: Room,
    by_id: dict[str, dict[str, Any]],
    item: dict[str, Any] | None,
    jetzt: datetime,
    tz: ZoneInfo,
    absenk: float,
) -> PlanInfo:
    """Plan-Soll eines Raums. Maßgeblich ist der Zeitplan aus ``schedule/list`` (``item``); der Zustand
    on/off der schedule-Entität bestimmt Block oder Absenkung, die Blocktemperatur kommt aus ihrem
    (nicht garantierten) Attribut ``temperatur``, sonst aus dem Block in ``item``.

    Ohne ``item``, ohne Blöcke oder mit einem Block ohne Temperatur gibt es kein Plan-Soll."""
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
    if item is None:
        return PlanInfo(None, im_block, wechsel, HINWEIS_PLAN_NICHT_LESBAR)
    alle = bloecke(item)
    if not alle:
        return PlanInfo(None, im_block, wechsel, HINWEIS_PLAN_LEER)
    if any(an.to_float((rng.get("data") or {}).get("temperatur")) is None for rng in alle):
        return PlanInfo(None, im_block, wechsel, HINWEIS_PLAN_OHNE_TEMP)
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
        self._stand = None
        self._ids.clear()

    def ids_leeren(self) -> None:
        """Zuordnung Entität -> Speicher-ID neu ermitteln (nach Auffrischen der Räume)."""
        self._ids.clear()

    async def items(self, rooms: list[Room]) -> dict[str, dict[str, Any]]:
        """entity_id der schedule-Entität -> Zeitplan (nur Räume mit Heizplan).

        Ist ``schedule/list`` nicht lesbar, gelten die zuletzt gelesenen Pläne weiter; ohne sie: leer."""
        jetzt = self.uhr()
        if self._items is None or self._stand is None or not self._stand <= jetzt < self._stand + PLAN_TTL:
            try:
                self._items = {str(i.get("id")): i for i in await self.client.schedule_list() if isinstance(i, dict)}
                self._stand = jetzt
                self._ids.clear()
            except HAError as err:
                if self._items is None:
                    _LOGGER.info("Heizpläne nicht lesbar, Plananwendung wartet: %s", err)
                    return {}
                _LOGGER.info("Heizpläne nicht lesbar, verwende die zuletzt gelesenen: %s", err)
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
    """Hintergrund-Task der Betriebsart generisch: Rück-Timer, Sommer-Abgleich und (mit ``plan_anwenden``)
    die Heizpläne der Räume im App-Modus ``plan``."""

    def __init__(self, adapter: GenerischAdapter) -> None:
        self.adapter = adapter
        self._task: asyncio.Task | None = None
        self._backoff: dict[str, datetime] = {}
        self._fehler: dict[str, int] = {}
        self.letzter_lauf: datetime | None = None

    # ------------------------------------------------------------- Hintergrund

    @property
    def laeuft(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, bereit: Callable[[], bool] | None = None) -> None:
        if not self.laeuft:
            _LOGGER.info(
                "Hintergrund der Betriebsart generisch gestartet (Heizpläne %s)",
                "werden angewendet" if self.adapter.plan_aktiv else "werden nicht angewendet, nur Rück-Timer",
            )
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

    def _pause_nach_fehler(self, raum: str, jetzt: datetime) -> timedelta:
        """Wartezeit nach dem n-ten Fehler in Folge: 5, 10, 20, 40, dann höchstens 60 Minuten."""
        n = self._fehler.get(raum, 0) + 1
        self._fehler[raum] = n
        pause = min(BACKOFF * (2 ** min(n - 1, 6)), BACKOFF_MAX)
        self._backoff[raum] = jetzt + pause
        return pause

    async def takt(self, erzwingen: bool = False) -> None:
        """Alle Räume einmal prüfen."""
        ad = self.adapter
        states = await ad.client.get_states()
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        rooms = await ad.raeume_aktuell(states)
        await ad.vorbereiten(rooms, by_id)
        jetzt = ad.uhr()
        async with ad.lock:
            await ad.zustand_abgleichen(rooms, by_id)
            for room in rooms:
                try:
                    await self.raum(room, by_id, jetzt, erzwingen)
                except HAError as err:
                    pause = self._pause_nach_fehler(room.raum, jetzt)
                    _LOGGER.warning(
                        "Plananwendung %s: %s (neuer Versuch in %s Minuten)", room.raum, err, int(pause.total_seconds() // 60)
                    )
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
            self._fehler.pop(room.raum, None)
        finally:
            if a != alt:
                await ad.app_speichern(room, a)

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
        # Abgelaufene Timer zuerst: zurück zum Plan-Soll bzw. zum vorherigen Wert/Preset
        for feld in ("boost_bis", "overlay_bis"):
            bis = zeit(a.get(feld))
            if bis is not None and bis <= jetzt:
                await ad.rueckkehr(room, a, cl, info, "Zeit abgelaufen")
                return
        cl = {**cl, "state": await ad.sommer_abgleich(room, a, cl, info)}
        if not ad.plan_aktiv or a["modus"] != "plan" or ad.pause or a.get("overlay_bis") or a.get("boost_bis"):
            return
        if cl.get("state") != "heat":  # nur im Zustand heat (nie einschalten, nie Geräteprogramm/Kühlen überschreiben)
            return
        if info is None or info.soll is None:
            return
        attrs = cl.get("attributes") or {}
        ist_soll = an.to_float(attrs.get("temperature"))
        tol = ad.toleranz(attrs)
        geschrieben = a.get("geschrieben")
        soll = ad.runden(info.soll, attrs)
        text = f"Heizplan {ad.fmt(soll)} °C" if info.im_block else f"Absenkung {ad.fmt(soll)} °C"
        if not erzwingen and geschrieben is not None and ist_soll is not None:
            if abs(ist_soll - geschrieben) <= tol:
                # Thermostat hat den geschriebenen Wert übernommen
                if a.get("gesehen") != ist_soll or a.get("nachgeschrieben"):
                    a.update(gesehen=ist_soll, nachgeschrieben=0)
            elif await self._abweichung(room, a, attrs, ist_soll, geschrieben, soll, tol, info, jetzt, by_id, text):
                return
        if soll > (ist_soll if ist_soll is not None else soll) and ad.fenster_offen(room, by_id):
            return  # bei offenem Fenster nicht erhöhen
        if ist_soll is not None and abs(ist_soll - soll) <= tol:
            if geschrieben is None or abs(geschrieben - soll) > TOLERANZ:
                a.update(geschrieben=soll, geschrieben_zeit=iso(jetzt), gesehen=ist_soll, nachgeschrieben=0)
            return
        if not erzwingen and geschrieben is not None and abs(geschrieben - soll) <= TOLERANZ:
            return
        await ad.soll_schreiben(room, a, soll, attrs, text)

    async def _abweichung(
        self,
        room: Room,
        a: dict[str, Any],
        attrs: dict[str, Any],
        ist_soll: float,
        geschrieben: float,
        soll: float,
        tol: float,
        info: PlanInfo,
        jetzt: datetime,
        by_id: dict[str, dict[str, Any]],
        text: str,
    ) -> bool:
        """Sollwert am Thermostat weicht vom geschriebenen ab. True = Raum ist damit erledigt."""
        ad = self.adapter
        preset = attrs.get("preset_mode")
        if ad.fenster_offen(room, by_id) or preset not in (None, "none", "None"):
            return True  # Fensterabsenkung oder Preset des Geräts: keine Handänderung, nicht dagegen schreiben
        gesehen = an.to_float(a.get("gesehen"))
        uebernommen = gesehen is not None and abs(gesehen - geschrieben) <= tol
        alter_wert = gesehen is not None and not uebernommen and abs(ist_soll - gesehen) <= tol
        if not alter_wert:
            seit = zeit(a.get("geschrieben_zeit"))
            if not uebernommen and seit is not None and jetzt - seit < KARENZ:
                return True  # Rückmeldung des Thermostats abwarten
            # Das Gerät hatte den Wert übernommen (oder zeigt einen dritten Wert): Handänderung bis zum Planwechsel
            a["overlay_bis"] = iso(info.wechsel) if info.wechsel else "dauerhaft"
            a["overlay_temp"] = ist_soll
            a["vor_temp"] = geschrieben
            a.update(geschrieben=ist_soll, gesehen=ist_soll, nachgeschrieben=0)
            bis = ad.zeit_text(info.wechsel)
            await ad.protokoll(
                room,
                f"Handänderung am Thermostat auf {ad.fmt(ist_soll)} °C erkannt, gilt {bis}",
                {"temperatur": ist_soll, "bis": a["overlay_bis"], "anlass": "handaenderung"},
            )
            return True
        if abs(geschrieben - soll) > TOLERANZ:
            return False  # neues Plan-Soll: normal schreiben
        seit = zeit(a.get("geschrieben_zeit"))
        if seit is not None and jetzt - seit < KARENZ:
            return True  # Rückmeldung des Thermostats abwarten
        stufe = int(a.get("nachgeschrieben") or 0)
        if stufe == 0:
            await ad.soll_schreiben(room, a, soll, attrs, f"{text} erneut geschrieben (vom Thermostat nicht übernommen)")
            a["nachgeschrieben"] = 1
        elif stufe == 1:
            a["nachgeschrieben"] = 2
            await ad.protokoll(
                room,
                f"Sollwert {ad.fmt(soll)} °C vom Thermostat nicht übernommen (zeigt {ad.fmt(ist_soll)} °C)",
                {"temperatur": soll, "thermostat": ist_soll, "anlass": "nicht_uebernommen"},
            )
        return True
