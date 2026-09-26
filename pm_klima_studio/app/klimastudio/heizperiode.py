"""Heizperiode / Sommerautomatik.

Die App schaltet ausschließlich eine Freigabe-Entität (Standard
``input_boolean.pm_heizperiode``), die in der Integration PM Klima als
``freigabe_entitaet`` eingetragen ist. on = Heizperiode, off = Sommer.
Thermostate werden nie direkt angesprochen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from . import analytics as an
from .coach.store import CoachStore, jetzt_iso
from .config import Options
from .ha_client import HAClient, HAError
from .steuerung import SPERRE_SENSOR, json_fehler

_LOGGER = logging.getLogger(__name__)

EINSTELLUNG = "heizperiode"
ZUSTAND = "heizperiode_zustand"
MODI = ("automatik", "heizperiode", "sommer")
STANDARD: dict[str, Any] = {"modus": "automatik", "heizgrenze": 13.0, "hysterese": 2.0, "tage_start": 2, "tage_ende": 3}
GRENZEN: dict[str, tuple[float, float]] = {"heizgrenze": (5, 20), "hysterese": (0, 5), "tage_start": (1, 7), "tage_ende": (1, 7)}
TAGE = 7
MIN_STUNDEN = 12  # Mindestanzahl Stundenwerte für ein gültiges Tagesmittel
INTERVALL = 3600
MITTEL_TTL = 900
BEREIT_WARTEN = 300
HINWEIS_EINRICHTEN = (
    "Die Entität {entitaet} wurde angelegt. Tragen Sie sie einmalig in der Integration PM Klima als Freigabe ein: "
    "Einstellungen → Geräte & Dienste → PM Klima → Konfigurieren → Schritt „Sperre“ → freigabe_entitaet."
)


# ------------------------------------------------------------------ Einstellungen


def pruefe_einstellungen(body: dict[str, Any], alt: dict[str, Any]) -> dict[str, Any]:
    """Teilmenge validieren und mit den bisherigen Einstellungen zusammenführen."""
    neu = dict(alt)
    fehler: list[str] = []
    unbekannt = set(body) - set(STANDARD)
    if unbekannt:
        fehler.append(f"Unbekannte Felder: {', '.join(sorted(unbekannt))}.")
    if "modus" in body:
        if body["modus"] not in MODI:
            fehler.append("modus muss automatik, heizperiode oder sommer sein.")
        else:
            neu["modus"] = body["modus"]
    for key, (lo, hi) in GRENZEN.items():
        if key not in body:
            continue
        v = body[key]
        ganzzahl = key.startswith("tage_")
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            fehler.append(f"{key} muss eine Zahl sein.")
            continue
        if ganzzahl and not float(v).is_integer():
            fehler.append(f"{key} muss eine ganze Zahl sein.")
            continue
        if not lo <= v <= hi:
            fehler.append(f"{key} muss zwischen {lo:g} und {hi:g} liegen.")
            continue
        neu[key] = int(v) if ganzzahl else round(float(v), 1)
    if fehler:
        raise json_fehler(web.HTTPBadRequest, " ".join(fehler), "ungueltig")
    return neu


def _normalisiere(raw: Any) -> dict[str, Any]:
    out = dict(STANDARD)
    if isinstance(raw, dict):
        try:
            out = pruefe_einstellungen({k: v for k, v in raw.items() if k in STANDARD}, out)
        except web.HTTPException:
            _LOGGER.warning("Gespeicherte Heizperioden-Einstellungen ungültig, verwende Standard")
            out = dict(STANDARD)
    return out


# ------------------------------------------------------------------ Entscheidung


def _fmt(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def entscheide(
    tagesmittel: list[float | None],
    einst: dict[str, Any],
    bisher: bool | None,
    zustand: bool | None,
) -> tuple[bool | None, str]:
    """Automatik-Entscheidung nach Heizgrenztemperatur (Tagesmittel, älteste zuerst).

    Rückgabe ``(aktiv, grund)``; ``aktiv`` None = zu wenige Daten, nichts ändern.
    """
    grenze = float(einst["heizgrenze"])
    ende = grenze + float(einst["hysterese"])
    n_start, n_ende = int(einst["tage_start"]), int(einst["tage_ende"])

    def letzte(n: int) -> list[float] | None:
        werte = tagesmittel[-n:] if len(tagesmittel) >= n else []
        if len(werte) < n or any(v is None for v in werte):
            return None
        return [float(v) for v in werte if v is not None]

    start_w = letzte(n_start)
    ende_w = letzte(n_ende)
    if start_w is None and ende_w is None:
        return None, "Zu wenige Daten: Für die letzten Tage fehlen Tagesmittel der Außentemperatur."
    if start_w is not None and all(v < grenze for v in start_w):
        return True, f"Tagesmittel der letzten {n_start} Tage unter der Heizgrenze von {_fmt(grenze)} °C."
    if ende_w is not None and all(v >= ende for v in ende_w):
        return False, f"Tagesmittel der letzten {n_ende} Tage mindestens {_fmt(ende)} °C (Heizgrenze plus Hysterese)."
    if bisher is not None:
        return bisher, "Tagesmittel im Übergangsbereich, bisheriger Zustand bleibt."
    if zustand is not None:
        return zustand, "Tagesmittel im Übergangsbereich, aktueller Zustand der Freigabe wird übernommen."
    gestern = next((v for v in reversed(tagesmittel) if v is not None), None)
    if gestern is None:  # pragma: no cover - durch die Prüfungen oben ausgeschlossen
        return None, "Zu wenige Daten."
    mitte = grenze + float(einst["hysterese"]) / 2
    if gestern < mitte:
        return True, f"Letztes Tagesmittel unter {_fmt(mitte)} °C, Heizperiode angenommen."
    return False, f"Letztes Tagesmittel mindestens {_fmt(mitte)} °C, Sommer angenommen."


def tagesmittel_aus_stunden(rows: list[dict[str, Any]], tage: list[date], tz: ZoneInfo) -> list[float | None]:
    """Stündliche Langzeitstatistik (``mean``) je lokalem Kalendertag mitteln."""
    werte: dict[date, list[float]] = {}
    for ts, v in an.stats_to_series(rows, "mean"):
        if v is None:
            continue
        werte.setdefault(datetime.fromtimestamp(ts, tz).date(), []).append(v)
    out: list[float | None] = []
    for tag in tage:
        vals = werte.get(tag) or []
        out.append(round(sum(vals) / len(vals), 1) if len(vals) >= MIN_STUNDEN else None)
    return out


def tagesmittel_aus_historie(series: an.Series, tage: list[date], tz: ZoneInfo) -> list[float | None]:
    """Zeitgewichtetes Tagesmittel aus der Historie (Treppenfunktion)."""
    out: list[float | None] = []
    for tag in tage:
        s = datetime(tag.year, tag.month, tag.day, tzinfo=tz).timestamp()
        e = datetime.combine(tag + timedelta(days=1), datetime.min.time(), tzinfo=tz).timestamp()
        pts = [(s, an.value_at(series, s))] + [(ts, v) for ts, v in series if s < ts < e]
        summe = dauer = 0.0
        for i, (ts, v) in enumerate(pts):
            t_end = pts[i + 1][0] if i + 1 < len(pts) else e
            if v is not None:
                summe += float(v) * (t_end - ts)
                dauer += t_end - ts
        out.append(round(summe / dauer, 1) if dauer >= MIN_STUNDEN * 3600 else None)
    return out


# ------------------------------------------------------------------ Dienst


def _an_aus(state: Any) -> bool | None:
    if state == "on":
        return True
    if state == "off":
        return False
    return None


class Heizperiode:
    """Bewertet die Heizperiode und schaltet die Freigabe-Entität."""

    def __init__(
        self,
        client: HAClient,
        opts: Options,
        store: CoachStore,
        tz: Callable[[], ZoneInfo],
        bereit: Callable[[], bool] | None = None,
    ) -> None:
        self.client = client
        self.opts = opts
        self.store = store
        self._tz = tz
        self._bereit = bereit or (lambda: True)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._mittel: tuple[float, date, list[date], list[float | None]] | None = None
        self.naechste: datetime | None = None

    @property
    def entitaet(self) -> str:
        return self.opts.heizperiode_entitaet

    @property
    def schreibbar(self) -> bool:
        return self.entitaet.startswith("input_boolean.")

    # ------------------------------------------------------------- Hintergrund

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        gewartet = 0.0
        while not self._bereit() and gewartet < BEREIT_WARTEN:
            await asyncio.sleep(5)
            gewartet += 5
        while True:
            try:
                await self.pruefen()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Prüfung der Heizperiode fehlgeschlagen")
            self.naechste = datetime.now(UTC) + timedelta(seconds=INTERVALL)
            await asyncio.sleep(INTERVALL)

    # ------------------------------------------------------------- Daten

    async def einstellungen(self) -> dict[str, Any]:
        return _normalisiere(await self.store.einstellung(EINSTELLUNG))

    async def _zustand_gespeichert(self) -> dict[str, Any]:
        raw = await self.store.einstellung(ZUSTAND)
        return raw if isinstance(raw, dict) else {}

    def _tage(self) -> list[date]:
        heute = datetime.now(self._tz()).date()
        return [heute - timedelta(days=i) for i in range(TAGE, 0, -1)]

    async def tagesmittel(self) -> tuple[list[date], list[float | None]]:
        """Tagesmittel der letzten 7 abgeschlossenen Tage (älteste zuerst)."""
        tz = self._tz()
        tage = self._tage()
        if self._mittel and self._mittel[1] == tage[-1] and time.monotonic() - self._mittel[0] < MITTEL_TTL:
            return self._mittel[2], self._mittel[3]
        start = datetime(tage[0].year, tage[0].month, tage[0].day, tzinfo=tz)
        ende = datetime.combine(tage[-1] + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        eid = self.opts.aussentemperatur
        werte: list[float | None] = [None] * len(tage)
        try:
            raw = await self.client.statistics_during_period([eid], start, ende, "hour", ["mean"])
            werte = tagesmittel_aus_stunden(raw.get(eid) or [], tage, tz)
        except HAError as err:
            _LOGGER.info("Langzeitstatistik für %s nicht verfügbar: %s", eid, err)
        if all(v is None for v in werte):
            try:
                raw = await self.client.history_during_period(
                    [eid], start, ende, minimal_response=True, no_attributes=True, significant_changes_only=False
                )
                series = an.numeric_series(an.parse_history_rows(raw.get(eid) or []))
                werte = tagesmittel_aus_historie(series, tage, tz)
            except HAError as err:
                _LOGGER.info("Historie für %s nicht verfügbar: %s", eid, err)
        self._mittel = (time.monotonic(), tage[-1], tage, werte)
        return tage, werte

    # ------------------------------------------------------------- Schalten

    async def _schalten(self, an_: bool, grund: str, modus: str, entitaet: str | None = None) -> None:
        eid = entitaet or self.entitaet
        await self.client.call_service("input_boolean", "turn_on" if an_ else "turn_off", {"entity_id": eid})
        text = f"Heizperiode {'eingeschaltet' if an_ else 'ausgeschaltet (Sommer)'}: {grund}"
        _LOGGER.info("%s (%s)", text, eid)
        await self.store.ereignis("heizperiode", None, text, {"aktiv": an_, "modus": modus, "entitaet": eid, "grund": grund})
        zustand = await self._zustand_gespeichert()
        zustand["letzte_aenderung"] = jetzt_iso()
        await self.store.einstellung_setzen(ZUSTAND, zustand)

    async def pruefen(self, erzwingen: bool = False, states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Auswerten und die Freigabe nur bei geänderter Entscheidung schalten.

        ``erzwingen`` (Moduswechsel): Entscheidung auch ohne Änderung anwenden.
        """
        async with self._lock:
            einst = await self.einstellungen()
            by_id = {s["entity_id"]: s for s in states or await self.client.get_states()}
            st = by_id.get(self.entitaet)
            ist = _an_aus(st.get("state")) if st else None
            gespeichert = await self._zustand_gespeichert()
            bisher = gespeichert.get("entscheidung")
            bisher = bisher if isinstance(bisher, bool) else None
            aktiv, grund = await self._entscheidung(einst, bisher, ist)
            if aktiv is None or st is None:
                if st is None:
                    grund = f"{grund} Die Freigabe-Entität {self.entitaet} fehlt."
                return {"aktiv": aktiv, "grund": grund}
            geaendert = aktiv != bisher
            if (geaendert or erzwingen) and ist is not aktiv:
                if self.schreibbar:
                    await self._schalten(aktiv, grund, einst["modus"])
                else:
                    _LOGGER.info("%s ist kein input_boolean, Heizperiode wird nur gelesen", self.entitaet)
            if geaendert:
                gespeichert = await self._zustand_gespeichert()
                gespeichert["entscheidung"] = aktiv
                await self.store.einstellung_setzen(ZUSTAND, gespeichert)
            return {"aktiv": aktiv, "grund": grund}

    async def _entscheidung(self, einst: dict[str, Any], bisher: bool | None, ist: bool | None) -> tuple[bool | None, str]:
        if einst["modus"] == "heizperiode":
            return True, "Manuell auf Heizperiode gestellt."
        if einst["modus"] == "sommer":
            return False, "Manuell auf Sommer gestellt."
        _, werte = await self.tagesmittel()
        return entscheide(werte, einst, bisher, ist)

    # ------------------------------------------------------------- API

    async def status(self, states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        einst = await self.einstellungen()
        by_id = {s["entity_id"]: s for s in states or await self.client.get_states()}
        st = by_id.get(self.entitaet)
        zustand = st.get("state") if st else None
        zustand = zustand if zustand in ("on", "off") else None
        aktiv = _an_aus(zustand)
        gespeichert = await self._zustand_gespeichert()
        bisher = gespeichert.get("entscheidung")
        bisher = bisher if isinstance(bisher, bool) else None
        tage, werte = await self.tagesmittel()
        e_aktiv, grund = await self._entscheidung(einst, bisher, aktiv)
        if st is None:
            grund = f"{grund} Die Freigabe-Entität {self.entitaet} fehlt."
        verknuepft: bool | None = None
        sperre = by_id.get(SPERRE_SENSOR)
        if zustand == "off" and sperre is not None:
            verknuepft = (sperre.get("attributes") or {}).get("sperre_grund") == "freigabe_aus"
        return {
            **einst,
            "entitaet": self.entitaet,
            "vorhanden": st is not None,
            "zustand": zustand,
            "aktiv": aktiv,
            "verknuepft": verknuepft,
            "tagesmittel": [{"datum": t.isoformat(), "mittel": v} for t, v in zip(tage, werte, strict=True)],
            "entscheidung": {"aktiv": e_aktiv, "grund": grund},
            "letzte_aenderung": gespeichert.get("letzte_aenderung"),
            "naechste_pruefung": self.naechste.isoformat(timespec="seconds") if self.naechste else None,
            "aussen_aktuell": an.to_float((by_id.get(self.opts.aussentemperatur) or {}).get("state")),
        }

    async def einstellen(self, body: dict[str, Any]) -> dict[str, Any]:
        alt = await self.einstellungen()
        neu = pruefe_einstellungen(body, alt)
        if neu["modus"] in ("heizperiode", "sommer") and not self.schreibbar:
            raise json_fehler(
                web.HTTPConflict,
                f"{self.entitaet} ist kein input_boolean und kann von Klima Studio nicht geschaltet werden.",
                "nur_lesen",
            )
        await self.store.einstellung_setzen(EINSTELLUNG, neu)
        if neu != alt:
            await self.store.ereignis("heizperiode", None, "Einstellungen der Heizperiode geändert", neu)
        await self.pruefen(erzwingen=neu["modus"] != alt["modus"])
        return await self.status()

    async def einrichten(self) -> dict[str, Any]:
        states = await self.client.get_states()
        if any(s.get("entity_id") == self.entitaet for s in states):
            raise json_fehler(web.HTTPConflict, f"{self.entitaet} ist bereits vorhanden.", "vorhanden")
        if not self.schreibbar:
            raise json_fehler(
                web.HTTPConflict, f"{self.entitaet} ist kein input_boolean und kann nicht angelegt werden.", "nur_lesen"
            )
        object_id = self.entitaet.split(".", 1)[1]
        name = "PM Heizperiode" if object_id == "pm_heizperiode" else object_id.replace("_", " ").title()
        result = await self.client.input_boolean_create(name, "mdi:radiator")
        neu = f"input_boolean.{result.get('id')}" if isinstance(result, dict) and result.get("id") else self.entitaet
        if neu != self.entitaet:
            _LOGGER.warning("Angelegt wurde %s statt %s", neu, self.entitaet)
        einst = await self.einstellungen()
        aktiv, grund = await self._entscheidung(einst, None, None)
        if aktiv is None:
            aktiv, grund = True, "Neu angelegt, noch keine Entscheidung möglich: Heizperiode."
        await self._schalten(aktiv, grund, einst["modus"], entitaet=neu)
        async with self._lock:
            gespeichert = await self._zustand_gespeichert()
            gespeichert["entscheidung"] = aktiv
            await self.store.einstellung_setzen(ZUSTAND, gespeichert)
        out = await self.status()
        out["hinweis"] = HINWEIS_EINRICHTEN.format(entitaet=neu)
        return out
