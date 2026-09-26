"""Klima-Coach: lokale Regel-Engine und KI-Analyse auf Anforderung."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from ..ha_client import HAError
from ..steuerung import json_fehler
from . import ki
from .engine import Regelwerk, auswerten, saison_aktiv
from .lage import LageDienst, lagebericht
from .store import CoachStore, jetzt_iso

__all__ = ["Coach", "CoachStore", "Regelwerk"]

_LOGGER = logging.getLogger(__name__)

TIPPS_TTL = 60
RUECKMELDUNGEN = ("ausblenden", "hilfreich", "erledigt")
ERLEDIGT_TAGE = 1  # „Erledigt“ blendet den Tipp bis zum nächsten Tag aus
TIPP_ID_RE = re.compile(r"^[a-z0-9_]{1,120}:[a-z0-9_]{1,64}$")
LAUF_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class Coach:
    """Verbindet Lage, Regelwerk, KI und Ablage (``ks`` = KlimaStudio)."""

    def __init__(self, ks: Any, store: CoachStore, wissen_pfad: Path | None = None) -> None:
        self.ks = ks
        self.store = store
        self.regelwerk = Regelwerk.laden(wissen_pfad)
        self.lage = LageDienst(ks)
        self._cache: tuple[float, dict[str, Any], list[dict[str, Any]]] | None = None
        self._cache_lock = asyncio.Lock()
        self._ki_lock = asyncio.Lock()

    def invalidieren(self) -> None:
        self._cache = None

    async def _auswertung(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with self._cache_lock:
            if self._cache and time.monotonic() - self._cache[0] < TIPPS_TTL:
                return self._cache[1], self._cache[2]
            lage = await self.lage.erstellen()
            tipps = auswerten(self.regelwerk, lage["global"], lage["raeume"])
            lage["extra"]["stand_ms"] = int(time.time() * 1000)
            self._cache = (time.monotonic(), lage, tipps)
            return lage, tipps

    async def _sichtbare_tipps(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lage, tipps = await self._auswertung()
        weg = await self.store.ausgeblendet()
        return lage, [t for t in tipps if t["id"] not in weg]

    # ------------------------------------------------------------- API

    async def uebersicht(self) -> dict[str, Any]:
        lage, tipps = await self._sichtbare_tipps()
        return {
            "tipps": tipps,
            "integration": lage["extra"]["empfehlungen"],
            "stand": lage["extra"]["stand_ms"],
            "wissen_version": self.regelwerk.version,
            "saison": "heizperiode" if saison_aktiv(lage["global"]) else "sommer",
        }

    async def rueckmeldung(self, body: dict[str, Any]) -> None:
        tipp_id = body.get("id")
        aktion = body.get("aktion")
        if not isinstance(tipp_id, str) or not TIPP_ID_RE.match(tipp_id):
            raise json_fehler(web.HTTPBadRequest, "Ungültige Tipp-ID.", "ungueltig")
        if aktion not in RUECKMELDUNGEN:
            raise json_fehler(web.HTTPBadRequest, "aktion muss ausblenden, hilfreich oder erledigt sein.", "ungueltig")
        bis: float | None = None
        if aktion == "ausblenden":
            tage = body.get("tage", 7)
            if isinstance(tage, bool) or not isinstance(tage, int) or not 1 <= tage <= 90:
                raise json_fehler(web.HTTPBadRequest, "tage muss eine ganze Zahl von 1 bis 90 sein.", "ungueltig")
            bis = time.time() + tage * 86400
        elif "tage" in body:
            raise json_fehler(web.HTTPBadRequest, "tage ist nur beim Ausblenden erlaubt.", "ungueltig")
        elif aktion == "erledigt":
            bis = time.time() + ERLEDIGT_TAGE * 86400
        await self.store.rueckmeldung(tipp_id, aktion, bis)

    async def lagebericht(self) -> dict[str, Any]:
        lage, tipps = await self._sichtbare_tipps()
        return lagebericht(lage, tipps, self.ks.opts.coach_anwesenheit)

    async def lagebericht_antwort(self) -> dict[str, Any]:
        bericht = await self.lagebericht()
        return {
            "entitaet": self.ks.opts.coach_ki_entitaet,
            "anwesenheit": self.ks.opts.coach_anwesenheit,
            "zeichen": len(json.dumps(bericht, ensure_ascii=False, separators=(",", ":"))),
            "lage": bericht,
        }

    async def ki_lauf(self) -> dict[str, Any]:
        if self._ki_lock.locked():
            raise json_fehler(web.HTTPConflict, "Eine KI-Analyse läuft bereits. Bitte warten Sie das Ergebnis ab.", "laeuft")
        async with self._ki_lock:
            eid = self.ks.opts.coach_ki_entitaet
            states = await self.ks.client.get_states()
            if not any(s.get("entity_id") == eid for s in states):
                raise json_fehler(
                    web.HTTPConflict,
                    f"Die KI-Entität {eid} ist nicht vorhanden. Richten Sie in Home Assistant eine KI-Aufgabe ein "
                    "(z. B. Anthropic oder OpenAI) und tragen Sie die Entität in den App-Optionen unter coach_ki_entitaet ein.",
                    "ki_fehlt",
                )
            bericht = await self.lagebericht()
            raeume = {r["raum"]: r["name"] for r in bericht["raeume"]}
            lauf: dict[str, Any] = {
                "id": uuid.uuid4().hex[:12],
                "erstellt": jetzt_iso(),
                "entitaet": eid,
                "dauer_s": None,
                "zusammenfassung": None,
                "tipps": [],
                "roh": None,
                "fehler": None,
            }
            start = time.monotonic()
            try:
                resp = await self.ks.client.call_service(
                    "ai_task",
                    "generate_data",
                    {"task_name": "klima_coach", "entity_id": eid, "instructions": ki.prompt(bericht)},
                    return_response=True,
                    timeout=ki.KI_TIMEOUT,
                )
            except HAError as err:
                lauf["dauer_s"] = round(time.monotonic() - start, 1)
                lauf["fehler"] = ki.fehlertext(err)[:500]
                await self.store.ki_lauf_speichern(lauf, bericht)
                raise HAError(lauf["fehler"], err.code, err.meldung) from err
            lauf["dauer_s"] = round(time.monotonic() - start, 1)
            data = ki.antwort_daten(resp)
            try:
                ergebnis = ki.bereinige(ki.parse_antwort(data), raeume, lauf["id"])
                lauf.update(ergebnis)
            except ValueError as err:
                lauf["fehler"] = str(err)
                roh = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
                lauf["roh"] = ki.STEUERZEICHEN_RE.sub("", roh or "")[: ki.MAX_ROH]
            await self.store.ki_lauf_speichern(lauf, bericht)
            await self.store.ereignis(
                "coach_ki", None, "KI-Analyse " + ("fehlgeschlagen" if lauf["fehler"] else "erstellt"), {"id": lauf["id"]}
            )
            _LOGGER.info("KI-Analyse %s über %s in %s s", lauf["id"], eid, lauf["dauer_s"])
            return lauf

    async def ki_laeufe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"],
                "erstellt": r["erstellt"],
                "entitaet": r["entitaet"],
                "zusammenfassung": r["zusammenfassung"],
                "anzahl_tipps": len(r["tipps"]),
                "fehler": r["fehler"],
            }
            for r in await self.store.ki_laeufe(20)
        ]

    async def ki_lauf_lesen(self, lauf_id: str) -> dict[str, Any]:
        if not LAUF_ID_RE.match(lauf_id):
            raise json_fehler(web.HTTPNotFound, "Analyse nicht gefunden.", "unbekannt")
        lauf = await self.store.ki_lauf(lauf_id)
        if lauf is None:
            raise json_fehler(web.HTTPNotFound, "Analyse nicht gefunden.", "unbekannt")
        return lauf
