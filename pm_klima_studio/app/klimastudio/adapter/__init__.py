"""Adapter je Betriebsart.

``pm_networks``: Die Integration PM Klima steuert (Dienste ``pm_heizung.*``, Entitäten
``climate.pm_*``, Freigabe der Heizperiode). ``generisch``: beliebige Standard-climate-Entitäten,
Räume aus den Bereichen von Home Assistant.

Server, Heizperiode und Coach sprechen ausschließlich über diese Schnittstelle mit dem
betriebsartspezifischen Teil.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import ZoneInfo

from aiohttp import web

from ..config import Options, Room
from ..ha_client import HAClient, HAError
from ..steuerung import json_fehler

if TYPE_CHECKING:
    from ..coach.store import CoachStore
    from ..plananwendung import Plananwendung

_LOGGER = logging.getLogger(__name__)

PM_DOMAIN = "pm_heizung"
ERKENNUNG_TTL = 300  # wie ROOM_TTL im Server
UNBEKANNT_TTL = 60  # unbekanntes Ergebnis (Fehler, Zeitüberschreitung) so lange merken
ZEITLIMIT = 5.0  # Sekunden für die gesamte Erkennung

# Fähigkeiten für /api/info (Oberfläche blendet danach ein und aus)
FAEHIGKEITEN = ("freigabe", "overlay_integration", "plan_anwendung", "empfehlungen_integration", "schimmel", "luftqualitaet")

HINWEIS_PM_FEHLT = (
    "Die Integration PM Klima ist in Home Assistant nicht geladen. Ohne PM Klima stehen Steuerung, Freigabe "
    "der Heizperiode und die Hinweise der Integration nicht zur Verfügung. Wenn Sie PM Klima nicht verwenden, "
    "stellen Sie in den App-Optionen die Betriebsart generisch ein."
)
HINWEIS_PM_VORHANDEN = (
    "Die Integration PM Klima ist in Home Assistant geladen. Mit der Betriebsart pm_networks nutzt Klima Studio "
    "ihre Funktionen (Overlay, Boost, Freigabe der Heizperiode, Hinweise der Integration)."
)


class Adapter(ABC):
    """Schnittstelle einer Betriebsart."""

    betriebsart: ClassVar[str]
    #: Raumerkennung braucht Bereichs-/Entitäts-/Geräteregistrierung (WebSocket)
    braucht_registries: ClassVar[bool] = False
    #: Heizperiode wirkt (Einstellen, Einrichten, Hintergrundprüfung schaltet)
    heizperiode_wirksam: ClassVar[bool] = True
    #: Heizperiode als interner Zustand der App (keine Freigabe-Entität nötig)
    heizperiode_intern: ClassVar[bool] = False

    def __init__(self, opts: Options, client: HAClient, store: CoachStore | None = None) -> None:
        self.opts = opts
        self.client = client
        self.store = store
        #: Uhr (UTC, zeitzonenbehaftet) und Zeitzone von HA; in Tests austauschbar
        self.uhr: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.tz: Callable[[], ZoneInfo] = lambda: ZoneInfo("Europe/Berlin")
        #: Räume der App (vom Server gesetzt); sonst werden sie aus den Zuständen ermittelt
        self.raeume_quelle: Callable[[], Awaitable[list[Room]]] | None = None
        #: Hintergrund-Task, der die Heizpläne anwendet (nur generisch mit ``plan_anwenden``)
        self.plananwendung: Plananwendung | None = None

    # ------------------------------------------------------------- Räume

    async def registries_laden(self) -> dict[str, list[dict[str, Any]]]:
        """Bereiche, Entitäten und Geräte aus der Registrierung; leer, wenn nicht benötigt."""
        return {"areas": [], "entities": [], "devices": []}

    async def raeume(self, states: list[dict[str, Any]]) -> list[Room]:
        registries = await self.registries_laden() if self.braucht_registries else {}
        return self.raeume_erkennen(states, registries)

    @abstractmethod
    def raeume_erkennen(self, states: list[dict[str, Any]], registries: dict[str, list[dict[str, Any]]]) -> list[Room]:
        """Räume aus Zuständen (und ggf. Registrierungen) ableiten, Overrides angewendet."""

    @abstractmethod
    def raum_zustand(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Zustand eines Raums wie in GET /api/steuerung."""

    @abstractmethod
    def raum_faehigkeiten(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Steuerbarkeit und Möglichkeiten des Raumthermostats."""

    async def vorbereiten(self, rooms: list[Room], by_id: dict[str, dict[str, Any]]) -> None:
        """Vor ``raum_zustand`` für mehrere Räume: Daten auffrischen (z. B. Heizpläne)."""
        return None

    def plan_geaendert(self) -> None:
        """Ein Heizplan wurde über Klima Studio geändert."""
        return None

    # ------------------------------------------------------------- Steuerung

    @abstractmethod
    def pruefe_steuerbar(self, room: Room) -> None:
        """Vor dem Lesen des Befehls: HTTP 409, wenn der Raum in dieser Betriebsart nicht gesteuert wird."""

    @abstractmethod
    async def aktion(self, room: Room, body: dict[str, Any]) -> dict[str, Any]:
        """Befehl prüfen und ausführen. Rückgabe ``{"aktion", "domain", "service", "daten", "text"}`` für das Protokoll."""

    # ------------------------------------------------------------- Integration / Fähigkeiten

    @abstractmethod
    def faehigkeiten(self) -> dict[str, bool]:
        """Fähigkeiten dieser Betriebsart (Schlüssel ``FAEHIGKEITEN``)."""

    def integration_status(self, by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Status der Heizungsintegration; None ohne Integration."""
        return None

    # ------------------------------------------------------------- Heizperiode

    @abstractmethod
    async def heizperiode_anwenden(self, aktiv: bool, entitaet: str) -> None:
        """Wirkung der Heizperiode (on) bzw. des Sommers (off) auslösen.

        Interner Zustand (``heizperiode_intern``) nimmt zusätzlich ``erstentscheid`` an: allererste
        Entscheidung der Automatik, dabei nur den Zustand merken."""

    def freigabe_verknuepft(self, by_id: dict[str, dict[str, Any]], zustand: str | None) -> bool | None:
        """Ob die Freigabe-Entität mit der Integration verknüpft ist (None = unbekannt)."""
        return None

    def heizperiode_ist(self) -> bool | None:
        """Interner Zustand (``heizperiode_intern``): True = Heizperiode, False = Sommer."""
        return None

    async def heizperiode_spiegeln(self, aktiv: bool, entitaet: str, by_id: dict[str, dict[str, Any]]) -> None:
        """Interner Zustand: vorhandene Spiegel-Entität (input_boolean) nachführen."""
        return None

    def heizperiode_pruefen_schreiben(self) -> None:
        """HTTP 409, wenn die Heizperiode in dieser Betriebsart (noch) nicht geschaltet werden kann."""
        if not self.heizperiode_wirksam:
            raise nicht_verfuegbar()

    # ------------------------------------------------------------- Coach

    def lage_integration(self, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Vollständiger Integrationsstatus für die Coach-Lage (``extra.integration``)."""
        return self.integration_status(by_id) or {}

    def empfehlungen(self, by_id: dict[str, dict[str, Any]]) -> list[Any]:
        """Hinweise der Integration (Liste) für Coach und Lagebericht."""
        return []

    @abstractmethod
    def ki_prompt(self, bericht: dict[str, Any]) -> str:
        """Anweisung an die KI inklusive Lagebericht."""


def nicht_verfuegbar() -> web.HTTPException:
    return json_fehler(web.HTTPConflict, "Im Modus Generisch noch nicht verfügbar.", "nicht_verfuegbar")


def adapter_fuer(opts: Options, client: HAClient, store: CoachStore | None = None) -> Adapter:
    """Fabrik: Adapter zur Option ``betriebsart``. ``store`` hält den App-Zustand (nur generisch)."""
    if opts.betriebsart == "generisch":
        from .generisch import GenerischAdapter

        return GenerischAdapter(opts, client, store)
    from .pm_klima import PmKlimaAdapter

    return PmKlimaAdapter(opts, client, store)


# ------------------------------------------------------------------ Erkennung PM Klima


async def pm_klima_geladen(client: HAClient) -> bool | None:
    """Ist die Integration PM Klima (Domäne ``pm_heizung``) geladen?

    ``config_entries/get`` → mindestens ein Eintrag im Zustand ``loaded``. Scheitert das (z. B. fehlende
    Berechtigung), gilt als Ersatz: ``get_services`` enthält ``pm_heizung``. None, wenn beides scheitert.
    """
    try:
        eintraege = await client.config_entries_get(PM_DOMAIN)
        return any(e.get("domain", PM_DOMAIN) == PM_DOMAIN and e.get("state") == "loaded" for e in eintraege)
    except HAError as err:
        _LOGGER.debug("config_entries/get nicht möglich (%s), prüfe Dienste", err)
    try:
        return PM_DOMAIN in await client.get_services()
    except HAError as err:
        _LOGGER.info("PM Klima nicht erkennbar: %s", err)
        return None


class PmKlimaErkennung:
    """Gecachtes Ergebnis von ``pm_klima_geladen``: bekannt ``ERKENNUNG_TTL``, unbekannt (None) ``ttl_unbekannt``.

    Die Erkennung dauert höchstens ``zeitlimit`` Sekunden (danach None), damit ``/api/info`` nie lange
    blockiert; gleichzeitige Aufrufe teilen sich eine Abfrage."""

    def __init__(
        self, client: HAClient, ttl: float = ERKENNUNG_TTL, ttl_unbekannt: float = UNBEKANNT_TTL, zeitlimit: float = ZEITLIMIT
    ) -> None:
        self.client = client
        self.ttl = ttl
        self.ttl_unbekannt = ttl_unbekannt
        self.zeitlimit = zeitlimit
        self._wert: tuple[float, bool | None] | None = None
        self._lock = asyncio.Lock()

    def _gueltig(self) -> bool:
        if self._wert is None:
            return False
        ttl = self.ttl if self._wert[1] is not None else self.ttl_unbekannt
        return time.monotonic() - self._wert[0] < ttl

    async def geladen(self, refresh: bool = False) -> bool | None:
        if not refresh and self._gueltig():
            return self._wert[1] if self._wert else None
        async with self._lock:
            if not refresh and self._gueltig():
                return self._wert[1] if self._wert else None
            try:
                wert = await asyncio.wait_for(pm_klima_geladen(self.client), self.zeitlimit)
            except TimeoutError:
                _LOGGER.info("PM Klima nicht erkennbar: keine Antwort innerhalb von %s s", self.zeitlimit)
                wert = None
            self._wert = (time.monotonic(), wert)
            return wert


def hinweis_betriebsart(betriebsart: str, geladen: bool | None) -> str | None:
    """Hinweis für die Oberfläche; die Betriebsart wird nie automatisch umgeschaltet."""
    if betriebsart == "pm_networks" and geladen is False:
        return HINWEIS_PM_FEHLT
    if betriebsart == "generisch" and geladen is True:
        return HINWEIS_PM_VORHANDEN
    return None


__all__ = [
    "FAEHIGKEITEN",
    "HINWEIS_PM_FEHLT",
    "HINWEIS_PM_VORHANDEN",
    "Adapter",
    "PmKlimaErkennung",
    "adapter_fuer",
    "hinweis_betriebsart",
    "nicht_verfuegbar",
    "pm_klima_geladen",
]
