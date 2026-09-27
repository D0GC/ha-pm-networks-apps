"""Betriebsart ``generisch``: Standard-climate-Entitäten ohne die Integration PM Klima.

Räume ergeben sich aus den Bereichen (Areas) von Home Assistant: je Bereich ein Raum mit dem
ersten Thermostat (sortiert) als Raumthermostat, Sensoren des Bereichs (zuerst die im Bereich
eingetragenen Temperatur-/Feuchtesensoren, dann nach ``device_class``). Thermostate ohne Bereich
bilden einen eigenen Raum (Kürzel aus der object_id). Entitäten von PM Klima werden nie als
Thermostat verwendet; Better-Thermostat-Hüllen haben Vorrang vor den Thermostaten ihres Bereichs.

Mehrere Thermostate in einem Bereich: gesteuert wird nur das Raumthermostat; die weiteren stehen
in ``climate_weitere`` und als Hinweis im Raumzustand (bewusst kein Gruppenschalten: jedes Gerät hat
eigene Modi, Grenzen und Rückmeldungen; Handänderung und Übernahme wären nicht eindeutig).

Steuerung über Standard-Dienste ``climate.*``. Die App führt je Raum einen eigenen Zustand
(SQLite-Tabelle ``app_raeume``): Modus ``plan`` (Klima Studio wendet den Heizplan an, siehe
``plananwendung``), ``hand`` (Standard für neue Räume: die App schreibt nichts) oder ``aus``,
Rück-Timer für Overlay und Boost, zuletzt geschriebener und übernommener Sollwert, gemerkte
hvac-Modi und die climate-Entität, zu der der Zustand gehört. Die Heizperiode ist ein interner
Zustand (Sommer-Pause).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .. import analytics as an
from .. import steuerung as st
from ..coach import ki
from ..coach.store import APP_RAUM_FELDER
from ..config import Options, Room, _name_for, _pick_by_value, overrides_anwenden
from ..ha_client import HAClient, HAError
from ..plananwendung import Plananwendung, PlanInfo, PlanQuelle, absenk_fuer, iso, plan_info, zeit
from . import FAEHIGKEITEN, Adapter

if TYPE_CHECKING:
    from ..coach.store import CoachStore
    from . import PmKlimaErkennung

_LOGGER = logging.getLogger(__name__)

# ClimateEntityFeature (homeassistant/components/climate/const.py)
TARGET_TEMPERATURE = 1
TARGET_TEMPERATURE_RANGE = 2
PRESET_MODE = 16
TURN_OFF = 128
TURN_ON = 256

NICHT_VERFUEGBAR = ("unavailable", "unknown")
FENSTER_KLASSEN = ("window", "door", "opening")
SLUG_UNGUELTIG_RE = re.compile(r"[^a-z0-9_]+")
PAUSE = "plan_pause"  # Einstellung: Sommer-Pause der Plananwendung
PM_PLATTFORM = "pm_heizung"
BT_PLATTFORM = "better_thermostat"
# Kühl- und Lüftungsbetrieb: nie mit dem Heizplan beschreiben, im Sommer nicht ausschalten
KUEHLEN = ("cool", "dry", "fan_only", "heat_cool")
BOOST_MAX = 25.0  # Boost ohne Preset: höchstens 25 °C (bzw. max_temp des Thermostats)
SOMMER_MAX_VERSUCHE = 5
TIMER_FELDER: dict[str, Any] = dict.fromkeys(("overlay_bis", "overlay_temp", "boost_bis", "boost_art", "vor_temp", "vor_preset"))

HINWEIS_GERAETEPROGRAMM = (
    "Geräteprogramm aktiv: Das Thermostat steht auf {modus}. Klima Studio wendet den Heizplan nur im Modus heat an."
)
HINWEIS_KUEHLEN = "Das Gerät kühlt oder lüftet ({modus}). Klima Studio wendet den Heizplan darauf nicht an."
HINWEIS_PLAN_AUS = "Die Plananwendung ist in den App-Optionen ausgeschaltet (plan_anwenden)."
HINWEIS_WEITERE = (
    "Im Bereich gibt es weitere Thermostate ({weitere}). Klima Studio steuert nur {climate}. Fassen Sie die Geräte "
    "z. B. mit Better Thermostat zusammen oder weisen Sie das Thermostat über raeume_override zu."
)
HINWEIS_BT = (
    "Better Thermostat {climate} steuert diesen Raum. Die Thermostate {quellen} im selben Bereich gelten als seine "
    "Quellgeräte und werden nicht direkt gesteuert."
)
HINWEIS_TADO = (
    "tado: Die Standard-Übersteuerung ist {typ} statt MANUAL. Werte von Klima Studio enden dann mit dem nächsten "
    "tado-Zeitblock. Stellen Sie in den Optionen der tado-Integration die Übersteuerung auf Manuell."
)
HINWEIS_NICHT_UEBERNOMMEN = "Das Thermostat hat den zuletzt geschriebenen Sollwert {wert} °C nicht übernommen."


def app_status(z: dict[str, Any]) -> tuple[str, str | None]:
    """Status eines Raums für die Übersicht aus ``raum_zustand``: (Status, Ende des Timers als ISO oder None).

    Status: ``boost`` | ``ueberbrueckung`` (Overlay) | ``aus`` | ``hand`` | ``plan_inaktiv`` (Plananwendung
    ausgeschaltet, ``plan_hinweis`` gesetzt oder kein Heizplan/Plan-Soll) | ``sommerpause`` | ``plan``."""

    def ende(bis: Any) -> str | None:
        dt = zeit(bis)
        return iso(dt) if dt else None

    if z.get("boost_bis"):
        return "boost", ende(z["boost_bis"])
    if z.get("overlay_bis"):
        return "ueberbrueckung", ende(z["overlay_bis"])
    modus = z.get("modus")
    if modus in ("aus", "hand"):
        return modus, None
    if not z.get("plan_anwendung"):
        return "plan_inaktiv", None
    if z.get("sommer_pause"):
        return "sommerpause", None
    if z.get("plan_hinweis") or z.get("plan_soll") is None:
        return "plan_inaktiv", None
    return "plan", None


def _slug(text: str) -> str:
    slug = SLUG_UNGUELTIG_RE.sub("_", str(text).lower()).strip("_")
    return slug[:64] or "raum"


def _bool(v: Any) -> bool | None:
    if v == "on":
        return True
    if v == "off":
        return False
    return None


def _features(attrs: dict[str, Any]) -> int | None:
    v = attrs.get("supported_features")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return int(v)


def _liste(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _registry_eintrag(e: dict[str, Any]) -> dict[str, Any]:
    """Eintrag aus ``list`` oder ``list_for_display`` (bereits umgesetzt) vereinheitlichen."""
    return {
        "entity_id": e.get("entity_id"),
        "platform": e.get("platform") or e.get("pl") or None,
        "area_id": e.get("area_id") or None,
        "device_id": e.get("device_id") or None,
        "hidden": bool(e.get("hidden") or e.get("hidden_by")),
    }


class GenerischAdapter(Adapter):
    betriebsart = "generisch"
    braucht_registries = True
    heizperiode_wirksam = True
    heizperiode_intern = True

    def __init__(self, opts: Options, client: HAClient, store: CoachStore | None = None) -> None:
        super().__init__(opts, client, store)
        self._app: dict[str, dict[str, Any]] | None = None
        self._pause: bool | None = None
        self._plan: dict[str, PlanInfo] = {}
        #: serialisiert Aktionen, Plananwendung und Heizperiode (App-Zustand, Schreibbefehle)
        self.lock = asyncio.Lock()
        self.quelle = PlanQuelle(client, lambda: self.uhr())
        # Der Hintergrund läuft immer (Rück-Timer); plan_anwenden schaltet nur die Heizpläne ab
        self.plananwendung = Plananwendung(self)
        #: Erkennung PM Klima (vom Server gesetzt): Entitäten mit praefix_klima ausschließen, wenn geladen
        self.pm_erkennung: PmKlimaErkennung | None = None
        self._pm_geladen: bool | None = None
        #: zuletzt erfolgreich gelesene Registrierungen (bei Fehlern weiterverwendet)
        self._registries: dict[str, list[dict[str, Any]]] = {}
        #: Hinweise der Raumerkennung je Raum (mehrere Thermostate, Better Thermostat)
        self._raum_hinweise: dict[str, list[str]] = {}
        #: Fehlversuche des Sommer-Abgleichs je Raum
        self._sommer_fehler: dict[str, int] = {}

    @property
    def plan_aktiv(self) -> bool:
        """Heizpläne anwenden (Option ``plan_anwenden``)."""
        return bool(self.opts.plan_anwenden)

    # ------------------------------------------------------------- Räume

    async def registries_laden(self) -> dict[str, list[dict[str, Any]]]:
        """Registrierungen lesen. Scheitert eine, gilt die zuletzt gelesene weiter; ohne sie werden Räume
        ohne diese Registrierung (aus der object_id) gebildet."""
        out: dict[str, list[dict[str, Any]]] = {}

        async def entitaeten() -> list[dict[str, Any]]:
            try:
                return await self.client.entity_registry_list_for_display()
            except HAError as err:
                _LOGGER.debug("entity_registry/list_for_display nicht möglich (%s), nutze list", err)
                return await self.client.entity_registry_list()

        for key, laden, name in (
            ("areas", self.client.area_registry_list, "Bereiche"),
            ("entities", entitaeten, "Entitätsregistrierung"),
            ("devices", self.client.device_registry_list, "Geräteregistrierung"),
        ):
            try:
                out[key] = await laden()
                self._registries[key] = out[key]
            except HAError as err:
                if key in self._registries:
                    _LOGGER.warning("%s nicht lesbar, verwende die zuletzt gelesenen: %s", name, err)
                    out[key] = self._registries[key]
                else:
                    _LOGGER.warning("%s nicht lesbar: %s", name, err)
                    out[key] = []
        return out

    async def raeume(self, states: list[dict[str, Any]]) -> list[Room]:
        registries = await self.registries_laden()
        if self.pm_erkennung is not None:
            self._pm_geladen = await self.pm_erkennung.geladen()
        self.quelle.ids_leeren()
        return self.raeume_erkennen(states, registries)

    def raeume_erkennen(self, states: list[dict[str, Any]], registries: dict[str, list[dict[str, Any]]]) -> list[Room]:
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        areas = {a["area_id"]: a for a in registries.get("areas") or [] if isinstance(a, dict) and a.get("area_id")}
        devices = {d["id"]: d for d in registries.get("devices") or [] if isinstance(d, dict) and d.get("id")}
        ents = {
            e["entity_id"]: _registry_eintrag(e)
            for e in registries.get("entities") or []
            if isinstance(e, dict) and e.get("entity_id")
        }

        def bereich(eid: str) -> str | None:
            """Bereich der Entität, sonst Bereich ihres Geräts."""
            e = ents.get(eid) or {}
            if e.get("area_id"):
                return e["area_id"]
            return (devices.get(e.get("device_id") or "") or {}).get("area_id") or None

        def dc(eid: str) -> str | None:
            return (by_id[eid].get("attributes") or {}).get("device_class")

        def plattform(eid: str) -> str | None:
            return (ents.get(eid) or {}).get("platform")

        def geraet(eid: str) -> str | None:
            return (ents.get(eid) or {}).get("device_id")

        je_bereich: dict[str, list[str]] = {}
        for eid in sorted(by_id):
            if eid.startswith(("sensor.", "binary_sensor.")):
                b = bereich(eid)
                if b:
                    je_bereich.setdefault(b, []).append(eid)

        def ausgeschlossen(eid: str) -> bool:
            if (ents.get(eid) or {}).get("hidden"):
                return True
            if plattform(eid) == PM_PLATTFORM:
                return True  # Entitäten von PM Klima steuert die Integration selbst
            return bool(self._pm_geladen and self.opts.praefix_klima and eid.startswith(self.opts.praefix_klima))

        kandidaten = sorted(e for e in by_id if e.startswith("climate.") and not ausgeschlossen(e))
        # Better Thermostat: Hülle bevorzugen, Thermostate im selben Bereich bzw. Gerät gelten als Quellgeräte
        bt = [e for e in kandidaten if plattform(e) == BT_PLATTFORM]
        quellen: dict[str, list[str]] = {}
        for h in bt:
            hb, hg = bereich(h), geraet(h)
            for e in kandidaten:
                if e in bt:
                    continue
                if (hb is not None and bereich(e) == hb) or (hg is not None and geraet(e) == hg):
                    quellen.setdefault(h, []).append(e)
        alle_quellen = {e for liste in quellen.values() for e in liste}
        for h, liste in quellen.items():
            _LOGGER.warning(
                "Better Thermostat %s: %s im selben Bereich/Gerät als Quellgeräte angenommen und nicht direkt gesteuert",
                h,
                ", ".join(liste),
            )

        rooms: dict[str, Room] = {}
        raum_bereich: dict[str, str | None] = {}
        hinweise: dict[str, list[str]] = {}
        # Thermostate mit Bereich zuerst, Better-Thermostat-Hüllen vor anderen Thermostaten des Bereichs
        klima = sorted((bereich(e) is None, e not in bt, e) for e in kandidaten if e not in alle_quellen)
        for _, _, eid in klima:
            attrs = by_id[eid].get("attributes") or {}
            b = bereich(eid)
            if b:
                slug = _slug(b)
                name = str((areas.get(b) or {}).get("name") or "") or _name_for(slug, None)
            else:
                slug = _slug(eid.split(".", 1)[1])
                name = _name_for(slug, attrs.get("friendly_name"))
            if slug in rooms:
                if b is not None and raum_bereich.get(slug) == b:
                    _LOGGER.info(
                        "Bereich %s: mehrere Thermostate, Raumthermostat ist %s (%s nur angezeigt; änderbar mit raeume_override)",
                        b,
                        rooms[slug].climate,
                        eid,
                    )
                    rooms[slug].climate_weitere.append(eid)
                    continue
                n = 2
                while f"{slug}_{n}" in rooms:
                    n += 1
                slug = f"{slug}_{n}"
            rooms[slug] = Room(raum=slug, name=name, climate=eid)
            raum_bereich[slug] = b
            if eid in quellen:
                hinweise.setdefault(slug, []).append(HINWEIS_BT.format(climate=eid, quellen=", ".join(quellen[eid])))

        humidity = [e for e in by_id if e.startswith("sensor.") and dc(e) == "humidity"]
        co2 = [e for e in by_id if e.startswith("sensor.") and dc(e) == "carbon_dioxide"]
        fenster = [e for e in by_id if e.startswith("binary_sensor.") and dc(e) in FENSTER_KLASSEN]

        def auswahl(kandidaten: list[str], slug: str, b: str | None) -> list[str]:
            """Sensoren des Bereichs, sonst (Rückfall wie bisher) Sensoren mit dem Raumkürzel im Namen,
            die keinem anderen Bereich zugeordnet sind."""
            im_bereich = je_bereich.get(b or "", [])
            eigen = [e for e in kandidaten if e in im_bereich]
            if eigen:
                return eigen
            return [e for e in kandidaten if slug in e.split(".", 1)[1] and not bereich(e)]

        def bereichssensor(b: str | None, feld: str) -> str | None:
            """Im Bereich eingetragener Sensor (``temperature_entity_id``/``humidity_entity_id``), falls vorhanden."""
            eid = (areas.get(b or "") or {}).get(feld)
            return eid if isinstance(eid, str) and eid in by_id else None

        for slug, room in rooms.items():
            attrs = (by_id.get(room.climate or "") or {}).get("attributes") or {}
            b = raum_bereich.get(slug)
            room.feuchte = bereichssensor(b, "humidity_entity_id") or _pick_by_value(
                auswahl(humidity, slug, b), by_id, an.to_float(attrs.get("current_humidity"))
            )
            cands = sorted(auswahl(co2, slug, b))
            room.co2 = cands[0] if cands else None
            cands = sorted(auswahl(fenster, slug, b))
            room.fenster = cands[0] if cands else None
            temps = sorted(e for e in je_bereich.get(b or "", []) if e.startswith("sensor.") and dc(e) == "temperature")
            room.temperatur = bereichssensor(b, "temperature_entity_id") or (temps[0] if temps else None)
            sched = f"{self.opts.praefix_heizplan}{slug}"
            room.schedule = sched if sched.startswith("schedule.") and sched in by_id else None

        ergebnis = self._eindeutig(overrides_anwenden(rooms, self.opts))
        for room in ergebnis:
            room.climate_weitere = [e for e in room.climate_weitere if e != room.climate]
            if room.climate_weitere:
                hinweise.setdefault(room.raum, []).append(
                    HINWEIS_WEITERE.format(weitere=", ".join(room.climate_weitere), climate=room.climate)
                )
        self._raum_hinweise = hinweise
        return ergebnis

    def _eindeutig(self, rooms: list[Room]) -> list[Room]:
        """Eine climate-Entität gehört zu genau einem Raum: Räume mit ausdrücklichem Override ``climate`` haben
        Vorrang, sonst der erste Raum. Automatisch erkannte Räume ohne Thermostat entfallen dann."""
        ausdruecklich = {str(ov.get("raum", "")).strip().lower() for ov in self.opts.raeume if isinstance(ov.get("climate"), str)}
        vergeben: dict[str, str] = {}
        for room in sorted(rooms, key=lambda r: r.raum not in ausdruecklich):
            if room.climate and room.climate in vergeben:
                _LOGGER.warning(
                    "Thermostat %s ist Raum %s und %s zugeordnet; gesteuert wird es nur in %s",
                    room.climate,
                    vergeben[room.climate],
                    room.raum,
                    vergeben[room.climate],
                )
                room.climate = None
            elif room.climate:
                vergeben[room.climate] = room.raum
        return [r for r in rooms if r.climate or r.raum in {str(o.get("raum", "")).strip().lower() for o in self.opts.raeume}]

    def raum_faehigkeiten(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cl = by_id.get(room.climate or "")
        attrs = (cl or {}).get("attributes") or {}
        modi = _liste(attrs.get("hvac_modes"))
        presets = _liste(attrs.get("preset_modes"))
        features = _features(attrs)
        if features is not None:
            ziel = bool(features & TARGET_TEMPERATURE)
            bereich = bool(features & TARGET_TEMPERATURE_RANGE) and not ziel
        else:
            ziel = "temperature" in attrs
            bereich = False
        # Nur target_temp_low/high (z. B. im Modus heat_cool): Bereichs-Thermostat
        if attrs.get("temperature") is None and (
            attrs.get("target_temp_low") is not None or attrs.get("target_temp_high") is not None
        ):
            bereich = True
        grund: str | None = None
        if not room.climate:
            grund = "Kein Thermostat zugeordnet."
        elif cl is None:
            grund = f"{room.climate} ist nicht vorhanden."
        elif cl.get("state") in (None, *NICHT_VERFUEGBAR):
            grund = f"{room.climate} ist derzeit nicht verfügbar."
        elif bereich:
            grund = "Thermostate mit Temperaturbereich werden in dieser Version nur angezeigt, nicht gesteuert."
        elif not ziel:
            grund = "Das Thermostat unterstützt keine Solltemperatur."
        return {
            "steuerbar": grund is None,
            "grund": grund,
            "modi": modi,
            "presets": presets,
            "boost": "boost" in presets,
            "aus": "off" in modi or bool((features or 0) & TURN_OFF),
            "bereich": bereich,
        }

    def raum_zustand(self, room: Room, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cl = by_id.get(room.climate or "") or {}
        attrs = cl.get("attributes") or {}
        f = self.raum_faehigkeiten(room, by_id)
        ist = an.to_float(attrs.get("current_temperature"))
        if ist is None and room.temperatur:
            ist = an.to_float((by_id.get(room.temperatur) or {}).get("state"))
        feuchte = an.to_float(attrs.get("current_humidity"))
        if feuchte is None and room.feuchte:
            feuchte = an.to_float((by_id.get(room.feuchte) or {}).get("state"))
        low, high = an.to_float(attrs.get("target_temp_low")), an.to_float(attrs.get("target_temp_high"))
        a = self.app(room.raum)
        info = self._plan.get(room.raum)
        wechsel = info.wechsel.isoformat(timespec="seconds") if info and info.wechsel else None
        return {
            "name": room.name,
            "climate": room.climate,
            # App-Modus (plan | hand | aus); der Zustand des Thermostats steht in hvac_modus
            "modus": a["modus"],
            "hvac_modus": cl.get("state"),
            "preset": attrs.get("preset_mode"),
            "grund": self._grund(a),
            "anzeige": None,
            "hvac_action": attrs.get("hvac_action"),
            "ist": ist,
            "soll": an.to_float(attrs.get("temperature")),
            "feuchte": feuchte,
            "plan_soll": info.soll if info else None,
            "zeitplan_temperatur": info.soll if info else None,
            "naechster_wechsel": wechsel,
            "naechster_wechsel_grund": None,
            "overlay_bis": a["overlay_bis"],
            "boost_bis": a["boost_bis"],
            "fenster_offen": self.fenster_offen(room, by_id),
            "min_temp": an.to_float(attrs.get("min_temp")),
            "max_temp": an.to_float(attrs.get("max_temp")),
            "schritt": an.to_float(attrs.get("target_temp_step")) or 0.5,
            "steuerbar": f["steuerbar"],
            "steuerbar_grund": f["grund"],
            "soll_bereich": {"min": low, "max": high} if f["bereich"] else None,
            "faehigkeiten": {k: f[k] for k in ("modi", "presets", "boost", "aus")},
            # Wählbare App-Modi und Art des Boosts (Preset boost oder Temperatur mit Timer)
            "modi": [m for m in st.MODI_GENERISCH if m != "aus" or f["aus"]],
            "boost_art": "preset" if f["boost"] else "temperatur",
            "boost_temp": None if f["boost"] else self.boost_temp(attrs),
            "sommer_pause": self.pause,
            "plan_anwendung": self.plan_aktiv,
            # Warum der Heizplan (gerade) nicht angewendet wird; None = keine Einschränkung
            "plan_hinweis": self._plan_hinweis(a, cl, info),
            # Weitere Hinweise zum Raum (mehrere Thermostate, Better Thermostat, tado, nicht übernommen)
            "hinweise": self._hinweise(room, a, attrs),
            "climate_weitere": list(room.climate_weitere),
            "nicht_uebernommen": int(a.get("nachgeschrieben") or 0) >= 2,
        }

    def _plan_hinweis(self, a: dict[str, Any], cl: dict[str, Any], info: PlanInfo | None) -> str | None:
        zustand = cl.get("state")
        if a["modus"] == "plan":
            if not self.plan_aktiv:
                return HINWEIS_PLAN_AUS
            if zustand in KUEHLEN:
                return HINWEIS_KUEHLEN.format(modus=zustand)
            if zustand not in ("heat", "off", None, *NICHT_VERFUEGBAR):
                return HINWEIS_GERAETEPROGRAMM.format(modus=zustand)
        return info.hinweis if info else None

    def _hinweise(self, room: Room, a: dict[str, Any], attrs: dict[str, Any]) -> list[str]:
        out = list(self._raum_hinweise.get(room.raum) or [])
        typ = attrs.get("default_overlay_type")
        if isinstance(typ, str) and typ and typ.upper() != "MANUAL":
            out.append(HINWEIS_TADO.format(typ=typ))
        if int(a.get("nachgeschrieben") or 0) >= 2 and a.get("geschrieben") is not None:
            out.append(HINWEIS_NICHT_UEBERNOMMEN.format(wert=self.fmt(a["geschrieben"])))
        return out

    def _grund(self, a: dict[str, Any]) -> str:
        if a["modus"] == "aus":
            return "aus"
        if a["boost_bis"]:
            return "boost"
        if a["overlay_bis"]:
            return "overlay"
        if a["modus"] == "hand":
            return "manuell"
        return "sommer" if self.pause else "zeitplan"

    def fenster_offen(self, room: Room, by_id: dict[str, dict[str, Any]]) -> bool | None:
        return _bool((by_id.get(room.fenster) or {}).get("state")) if room.fenster else None

    # ------------------------------------------------------------- App-Zustand (SQLite)

    def _laden(self) -> dict[str, dict[str, Any]]:
        if self._app is None:
            if self.store is not None:
                self._app = self.store.app_raeume_lesen()
                pause = self.store.einstellung_lesen(PAUSE)
            else:
                self._app, pause = {}, None
            self._pause = bool(isinstance(pause, dict) and pause.get("sommer"))
        return self._app

    def app(self, raum: str) -> dict[str, Any]:
        """App-Zustand eines Raums (Kopie; Standard: Modus hand ohne Timer)."""
        return {**APP_RAUM_FELDER, **(self._laden().get(raum) or {})}

    async def app_speichern(self, room: Room, a: dict[str, Any]) -> None:
        """App-Zustand speichern; er gehört zur aktuellen climate-Entität des Raums."""
        if room.climate:
            a["climate"] = room.climate
        self._laden()[room.raum] = {k: a.get(k, v) for k, v in APP_RAUM_FELDER.items()}
        if self.store is not None:
            await self.store.app_raum_setzen(room.raum, a)

    async def _app_verwerfen(self, raum: str) -> None:
        self._laden().pop(raum, None)
        if self.store is not None:
            await self.store.app_raum_loeschen(raum)

    @property
    def pause(self) -> bool:
        """Sommer-Pause: Heizpläne werden nicht angewendet."""
        self._laden()
        return bool(self._pause)

    def pause_gespeichert(self) -> bool:
        """Gibt es bereits einen gespeicherten Zustand der Sommer-Pause?"""
        return self.store is not None and self.store.einstellung_lesen(PAUSE) is not None

    async def pause_setzen(self, sommer: bool) -> None:
        self._laden()
        self._pause = sommer
        if self.store is not None:
            await self.store.einstellung_setzen(PAUSE, {"sommer": sommer, "seit": iso(self.uhr())})

    async def zustand_abgleichen(self, rooms: list[Room], by_id: dict[str, dict[str, Any]]) -> None:
        """App-Zustand an die Räume anpassen (Aufrufer hält ``lock``).

        * Zustand ohne climate-Entität (ältere Version): der aktuellen zuordnen.
        * Raumthermostat geändert: Timer am bisherigen Thermostat zurücksetzen, Zustand verwerfen.
        * Raum nicht mehr vorhanden: laufende Timer zurücksetzen und löschen (Modus bleibt gespeichert).
        """
        if not rooms:
            return  # ohne Raumliste (z. B. Fehler beim Lesen) nichts verwerfen
        je_raum = {r.raum: r for r in rooms}
        for raum, z in list(self._laden().items()):
            room = je_raum.get(raum)
            try:
                if room is not None and room.climate:
                    if not z.get("climate"):
                        await self.app_speichern(room, dict(z))
                    elif z["climate"] != room.climate:
                        await self._timer_verwerfen(Room(raum=raum, name=room.name, climate=z["climate"]), dict(z), by_id)
                        await self._app_verwerfen(raum)
                        await self.protokoll(
                            room,
                            f"Raumthermostat geändert ({z['climate']} → {room.climate}), App-Zustand zurückgesetzt",
                            {"alt": z["climate"], "neu": room.climate, "anlass": "thermostat_geaendert"},
                        )
                elif room is None and (z.get("overlay_bis") or z.get("boost_bis")):
                    alt = Room(raum=raum, name=raum, climate=z.get("climate"))
                    neu = dict(z)
                    await self._timer_verwerfen(alt, neu, by_id)
                    self._laden()[raum] = {k: neu.get(k, v) for k, v in APP_RAUM_FELDER.items()}
                    if self.store is not None:
                        await self.store.app_raum_setzen(raum, neu)
                    await self.protokoll(alt, "Raum nicht mehr vorhanden, zeitweise Einstellung beendet", {"anlass": "verwaist"})
            except HAError as err:
                _LOGGER.warning("App-Zustand %s nicht abgeglichen: %s", raum, err)

    async def _timer_verwerfen(self, room: Room, a: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> None:
        """Overlay/Boost beenden; existiert das Thermostat noch, Preset und Sollwert davor wiederherstellen."""
        cl = by_id.get(room.climate or "")
        if cl is not None and (a.get("overlay_bis") or a.get("boost_bis")):
            attrs = cl.get("attributes") or {}
            an_ = cl.get("state") not in ("off", None, *NICHT_VERFUEGBAR)
            vor = a.get("vor_temp")
            await self._boost_beenden(room, a, attrs, an_)
            if an_ and vor is not None:
                await self.soll_schreiben(room, a, vor, attrs)
        a.update(TIMER_FELDER)

    # ------------------------------------------------------------- Heizpläne

    async def raeume_aktuell(self, states: list[dict[str, Any]]) -> list[Room]:
        if self.raeume_quelle is not None:
            return await self.raeume_quelle()
        return await self.raeume(states)

    async def vorbereiten(self, rooms: list[Room], by_id: dict[str, dict[str, Any]]) -> None:
        items = await self.quelle.items(rooms)
        jetzt, tz = self.uhr(), self.tz()
        for room in rooms:
            self._plan[room.raum] = plan_info(room, by_id, items.get(room.schedule or ""), jetzt, tz, self.opts.absenktemperatur)

    def plan_geaendert(self) -> None:
        self.quelle.invalidieren()

    def plan_info_cache(self, raum: str) -> PlanInfo | None:
        return self._plan.get(raum)

    # ------------------------------------------------------------- Schreiben

    @staticmethod
    def fmt(v: float) -> str:
        return st._fmt_temp(v)

    def zeit_text(self, bis: datetime | str | None) -> str:
        if bis is None or bis == "dauerhaft":
            return "dauerhaft"
        dt = bis if isinstance(bis, datetime) else zeit(bis)
        if dt is None:
            return "dauerhaft"
        lokal = dt.astimezone(self.tz())
        heute = self.uhr().astimezone(self.tz()).date()
        tag = "" if lokal.date() == heute else f"{lokal:%d.%m.} "
        return f"bis {tag}{lokal:%H:%M} Uhr"

    @staticmethod
    def runden(wert: float, attrs: dict[str, Any]) -> float:
        """Auf die Schrittweite des Thermostats runden und auf min/max begrenzen."""
        lo, hi, schritt = st.grenzen_generisch(attrs)
        v = round(wert / schritt) * schritt
        return round(min(max(v, lo), hi), 2)

    @staticmethod
    def toleranz(attrs: dict[str, Any]) -> float:
        """Abweichung, ab der ein Sollwert am Thermostat als anders gilt: max(Schritt/2, 0,25 °C)."""
        _, _, schritt = st.grenzen_generisch(attrs)
        return max(schritt / 2, 0.25)

    @staticmethod
    def boost_temp(attrs: dict[str, Any]) -> float:
        """Boost ohne Preset: höchstens 25 °C bzw. die Höchsttemperatur des Thermostats."""
        _, hi, _ = st.grenzen_generisch(attrs)
        return GenerischAdapter.runden(min(hi, BOOST_MAX), attrs)

    async def protokoll(self, room: Room, text: str, daten: dict[str, Any]) -> None:
        _LOGGER.info("Plananwendung %s: %s", room.raum, text)
        if self.store is not None:
            await self.store.ereignis("plan", room.raum, f"{room.name}: {text}", daten)

    async def _dienst(self, room: Room, service: str, daten: dict[str, Any]) -> None:
        await self.client.call_service("climate", service, {"entity_id": room.climate, **daten})

    async def soll_schreiben(
        self, room: Room, a: dict[str, Any], wert: float, attrs: dict[str, Any], protokoll: str | None = None
    ) -> bool:
        """Solltemperatur setzen und als zuletzt geschriebenen Wert merken (``protokoll``: Ereignis ``plan``).

        Steht das Thermostat bereits auf dem gerundeten Wert, wird nichts gesendet (API-Limits, z. B. tado).
        Rückgabe: ob ein Befehl gesendet wurde."""
        v = self.runden(wert, attrs)
        ist = an.to_float(attrs.get("temperature"))
        if ist is not None and abs(ist - v) < 0.01:
            a.update(geschrieben=v, geschrieben_zeit=iso(self.uhr()), gesehen=ist, nachgeschrieben=0)
            return False
        await self._dienst(room, "set_temperature", {"temperature": st._temp_out(v)})
        # ``gesehen`` bleibt der Wert vor dem Schreiben, bis das Thermostat den neuen Wert meldet
        a.update(geschrieben=v, geschrieben_zeit=iso(self.uhr()), gesehen=ist, nachgeschrieben=0)
        if protokoll:
            await self.protokoll(room, protokoll, {"temperatur": v, "dienst": "climate.set_temperature"})
        return True

    @staticmethod
    def kann_aus(attrs: dict[str, Any]) -> bool:
        return "off" in _liste(attrs.get("hvac_modes")) or bool((_features(attrs) or 0) & TURN_OFF)

    async def _ausschalten(self, room: Room, attrs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if "off" in _liste(attrs.get("hvac_modes")):
            await self._dienst(room, "set_hvac_mode", {"hvac_mode": "off"})
            return "set_hvac_mode", {"hvac_mode": "off"}
        await self._dienst(room, "turn_off", {})
        return "turn_off", {}

    async def _einschalten(self, room: Room, attrs: dict[str, Any], gemerkt: str | None) -> tuple[str, dict[str, Any]]:
        """Heizenden Modus wiederherstellen: bevorzugt heat (nicht ein gemerktes Geräteprogramm auto),
        sonst der gemerkte Modus bzw. der erste verfügbare."""
        modi = [m for m in _liste(attrs.get("hvac_modes")) if m != "off"]
        modus = "heat" if "heat" in modi else (gemerkt if gemerkt in modi else (modi[0] if modi else None))
        if modus is None:
            await self._dienst(room, "turn_on", {})
            return "turn_on", {}
        await self._dienst(room, "set_hvac_mode", {"hvac_mode": modus})
        return "set_hvac_mode", {"hvac_mode": modus}

    async def _boost_beenden(self, room: Room, a: dict[str, Any], attrs: dict[str, Any], aktiv: bool) -> None:
        """Boost-Felder löschen; ein Preset-Boost wird auf das vorherige Preset (sonst none) zurückgesetzt."""
        if a.get("boost_bis") and a.get("boost_art") == "preset" and aktiv:
            presets = _liste(attrs.get("preset_modes"))
            vor = a.get("vor_preset")
            ziel = vor if vor in presets and vor != "boost" else ("none" if "none" in presets else None)
            if ziel is not None and attrs.get("preset_mode") != ziel:
                await self._dienst(room, "set_preset_mode", {"preset_mode": ziel})
        a.update(boost_bis=None, boost_art=None, vor_preset=None)

    async def rueckkehr(
        self, room: Room, a: dict[str, Any], cl: dict[str, Any], info: PlanInfo | None, anlass: str | None
    ) -> tuple[str, bool]:
        """Overlay/Boost beenden: Preset zurück, Solltemperatur auf Plan-Soll (Plan-Modus mit Plananwendung,
        Thermostat im Zustand heat, ohne Sommer-Pause) bzw. auf den Wert vor Overlay/Boost.
        ``anlass``: Ereignis ``plan`` schreiben.

        Rückgabe ``(text, geschrieben)``.
        """
        attrs = cl.get("attributes") or {}
        an_ = cl.get("state") not in ("off", None, *NICHT_VERFUEGBAR)
        hatte_timer = bool(a.get("overlay_bis") or a.get("boost_bis"))
        vor = a.get("vor_temp")
        await self._boost_beenden(room, a, attrs, an_)
        a.update(overlay_bis=None, overlay_temp=None, vor_temp=None)
        beendet = "Zeitweise Einstellung beendet"
        if not an_:
            return beendet, False
        ziel: float | None = None
        nach_plan = a["modus"] == "plan" and self.plan_aktiv and not self.pause and cl.get("state") == "heat"
        if nach_plan and info is not None and info.soll is not None:
            ziel = info.soll
        elif hatte_timer and vor is not None:
            ziel, nach_plan = vor, False
        else:
            return beendet, False
        ziel = self.runden(ziel, attrs)
        text = f"Zurück auf {self.fmt(ziel)} °C" + (" (Heizplan)" if nach_plan else "")
        geschrieben = await self.soll_schreiben(room, a, ziel, attrs)
        if anlass:
            await self.protokoll(room, f"{anlass}, {text[0].lower()}{text[1:]}", {"temperatur": ziel, "anlass": "rueckkehr"})
        return text, geschrieben

    async def sommer_abgleich(
        self, room: Room, a: dict[str, Any], cl: dict[str, Any], info: PlanInfo | None = None
    ) -> str | None:
        """Beginn der Sommer-Pause (``sommer_aus_offen``, nur Räume im Modus plan mit Thermostat im Zustand heat):
        laufende Timer zurücksetzen, dann mit ``plan_pausieren_und_aus`` ausschalten (Modus merken), sonst bzw.
        ohne Ausschaltmöglichkeit einmal die Absenktemperatur schreiben. Heizperiode: gemerkten Modus
        wiederherstellen (bevorzugt heat). Nach wiederholten Fehlern wird aufgegeben (Ereignis).

        Rückgabe: Zustand des Thermostats danach."""
        zustand = cl.get("state")
        attrs = cl.get("attributes") or {}
        if self.pause and a.get("sommer_aus_offen"):
            if a["modus"] == "plan" and zustand == "heat" and self.plan_aktiv:
                try:
                    if a.get("overlay_bis") or a.get("boost_bis"):
                        await self.rueckkehr(room, a, cl, info, None)
                    if self.opts.sommer_aktion == "plan_pausieren_und_aus" and self.kann_aus(attrs):
                        a["hvac_vor_sommer"] = zustand
                        await self._ausschalten(room, attrs)
                        await self.protokoll(room, "Sommer, Thermostat ausgeschaltet", {"hvac_mode": "off", "vorher": zustand})
                        zustand = "off"
                    else:
                        wert = self.runden(absenk_fuer(room, self.opts.absenktemperatur), attrs)
                        if await self.soll_schreiben(room, a, wert, attrs):
                            await self.protokoll(
                                room,
                                f"Sommer, Absenkung {self.fmt(wert)} °C (Heizplan pausiert)",
                                {"temperatur": wert, "anlass": "sommer"},
                            )
                except HAError as err:
                    n = self._sommer_fehler.get(room.raum, 0) + 1
                    self._sommer_fehler[room.raum] = n
                    if n < SOMMER_MAX_VERSUCHE:
                        raise
                    self._sommer_fehler.pop(room.raum, None)
                    a["sommer_aus_offen"] = False
                    await self.protokoll(
                        room,
                        f"Sommer: Thermostat nach {n} Versuchen nicht umgestellt ({err}). Bitte prüfen Sie das Gerät.",
                        {"anlass": "sommer_fehler", "versuche": n},
                    )
                    return zustand
            self._sommer_fehler.pop(room.raum, None)
            a["sommer_aus_offen"] = False
        elif not self.pause and a.get("hvac_vor_sommer"):
            gemerkt = a["hvac_vor_sommer"]
            if a["modus"] == "plan" and zustand == "off":
                _, daten = await self._einschalten(room, attrs, gemerkt)
                zustand = daten.get("hvac_mode") or gemerkt
                await self.protokoll(room, "Heizperiode, Thermostat wieder eingeschaltet", {"hvac_mode": zustand})
            a["hvac_vor_sommer"] = None
        return zustand

    # ------------------------------------------------------------- Steuerung

    def pruefe_steuerbar(self, room: Room) -> None:
        if not room.climate:
            raise st.json_fehler(web.HTTPConflict, "Kein Thermostat zugeordnet.", "nicht_steuerbar")

    async def aktion(self, room: Room, body: dict[str, Any]) -> dict[str, Any]:
        states = await self.client.get_states()
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        cl = by_id.get(room.climate or "")
        f = self.raum_faehigkeiten(room, by_id)
        if not f["steuerbar"]:
            code = "nicht_verfuegbar" if cl is None or cl.get("state") in (None, *NICHT_VERFUEGBAR) else "nicht_steuerbar"
            raise st.json_fehler(web.HTTPConflict, str(f["grund"]), code)
        assert cl is not None
        attrs = cl.get("attributes") or {}
        befehl = st.pruefe_befehl_generisch(body, attrs)
        await self.vorbereiten([room], by_id)
        async with self.lock:
            a = self.app(room.raum)
            alt = dict(a)
            try:
                out = await self._aktion(room, cl, a, f, befehl, by_id)
            finally:
                if a != alt:
                    await self.app_speichern(room, a)
        if befehl.get("modus") == "plan" and self.plan_aktiv and self.plananwendung is not None:
            await self.plananwendung.takt_raum(room)
        return out

    async def _aktion(
        self,
        room: Room,
        cl: dict[str, Any],
        a: dict[str, Any],
        f: dict[str, Any],
        befehl: dict[str, Any],
        by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        aktion = befehl["aktion"]
        attrs = cl.get("attributes") or {}
        zustand = cl.get("state")
        jetzt = self.uhr()

        def ergebnis(service: str | None, daten: dict[str, Any], text: str) -> dict[str, Any]:
            return {
                "aktion": aktion,
                "domain": "climate" if service else "klimastudio",
                "service": service or aktion,
                "daten": daten,
                "text": text,
            }

        if aktion == "modus":
            return await self._modus(room, cl, a, f, befehl["modus"], ergebnis)
        if aktion == "zurueck":
            text, geschrieben = await self.rueckkehr(room, a, cl, self._plan.get(room.raum), None)
            daten = {"temperature": st._temp_out(a["geschrieben"])} if geschrieben else {}
            return ergebnis("set_temperature" if geschrieben else None, daten, text)
        if a["modus"] == "aus" or zustand == "off":
            raise st.json_fehler(
                web.HTTPConflict,
                "Das Thermostat ist ausgeschaltet. Stellen Sie den Raum zuerst auf Plan oder Hand.",
                "modus",
            )
        if aktion == "overlay":
            if a["modus"] != "plan":
                raise st.json_fehler(
                    web.HTTPConflict,
                    "Eine zeitweise Temperatur ist nur im Modus Plan möglich. Stellen Sie den Raum zuerst auf Plan.",
                    "modus",
                )
            temp, dauer = befehl["temperatur"], befehl["dauer"]
            zeitweise = a.get("overlay_bis") or (a.get("boost_bis") and a.get("boost_art") == "temperatur")
            vor = a.get("vor_temp") if zeitweise else an.to_float(attrs.get("temperature"))
            await self._boost_beenden(room, a, attrs, True)
            if dauer is None:
                info = self._plan.get(room.raum)
                bis = iso(info.wechsel) if info and info.wechsel else "dauerhaft"
                text = f"Temperatur {self.fmt(temp)} °C bis zum nächsten Planwechsel"
            elif dauer == 0:
                bis, text = "dauerhaft", f"Temperatur {self.fmt(temp)} °C dauerhaft"
            else:
                bis, text = iso(jetzt + timedelta(minutes=dauer)), f"Temperatur {self.fmt(temp)} °C für {dauer} Minuten"
            gesendet = await self.soll_schreiben(room, a, temp, attrs)
            a.update(overlay_bis=bis, overlay_temp=temp, vor_temp=vor)
            return ergebnis(
                "set_temperature" if gesendet else None, {"temperatur": st._temp_out(temp), "dauer": dauer, "bis": bis}, text
            )
        if aktion == "temperatur":
            if a["modus"] != "hand":
                raise st.json_fehler(
                    web.HTTPConflict, "Ein Handwert ist nur im Modus Hand möglich. Stellen Sie den Raum zuerst auf Hand.", "modus"
                )
            temp = befehl["temperatur"]
            await self._boost_beenden(room, a, attrs, True)
            a.update(vor_temp=None)
            gesendet = await self.soll_schreiben(room, a, temp, attrs)
            return ergebnis(
                "set_temperature" if gesendet else None, {"temperature": st._temp_out(temp)}, f"Handwert {self.fmt(temp)} °C"
            )
        # boost
        dauer = befehl["dauer"]
        bis = iso(jetzt + timedelta(minutes=dauer))
        laeuft = bool(a.get("boost_bis"))
        if f["boost"]:
            if not laeuft or a.get("boost_art") != "preset":
                a["vor_preset"] = attrs.get("preset_mode")
            await self._dienst(room, "set_preset_mode", {"preset_mode": "boost"})
            a.update(boost_bis=bis, boost_art="preset")
            return ergebnis("set_preset_mode", {"preset_mode": "boost", "dauer": dauer}, f"Boost für {dauer} Minuten")
        hi = self.boost_temp(attrs)
        if not laeuft:
            a["vor_temp"] = a.get("vor_temp") if a.get("overlay_bis") else an.to_float(attrs.get("temperature"))
        a.update(overlay_bis=None, overlay_temp=None)
        gesendet = await self.soll_schreiben(room, a, hi, attrs)
        a.update(boost_bis=bis, boost_art="temperatur")
        return ergebnis(
            "set_temperature" if gesendet else None,
            {"temperature": st._temp_out(hi), "dauer": dauer},
            f"Boost für {dauer} Minuten ({self.fmt(hi)} °C)",
        )

    async def _modus(
        self,
        room: Room,
        cl: dict[str, Any],
        a: dict[str, Any],
        f: dict[str, Any],
        neu: str,
        ergebnis: Callable[[str | None, dict[str, Any], str], dict[str, Any]],
    ) -> dict[str, Any]:
        attrs = cl.get("attributes") or {}
        zustand = cl.get("state")
        text = f"Modus {st.MODUS_TEXT_GENERISCH[neu]}"
        if neu == "aus":
            if not f["aus"]:
                raise st.json_fehler(web.HTTPConflict, "Das Thermostat kann nicht ausgeschaltet werden.", "modus")
            # Overlay/Boost vorher beenden: Temperatur-Boost nicht im ausgeschalteten Gerät stehen lassen
            if a.get("overlay_bis") or a.get("boost_bis"):
                await self.rueckkehr(room, a, cl, self._plan.get(room.raum), None)
            if zustand != "off":
                a["hvac_vor_aus"] = zustand
            a.update(modus="aus", hvac_vor_sommer=None, sommer_aus_offen=False)
            service, daten = await self._ausschalten(room, attrs)
            return ergebnis(service, daten, text)
        alt = a["modus"]
        a["modus"] = neu
        if neu == "hand" and a.get("overlay_bis"):
            a.update(overlay_bis=None, overlay_temp=None, vor_temp=None)
        if neu == "plan" and alt != "plan":
            # Plan-Soll neu anwenden (steht das Thermostat schon darauf, wird nichts gesendet)
            a.update(geschrieben=None, geschrieben_zeit=None, gesehen=None, nachgeschrieben=0)
        service: str | None = None
        daten: dict[str, Any] = {}
        modi = _liste(attrs.get("hvac_modes"))
        if zustand == "off":
            gemerkt = a.get("hvac_vor_aus") or a.get("hvac_vor_sommer")
            if neu == "plan" and self.pause and self.plan_aktiv and self.opts.sommer_aktion == "plan_pausieren_und_aus":
                # Sommer: bleibt aus, zu Beginn der Heizperiode wird eingeschaltet
                a["hvac_vor_sommer"] = gemerkt or "heat"
                text += " (Sommer, das Thermostat bleibt bis zur Heizperiode aus)"
            else:
                service, daten = await self._einschalten(room, attrs, gemerkt)
                a["hvac_vor_sommer"] = None
            a["hvac_vor_aus"] = None
        elif zustand not in ("heat", None, *KUEHLEN, *NICHT_VERFUEGBAR) and "heat" in modi:
            # Geräteprogramm (z. B. auto): einmalig auf heat, damit Plan bzw. Handwert gelten
            await self._dienst(room, "set_hvac_mode", {"hvac_mode": "heat"})
            service, daten = "set_hvac_mode", {"hvac_mode": "heat"}
            text += f" (Thermostat von {zustand} auf heat umgestellt)"
            await self.protokoll(room, f"Thermostat von {zustand} auf heat umgestellt", {"hvac_mode": "heat", "vorher": zustand})
        return ergebnis(service, daten, text)

    # ------------------------------------------------------------- Integration / Fähigkeiten

    def faehigkeiten(self) -> dict[str, bool]:
        # Schimmel-Schätzung folgt mit 1.3.0; keine Freigabe-Entität (Heizperiode intern)
        return {k: (k == "plan_anwendung" and self.opts.plan_anwenden) for k in FAEHIGKEITEN}

    # ------------------------------------------------------------- Heizperiode

    def heizperiode_ist(self) -> bool | None:
        return not self.pause

    async def heizperiode_anwenden(self, aktiv: bool, entitaet: str, erstentscheid: bool = False) -> None:
        """Sommer: Plananwendung pausieren; mit aktiver Plananwendung Räume im Plan-Modus ausschalten
        (``plan_pausieren_und_aus``) bzw. einmal auf die Absenktemperatur stellen. Heizperiode: gemerkten
        Modus wiederherstellen und die Heizpläne anwenden. Hand-Räume bleiben unberührt.

        ``erstentscheid``: allererste Entscheidung der Automatik – nur den Zustand merken, nichts schalten."""
        states = await self.client.get_states()
        by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
        rooms = await self.raeume_aktuell(states)
        await self.vorbereiten(rooms, by_id)
        umstellen = not aktiv and self.plan_aktiv and not erstentscheid
        if erstentscheid and not aktiv:
            _LOGGER.info("Erste Entscheidung der Heizperiode: Sommer, Thermostate bleiben unverändert")
        async with self.lock:
            await self.pause_setzen(not aktiv)
            for room in rooms:
                a = self.app(room.raum)
                alt = dict(a)
                a["sommer_aus_offen"] = umstellen and a["modus"] == "plan"
                cl = by_id.get(room.climate or "")
                try:
                    if cl is not None and self.raum_faehigkeiten(room, by_id)["steuerbar"]:
                        await self.sommer_abgleich(room, a, cl, self._plan.get(room.raum))
                except HAError as err:
                    _LOGGER.warning("Heizperiode %s: %s (neuer Versuch mit der Plananwendung)", room.raum, err)
                finally:
                    if a != alt:
                        await self.app_speichern(room, a)
        await self.heizperiode_spiegeln(aktiv, entitaet, by_id)
        if aktiv and self.plan_aktiv and self.plananwendung is not None:
            await self.plananwendung.takt(erzwingen=True)

    async def heizperiode_spiegeln(self, aktiv: bool, entitaet: str, by_id: dict[str, dict[str, Any]]) -> None:
        """Vorhandenen input_boolean als Spiegel der Heizperiode schalten (keine Voraussetzung)."""
        spiegel = by_id.get(entitaet)
        if not entitaet.startswith("input_boolean.") or spiegel is None:
            return
        if spiegel.get("state") == ("on" if aktiv else "off"):
            return
        try:
            await self.client.call_service("input_boolean", "turn_on" if aktiv else "turn_off", {"entity_id": entitaet})
        except HAError as err:
            _LOGGER.warning("Spiegel der Heizperiode %s nicht geschaltet: %s", entitaet, err)

    # ------------------------------------------------------------- Coach

    def ki_prompt(self, bericht: dict[str, Any]) -> str:
        return ki.prompt(bericht, ki.PROMPT_GENERISCH)
