"""Betriebsart ``pm_networks``: Integration PM Klima.

Verhalten wie bis 1.1.1: Räume per Präfix (``climate.pm_*``), Befehle über ``pm_heizung.*`` bzw.
``climate.*`` auf diesen Entitäten, Heizperiode über die Freigabe-Entität der Integration.
Die Umsetzung liegt unverändert in ``config.discover_rooms`` und ``steuerung``; dieser Adapter
bündelt sie.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from .. import steuerung as st
from ..coach import ki
from ..config import Room, discover_rooms
from . import FAEHIGKEITEN, Adapter


class PmKlimaAdapter(Adapter):
    betriebsart = "pm_networks"

    # ------------------------------------------------------------- Räume

    def raeume_erkennen(self, states: list[dict[str, Any]], registries: dict[str, list[dict[str, Any]]]) -> list[Room]:
        return discover_rooms(states, self.opts)

    def raum_zustand(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return st.raum_zustand(room, by_id, self.opts)

    def raum_faehigkeiten(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cl = by_id.get(room.climate or "") or {}
        return {
            "steuerbar": st.steuerbar(room, self.opts) and bool(cl) and cl.get("state") not in (None, "unavailable", "unknown"),
            "modi": list(st.MODI),
            "overlay": True,
            "boost": True,
        }

    # ------------------------------------------------------------- Steuerung

    def pruefe_steuerbar(self, room: Room) -> None:
        if not st.steuerbar(room, self.opts):
            raise st.json_fehler(web.HTTPConflict, f"{room.name} wird nicht von PM Klima gesteuert.", "nicht_gesteuert")

    async def aktion(self, room: Room, body: dict[str, Any]) -> dict[str, Any]:
        by_id = {s["entity_id"]: s for s in await self.client.get_states() if "entity_id" in s}
        cl = by_id.get(room.climate or "")
        if cl is None or cl.get("state") in (None, "unavailable", "unknown"):
            raise st.json_fehler(web.HTTPConflict, f"{room.climate} ist derzeit nicht verfügbar.", "nicht_verfuegbar")
        befehl = st.pruefe_befehl(body, cl.get("attributes") or {})
        st.pruefe_modus(befehl, cl.get("state"))
        await self.client.call_service(befehl["domain"], befehl["service"], {"entity_id": room.climate, **befehl["daten"]})
        return befehl

    # ------------------------------------------------------------- Integration / Fähigkeiten

    def faehigkeiten(self) -> dict[str, bool]:
        werte = {
            "freigabe": True,
            "overlay_integration": True,
            "plan_anwendung": False,  # PM Klima wendet die Heizpläne selbst an
            "empfehlungen_integration": True,
            "schimmel": True,
            "luftqualitaet": True,
        }
        return {k: werte[k] for k in FAEHIGKEITEN}

    def integration_status(self, by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        return st.integration_status(by_id)

    # ------------------------------------------------------------- Heizperiode

    async def heizperiode_anwenden(self, aktiv: bool, entitaet: str) -> None:
        """Freigabe-Entität schalten; die Integration sperrt bzw. gibt die Räume frei."""
        await self.client.call_service("input_boolean", "turn_on" if aktiv else "turn_off", {"entity_id": entitaet})

    def freigabe_verknuepft(self, by_id: dict[str, dict[str, Any]], zustand: str | None) -> bool | None:
        sperre = by_id.get(st.SPERRE_SENSOR)
        if zustand == "off" and sperre is not None:
            return (sperre.get("attributes") or {}).get("sperre_grund") == "freigabe_aus"
        return None

    # ------------------------------------------------------------- Coach

    def empfehlungen(self, by_id: dict[str, dict[str, Any]]) -> list[Any]:
        emp = by_id.get(self.opts.empfehlung) or {}
        liste = (emp.get("attributes") or {}).get("liste")
        return liste if isinstance(liste, list) else []

    def ki_prompt(self, bericht: dict[str, Any]) -> str:
        return ki.prompt(bericht)
