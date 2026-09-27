"""aiohttp-Server (Ingress) mit JSON-API und statischem Frontend."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import web

from . import __version__
from . import schedule as sch
from . import steuerung as st
from .adapter import PmKlimaErkennung, adapter_fuer, hinweis_betriebsart
from .adapter.generisch import app_status
from .coach import Coach, CoachStore
from .config import DATA_DIR, Options, Room
from .data import RANGES, DataService
from .ha_client import HAClient, HAError
from .heizperiode import Heizperiode
from .report import ReportArchive, WeeklyScheduler, build_report

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INGRESS_IP = "172.30.32.2"
INGRESS_PATH_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_\-]+$")
ROOM_TTL = 300
OVERVIEW_TTL = 120
REGISTRY_CACHE_MAX = 32
EREIGNIS_TYP_RE = re.compile(r"^[a-z_]{1,32}$")
TZ_RETRY = (10, 30, 60, 120, 300)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'"
)

K_APP: web.AppKey[KlimaStudio] = web.AppKey("klimastudio")


def allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = os.environ.get("PMKS_ALLOWED_IPS", INGRESS_IP)
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def make_ingress_filter(networks: list[Any]):
    @web.middleware
    async def ingress_filter(request: web.Request, handler):
        remote = request.remote or ""
        try:
            addr = ipaddress.ip_address(remote)
        except ValueError:
            addr = None
        if addr is None or not any(addr in net for net in networks):
            _LOGGER.warning("Zugriff von %s abgewiesen (nur Ingress erlaubt)", remote)
            raise web.HTTPForbidden(text="Nur über Home-Assistant-Ingress erreichbar.")
        return await handler(request)

    return ingress_filter


def _setze_header(request: web.Request, headers: Any) -> None:
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        headers.setdefault("Cache-Control", "no-store")
    elif request.path.startswith("/static/") and request.path.endswith((".js", ".css")):
        # Immer beim Server nachfragen (304 bei unveränderter Datei), damit nie eine veraltete Oberfläche läuft
        headers.setdefault("Cache-Control", "no-cache")


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as exc:  # auch Fehlerantworten (400/404/409 …) absichern
        _setze_header(request, exc.headers)
        raise
    _setze_header(request, resp.headers)
    return resp


@web.middleware
async def error_json(request: web.Request, handler):
    try:
        return await handler(request)
    except HAError as err:
        _LOGGER.warning("%s %s: %s", request.method, request.path, err)
        return web.json_response({"fehler": str(err), "code": err.code}, status=502)


def ingress_base(request: web.Request) -> str:
    path = request.headers.get("X-Ingress-Path", "")
    if path and INGRESS_PATH_RE.match(path):
        return path + "/"
    return "/"


class KlimaStudio:
    """Anwendungszustand."""

    def __init__(self, opts: Options, client: HAClient, data_dir: Path = DATA_DIR, wissen_pfad: Path | None = None) -> None:
        self.opts = opts
        self.client = client
        self.store = CoachStore(data_dir / "coach.db")
        self.adapter = adapter_fuer(opts, client, self.store)
        self.adapter.tz = lambda: self.tz
        self.adapter.raeume_quelle = self.rooms
        # Nur Betriebsart generisch: Hintergrund für Rück-Timer und (mit plan_anwenden) die Heizpläne
        self.plananwendung = self.adapter.plananwendung
        self.pm_erkennung = PmKlimaErkennung(client, ROOM_TTL)
        generisch = self.adapter.betriebsart == "generisch"
        if generisch:
            self.adapter.pm_erkennung = self.pm_erkennung  # type: ignore[attr-defined]
        self.data = DataService(client, generisch=generisch)
        self.archive = ReportArchive(data_dir / "berichte.json")
        self.tz = ZoneInfo("Europe/Berlin")
        self._rooms: list[Room] = []
        self._rooms_at = 0.0
        self._overview: dict[str, tuple[float, Any]] = {}
        self._registry_ids: dict[str, str] = {}
        self._report_lock = asyncio.Lock()
        self.scheduler: WeeklyScheduler | None = None
        self._tz_loaded = False
        self._tz_task: asyncio.Task | None = None
        self.heizperiode = Heizperiode(
            client, opts, self.store, tz=lambda: self.tz, bereit=lambda: self._tz_loaded, adapter=self.adapter
        )
        self.coach = Coach(self, self.store, wissen_pfad)

    # ------------------------------------------------------------- Start

    async def load_timezone(self) -> bool:
        try:
            cfg = await self.client.get_config()
            self.tz = ZoneInfo(cfg.get("time_zone") or "Europe/Berlin")
        except (HAError, ZoneInfoNotFoundError) as err:
            _LOGGER.warning("Zeitzone aus HA nicht lesbar (%s), nutze vorerst %s", err, self.tz)
            return False
        self._tz_loaded = True
        return True

    async def _tz_retry_loop(self) -> None:
        attempt = 0
        while not self._tz_loaded:
            await asyncio.sleep(TZ_RETRY[min(attempt, len(TZ_RETRY) - 1)])
            attempt += 1
            if await self.load_timezone() and self.scheduler:
                self.scheduler.set_tz(self.tz)
                _LOGGER.info("Zeitzone %s nachgeladen", self.tz)

    async def start(self) -> None:
        if not await self.load_timezone():
            self._tz_task = asyncio.create_task(self._tz_retry_loop())
        hour, minute = self.opts.bericht_hm
        self.scheduler = WeeklyScheduler(
            lambda: self.create_report(push=True, anlass="zeitplan"),
            self.opts.bericht_weekday,
            hour,
            minute,
            self.tz,
        )
        self.scheduler.start()
        self.heizperiode.start()
        if self.plananwendung is not None:
            self.plananwendung.start(bereit=lambda: self._tz_loaded)

    async def stop(self) -> None:
        if self._tz_task:
            self._tz_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tz_task
        if self.scheduler:
            await self.scheduler.stop()
        await self.heizperiode.stop()
        if self.plananwendung is not None:
            await self.plananwendung.stop()
        self.store.close()
        await self.client.close()

    # ------------------------------------------------------------- Räume

    async def rooms(self, refresh: bool = False) -> list[Room]:
        if refresh or not self._rooms or time.monotonic() - self._rooms_at > ROOM_TTL:
            states = await self.client.get_states()
            self._rooms = await self.adapter.raeume(states)
            self._rooms_at = time.monotonic()
            self._registry_ids.clear()  # Zuordnung Entität -> Speicher-ID neu ermitteln
        return self._rooms

    async def room(self, slug: str) -> Room:
        for room in await self.rooms():
            if room.raum == slug:
                return room
        raise web.HTTPNotFound(text=f"Raum {slug} unbekannt")

    # ------------------------------------------------------------- Heizplan

    async def _schedule_storage_id(self, entity_id: str) -> str:
        """Speicher-ID des schedule-Helfers aus der Entitätsregistrierung (keine Vermutung)."""
        if not entity_id.startswith("schedule."):
            raise web.HTTPConflict(text=f"{entity_id} ist kein schedule-Helfer.")
        cached = self._registry_ids.get(entity_id)
        if cached is not None:
            return cached
        try:
            entry = await self.client.entity_registry_get(entity_id)
        except HAError as err:
            if err.code == "not_found":
                raise web.HTTPConflict(text=f"{entity_id} ist nicht registriert und kann nicht bearbeitet werden.") from err
            raise HAError(f"Registrierung von {entity_id} nicht lesbar, Heizplan wird nicht bearbeitet: {err}", err.code) from err
        if entry.get("platform") != "schedule" or not entry.get("unique_id"):
            raise web.HTTPConflict(text=f"{entity_id} ist kein über die Oberfläche angelegter schedule-Helfer.")
        storage_id = str(entry["unique_id"])
        if len(self._registry_ids) >= REGISTRY_CACHE_MAX:
            self._registry_ids.pop(next(iter(self._registry_ids)))
        self._registry_ids[entity_id] = storage_id
        return storage_id

    async def schedule_item(self, room: Room) -> dict[str, Any]:
        if not room.schedule:
            raise web.HTTPNotFound(text=f"Für {room.name} ist kein Heizplan zugeordnet.")
        storage_id = await self._schedule_storage_id(room.schedule)
        items = await self.client.schedule_list()
        item = next((i for i in items if i.get("id") == storage_id), None)
        if item is None:
            self._registry_ids.pop(room.schedule, None)
            raise web.HTTPConflict(
                text=f"{room.schedule} ist kein über die Oberfläche angelegter Zeitplan und kann nicht bearbeitet werden."
            )
        return item

    # ------------------------------------------------------------- Bericht

    async def create_report(self, push: bool, anlass: str = "manuell", dienste: list[str] | None = None) -> dict[str, Any]:
        async with self._report_lock:
            end = datetime.now(UTC)
            start = end - RANGES["7d"]
            rooms = await self.rooms(refresh=True)
            analyses = []
            for room in rooms:
                analyses.append(await self.data.room_analysis(room, "7d", end=end, detail=False))
            # Schimmelrisiko nur mit Daten (PM Klima bzw. zugeordnete Schimmelsensoren)
            schimmel = self.adapter.faehigkeiten()["schimmel"] or any(r.schimmel for r in rooms)
            report = build_report(analyses, start, end, self.tz, anlass=anlass, schimmel=schimmel)
            targets = dienste if dienste is not None else self.opts.bericht_notify
            if push and targets:
                for svc in targets:
                    try:
                        await self.client.call_service(
                            "notify", svc, {"title": "Klima Studio · Wochenbericht", "message": report["push_text"]}
                        )
                        report["push"].append({"dienst": f"notify.{svc}", "ok": True})
                    except HAError as err:
                        _LOGGER.error("Push über notify.%s fehlgeschlagen: %s", svc, err)
                        report["push"].append({"dienst": f"notify.{svc}", "ok": False, "fehler": str(err)})
            self.archive.add(report)
            _LOGGER.info("Wochenbericht %s erstellt (%s)", report["id"], anlass)
            return report


# ------------------------------------------------------------------ Handler


def _ks(request: web.Request) -> KlimaStudio:
    return request.app[K_APP]


async def _json_body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="JSON erwartet")
    try:
        body = await request.json()
    except ValueError as err:
        raise web.HTTPBadRequest(text="Ungültiges JSON") from err
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Objekt erwartet")
    return body


def _json_pflicht(request: web.Request) -> None:
    """POST ohne Nutzdaten: Content-Type application/json verlangen (Schutz vor einfachen Formular-POSTs)."""
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="JSON erwartet")


async def index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__BASE__", ingress_base(request)).replace("__VERSION__", __version__)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def static_versioned(request: web.Request) -> web.FileResponse:
    """/static/<version>/<datei>: versionierter Pfad gegen Browser-Caches, die die Query ignorieren.

    Jede Versionsangabe liefert die aktuelle Datei; der Pfad darf STATIC_DIR nicht verlassen.
    """
    base = STATIC_DIR.resolve()
    try:
        path = (base / request.match_info["datei"]).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise web.HTTPNotFound() from None
    if not path.is_relative_to(base) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def api_info(request: web.Request) -> web.Response:
    ks = _ks(request)
    rooms = await ks.rooms()
    if ks.client.ha_version is None:
        with contextlib.suppress(HAError):
            await ks.client.schedule_list()
    nxt = ks.scheduler.next.isoformat(timespec="minutes") if ks.scheduler and ks.scheduler.next else None
    geladen = await ks.pm_erkennung.geladen()
    extra: dict[str, Any] = {}
    if ks.adapter.betriebsart == "generisch":
        extra["absenktemperatur"] = ks.opts.absenktemperatur
    return web.json_response(
        {
            "version": __version__,
            "ha_version": ks.client.ha_version,
            "zeitzone": str(ks.tz),
            "raeume": [r.to_dict() for r in rooms],
            "bericht": {
                "tag": ks.opts.bericht_tag,
                "uhrzeit": ks.opts.bericht_uhrzeit,
                "notify": [f"notify.{n}" for n in ks.opts.bericht_notify],
                "naechster": nxt,
            },
            "coach": {"ki_entitaet": ks.opts.coach_ki_entitaet, "anwesenheit": ks.opts.coach_anwesenheit},
            "betriebsart": ks.adapter.betriebsart,
            "pm_klima_geladen": geladen,
            "faehigkeiten": ks.adapter.faehigkeiten(),
            "hinweis_betriebsart": hinweis_betriebsart(ks.adapter.betriebsart, geladen),
            **extra,
        }
    )


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def api_current(request: web.Request) -> web.Response:
    ks = _ks(request)
    rooms = await ks.rooms()
    result = await ks.data.current(
        rooms,
        {"empfehlung": ks.opts.empfehlung, "aussentemperatur": ks.opts.aussentemperatur, "aussenfeuchte": ks.opts.aussenfeuchte},
    )
    if ks.adapter.betriebsart == "generisch":
        # nur generisch: App-Modus (plan | hand | aus), Plan-Soll und Status (Timer, Sommer-Pause) je Raum
        by_id = {s["entity_id"]: s for s in await ks.client.get_states() if "entity_id" in s}
        await ks.adapter.vorbereiten(rooms, by_id)
        for room in rooms:
            z = result["raeume"].get(room.raum)
            if z is not None:
                zustand = ks.adapter.raum_zustand(room, by_id)
                z["app_modus"] = zustand["modus"]
                z["plan_soll"] = zustand["plan_soll"]
                z["app_status"], z["status_bis"] = app_status(zustand)
        result["sommerpause"] = ks.adapter.pause
    return web.json_response(result)


def _range(request: web.Request) -> str:
    rng = request.query.get("bereich", "24h")
    if rng not in RANGES:
        raise web.HTTPBadRequest(text="bereich muss 24h, 7d oder 30d sein")
    return rng


async def api_analysis(request: web.Request) -> web.Response:
    ks = _ks(request)
    room = await ks.room(request.match_info["raum"])
    return web.json_response(await ks.data.room_analysis(room, _range(request)))


async def api_overview(request: web.Request) -> web.Response:
    ks = _ks(request)
    rng = _range(request)
    cached = ks._overview.get(rng)
    if cached and time.monotonic() - cached[0] < OVERVIEW_TTL:
        return web.json_response(cached[1])
    rooms = await ks.rooms()
    end = datetime.now(UTC)
    tiles = []
    for room in rooms:
        a = await ks.data.room_analysis(room, rng, end=end, detail=False)
        tiles.append(a)
    result = {"bereich": rng, "raeume": tiles, "erstellt": int(end.timestamp() * 1000)}
    ks._overview[rng] = (time.monotonic(), result)
    return web.json_response(result)


async def api_schedule_get(request: web.Request) -> web.Response:
    ks = _ks(request)
    room = await ks.room(request.match_info["raum"])
    item = await ks.schedule_item(room)
    return web.json_response(
        {
            "raum": room.raum,
            "entity_id": room.schedule,
            "id": item["id"],
            "name": item.get("name"),
            "icon": item.get("icon"),
            "tage": sch.ha_to_ui(item),
            "revision": sch.revision(item),
        }
    )


def _validate(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return sch.validate_days(body.get("tage"), allow_empty=body.get("leer_bestaetigt") is True)
    except sch.EmptyScheduleError as err:
        raise web.HTTPUnprocessableEntity(
            text=json.dumps({"fehler": "Heizplan leer", "code": "leer", "details": err.errors}, ensure_ascii=False),
            content_type="application/json",
        ) from err
    except sch.ScheduleError as err:
        raise web.HTTPUnprocessableEntity(
            text=json.dumps({"fehler": "Heizplan ungültig", "details": err.errors}, ensure_ascii=False),
            content_type="application/json",
        ) from err


async def api_schedule_preview(request: web.Request) -> web.Response:
    ks = _ks(request)
    room = await ks.room(request.match_info["raum"])
    body = await _json_body(request)
    days = _validate(body)
    item = await ks.schedule_item(room)
    current = sch.ha_to_ui(item)
    rev = sch.revision(item)
    return web.json_response(
        {
            "diff": sch.diff_days(current, days),
            "konflikt": bool(body.get("revision")) and body.get("revision") != rev,
            "revision_aktuell": rev,
        }
    )


async def api_schedule_save(request: web.Request) -> web.Response:
    ks = _ks(request)
    room = await ks.room(request.match_info["raum"])
    body = await _json_body(request)
    days = _validate(body)
    item = await ks.schedule_item(room)  # erneut lesen, unmittelbar vor dem Schreiben
    rev = sch.revision(item)
    if body.get("revision") != rev and not body.get("erzwingen"):
        return web.json_response(
            {
                "fehler": "Der Heizplan wurde inzwischen außerhalb von Klima Studio geändert.",
                "konflikt": True,
                "revision_aktuell": rev,
                "tage_aktuell": sch.ha_to_ui(item),
                "diff": sch.diff_days(sch.ha_to_ui(item), days),
            },
            status=409,
        )
    msg = sch.build_update_message(item, days)
    result = await ks.client.schedule_update(msg)
    _LOGGER.info("Heizplan %s gespeichert", item["id"])
    ks._overview.clear()
    ks.adapter.plan_geaendert()
    return web.json_response(
        {"ok": True, "tage": sch.ha_to_ui(result), "revision": sch.revision(result), "name": result.get("name")}
    )


async def api_reports(request: web.Request) -> web.Response:
    ks = _ks(request)
    items = ks.archive.load()
    return web.json_response(
        [
            {k: r.get(k) for k in ("id", "erstellt", "anlass", "zeitraum", "einleitung", "push")}
            | {"auffaellig": len(r.get("auffaellig") or [])}
            for r in items
        ]
    )


async def api_report_get(request: web.Request) -> web.Response:
    report = _ks(request).archive.get(request.match_info["id"])
    if report is None:
        raise web.HTTPNotFound(text="Bericht nicht gefunden")
    return web.json_response(report)


async def api_report_create(request: web.Request) -> web.Response:
    ks = _ks(request)
    body = await _json_body(request)
    push = bool(body.get("push"))
    report = await ks.create_report(push=push, anlass="manuell")
    return web.json_response(report)


# ------------------------------------------------------------------ Steuerung


async def api_steuerung(request: web.Request) -> web.Response:
    ks = _ks(request)
    states = await ks.client.get_states()
    by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
    rooms = await ks.rooms()
    await ks.adapter.vorbereiten(rooms, by_id)
    try:
        heizperiode = await ks.heizperiode.status(states)
    except (sqlite3.Error, HAError) as err:  # Raumsteuerung bleibt nutzbar
        _LOGGER.warning("Status der Heizperiode nicht verfügbar: %s", err)
        heizperiode = None
    return web.json_response(
        {
            "raeume": {r.raum: ks.adapter.raum_zustand(r, by_id) for r in rooms},
            "integration": ks.adapter.integration_status(by_id),
            "heizperiode": heizperiode,
        }
    )


async def api_steuerung_raum(request: web.Request) -> web.Response:
    ks = _ks(request)
    slug = st.pruefe_raum_slug(request.match_info["raum"])
    room = await ks.room(slug)
    ks.adapter.pruefe_steuerbar(room)
    body = await _json_body(request)
    befehl = await ks.adapter.aktion(room, body)
    _LOGGER.info("Steuerung %s: %s", room.raum, befehl["text"])
    await ks.store.ereignis(
        "steuerung",
        room.raum,
        f"{room.name}: {befehl['text']}",
        {"aktion": befehl["aktion"], "dienst": f"{befehl['domain']}.{befehl['service']}", **befehl["daten"]},
    )
    ks.coach.invalidieren()
    by_id = {s["entity_id"]: s for s in await ks.client.get_states() if "entity_id" in s}
    return web.json_response({"ok": True, "raum": ks.adapter.raum_zustand(room, by_id)})


async def api_heizperiode_get(request: web.Request) -> web.Response:
    return web.json_response(await _ks(request).heizperiode.status())


async def api_heizperiode_post(request: web.Request) -> web.Response:
    ks = _ks(request)
    body = await _json_body(request)
    result = await ks.heizperiode.einstellen(body)
    ks.coach.invalidieren()
    return web.json_response(result)


async def api_heizperiode_einrichten(request: web.Request) -> web.Response:
    _json_pflicht(request)
    ks = _ks(request)
    result = await ks.heizperiode.einrichten()
    ks.coach.invalidieren()
    return web.json_response(result)


# ------------------------------------------------------------------ Coach


async def api_coach(request: web.Request) -> web.Response:
    return web.json_response(await _ks(request).coach.uebersicht())


async def api_coach_rueckmeldung(request: web.Request) -> web.Response:
    body = await _json_body(request)
    ks = _ks(request)
    await ks.coach.rueckmeldung(body)
    ks.coach.invalidieren()
    return web.json_response({"ok": True})


async def api_coach_lagebericht(request: web.Request) -> web.Response:
    return web.json_response(await _ks(request).coach.lagebericht_antwort())


async def api_coach_ki_post(request: web.Request) -> web.Response:
    _json_pflicht(request)
    return web.json_response(await _ks(request).coach.ki_lauf())


async def api_coach_ki_liste(request: web.Request) -> web.Response:
    return web.json_response(await _ks(request).coach.ki_laeufe())


async def api_coach_ki_get(request: web.Request) -> web.Response:
    return web.json_response(await _ks(request).coach.ki_lauf_lesen(request.match_info["id"]))


async def api_ereignisse(request: web.Request) -> web.Response:
    typ = request.query.get("typ") or None
    if typ is not None and not EREIGNIS_TYP_RE.match(typ):
        raise st.json_fehler(web.HTTPBadRequest, "Ungültiger Ereignistyp.", "ungueltig")
    return web.json_response(await _ks(request).store.ereignisse(typ, 100))


# ------------------------------------------------------------------ Aufbau


def create_app(ks: KlimaStudio, networks: list[Any] | None = None) -> web.Application:
    app = web.Application(
        middlewares=[make_ingress_filter(networks or allowed_networks()), security_headers, error_json],
        client_max_size=512 * 1024,
    )
    app[K_APP] = ks
    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/api/health", api_health)
    r.add_get("/api/info", api_info)
    r.add_get("/api/aktuell", api_current)
    r.add_get("/api/uebersicht", api_overview)
    r.add_get("/api/auswertung/{raum}", api_analysis)
    r.add_get("/api/heizplan/{raum}", api_schedule_get)
    r.add_post("/api/heizplan/{raum}/vorschau", api_schedule_preview)
    r.add_post("/api/heizplan/{raum}", api_schedule_save)
    r.add_get("/api/berichte", api_reports)
    r.add_post("/api/berichte", api_report_create)
    r.add_get("/api/berichte/{id}", api_report_get)
    r.add_get("/api/steuerung", api_steuerung)
    r.add_post("/api/steuerung/{raum}", api_steuerung_raum)
    r.add_get("/api/heizperiode", api_heizperiode_get)
    r.add_post("/api/heizperiode", api_heizperiode_post)
    r.add_post("/api/heizperiode/einrichten", api_heizperiode_einrichten)
    r.add_get("/api/coach", api_coach)
    r.add_post("/api/coach/rueckmeldung", api_coach_rueckmeldung)
    r.add_get("/api/coach/lagebericht", api_coach_lagebericht)
    r.add_get("/api/coach/ki", api_coach_ki_liste)
    r.add_post("/api/coach/ki", api_coach_ki_post)
    r.add_get("/api/coach/ki/{id}", api_coach_ki_get)
    r.add_get("/api/ereignisse", api_ereignisse)
    r.add_get(r"/static/{version:\d+(?:\.\d+)+[0-9A-Za-z.+-]*}/{datei:.+}", static_versioned)
    r.add_static("/static", STATIC_DIR, show_index=False, append_version=False)
    return app


async def run(port: int = 8099) -> None:
    opts = Options.load()
    logging.getLogger().setLevel(opts.log_level.upper())
    timeout = aiohttp.ClientTimeout(total=None, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = HAClient(session)
        ks = KlimaStudio(opts, client)
        await ks.start()
        app = create_app(ks)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)  # noqa: S104 - Ingress im Container-Netz
        await site.start()
        _LOGGER.info("PM Klima Studio %s lauscht auf Port %s", __version__, port)
        try:
            await asyncio.Event().wait()
        finally:
            await ks.stop()
            await runner.cleanup()
