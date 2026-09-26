"""Nachbildung der benötigten Home-Assistant-Schnittstellen für Tests und Vorführung.

Pfade wie hinter dem Supervisor-Proxy:
  REST  /core/api/states, /core/api/config, /core/api/services/<domain>/<service>
  WS    /core/websocket   (auth_required -> auth -> auth_ok, danach Befehle mit id)

Nachrichtenformate entsprechen HA 2026.9:
  schedule/list, schedule/update (helpers/collection.py, components/schedule/__init__.py),
  history/history_during_period (components/history/websocket_api.py, komprimiert s/a/lu),
  recorder/statistics_during_period (components/recorder/websocket_api.py, start in ms),
  config/entity_registry/get, input_boolean/create,
  Dienste pm_heizung.* / climate.* mit Zustandsänderung, weather.get_forecasts und
  ai_task.generate_data (nur mit Antwort: REST ``?return_response`` -> ``{"changed_states", "service_response"}``,
  WS ``call_service`` mit ``return_response`` -> ``{"context", "response"}``).

Start eigenständig:  python tests/fake_ha.py --port 8123
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import WSMsgType, web

TOKEN = "test-token"
TZ = ZoneInfo("Europe/Berlin")
STEP = 300  # 5 Minuten
DAYS_BACK = 35
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

ROOMS = {
    "wohnzimmer": {"name": "Wohnzimmer", "feuchte": "sensor.wohnzimmerluftfeuchte", "fenster": True, "co2": True},
    "kuche": {"name": "Küche", "feuchte": "sensor.kuche_luftfeuchtigkeit", "fenster": True, "co2": False},
    "schlafzimmer": {"name": "Schlafzimmer", "feuchte": "sensor.schlafzimmer_luftfeuchtigkeit", "fenster": False, "co2": False},
    "badezimmer": {"name": "Badezimmer", "feuchte": "sensor.badezimmer_luftfeuchtigkeit", "fenster": False, "co2": False},
}


def default_schedules() -> dict[str, dict[str, Any]]:
    def blocks(*spec):
        return [{"from": f, "to": t, "data": {"temperatur": temp}} for f, t, temp in spec]

    wk_wz = blocks(("06:00:00", "08:30:00", 21), ("16:00:00", "22:30:00", 21))
    we_wz = blocks(("08:00:00", "23:00:00", 21))
    wk_k = blocks(("06:00:00", "08:30:00", 21), ("16:00:00", "22:00:00", 21))
    we_k = blocks(("08:00:00", "22:00:00", 21))
    sz = blocks(("06:00:00", "07:30:00", 20), ("21:00:00", "23:00:00", 20))
    wk_b = blocks(("05:00:00", "07:00:00", 25), ("18:00:00", "22:00:00", 22))
    we_b = blocks(("18:00:00", "22:00:00", 22))

    def item(slug, name, wk, we):
        out = {"id": f"heizplan_{slug}", "name": f"Heizplan {name}", "icon": "mdi:calendar-clock"}
        for d in DAYS:
            out[d] = copy.deepcopy(wk if d not in ("saturday", "sunday") else we)
        return out

    return {
        "heizplan_wohnzimmer": item("wohnzimmer", "Wohnzimmer", wk_wz, we_wz),
        "heizplan_kuche": item("kuche", "Küche", wk_k, we_k),
        "heizplan_schlafzimmer": item("schlafzimmer", "Schlafzimmer", sz, sz),
        "heizplan_badezimmer": item("badezimmer", "Badezimmer", wk_b, we_b),
    }


def _t(value: str) -> int:
    h, m, *_ = (int(x) for x in value.split(":"))
    return h * 60 + m


def schedule_temp(item: dict[str, Any], local: datetime) -> float | None:
    mins = local.hour * 60 + local.minute
    for rng in item.get(DAYS[local.weekday()], []):
        if _t(rng["from"]) <= mins < _t(rng["to"]):
            return float((rng.get("data") or {}).get("temperatur", 20))
    return None


class Simulation:
    """Deterministische Verläufe der letzten ``DAYS_BACK`` Tage im 5-Minuten-Raster."""

    def __init__(self, schedules: dict[str, dict[str, Any]], now: datetime | None = None, seed: int = 7) -> None:
        self.now = now or datetime.now(UTC)
        end = int(self.now.timestamp()) // STEP * STEP
        self.start = end - DAYS_BACK * 86400
        self.times = list(range(self.start, end + 1, STEP))
        rnd = random.Random(seed)
        self.series: dict[str, list[tuple[float, Any, dict[str, Any] | None]]] = {}
        outdoor = [8 + 5 * math.sin((t % 86400) / 86400 * 2 * math.pi - 2) + rnd.uniform(-0.3, 0.3) for t in self.times]
        self._emit_numeric("sensor.aussentemperatur", outdoor)
        self._emit_numeric(
            "sensor.aussenluftfeuchte", [80 - 10 * math.sin((t % 86400) / 86400 * 2 * math.pi - 2) for t in self.times]
        )

        for slug, cfg in ROOMS.items():
            item = schedules[f"heizplan_{slug}"]
            win = [self._window_open(slug, t) for t in self.times]
            ist, soll_l, action = 19.5, [], []
            ist_l = []
            heating = False
            hum_l, co2_l = [], []
            hum = 52.0
            co2 = 520.0
            for i, t in enumerate(self.times):
                local = datetime.fromtimestamp(t, TZ)
                soll = schedule_temp(item, local)
                target = soll if soll is not None else 17.0
                if win[i]:
                    heating = False
                elif ist < target - 0.3:
                    heating = True
                elif ist >= target + 0.2:
                    heating = False
                ist += (0.06 if heating else -0.015) + (-0.05 if win[i] else 0) + rnd.uniform(-0.01, 0.01)
                soll_l.append(soll if soll is not None else 17.0)
                ist_l.append(round(ist, 1))
                action.append("heating" if heating else "idle")
                # Feuchte
                base = 52 + 6 * math.sin((local.hour * 60 + local.minute) / 1440 * 2 * math.pi)
                if slug == "badezimmer" and local.hour == 6 and local.minute < 30:
                    hum = min(88, hum + 5)
                if slug == "kuche" and local.hour in (12, 18) and local.minute < 20:
                    hum = min(78, hum + 2)
                if slug == "wohnzimmer" and local.day % 5 == 0 and 20 <= local.hour < 23:
                    hum = min(74, hum + 0.6)
                hum += (base - hum) * 0.05
                if win[i]:
                    hum -= 1.4
                hum_l.append(round(max(30, hum), 1))
                # CO2 (Wohnzimmer)
                if cfg["co2"]:
                    occupied = 17 <= local.hour < 23 or (local.weekday() >= 5 and 9 <= local.hour < 23)
                    co2 += (18 if occupied else -6) * (1.4 if local.day % 3 == 0 else 1)
                    if win[i]:
                        co2 -= 90
                    co2 = min(1750, max(430, co2 + rnd.uniform(-5, 5)))
                    co2_l.append(round(co2))
            self._emit_climate(f"climate.pm_{slug}", cfg["name"], soll_l, ist_l, action, hum_l)
            self._emit_numeric(cfg["feuchte"], hum_l)
            if slug == "wohnzimmer":
                self._emit_numeric("sensor.wohnzimmer_luftfeuchtigkeit", [round(h - 8, 1) for h in hum_l])
            self._emit_numeric(f"sensor.pm_{slug}_schimmelrisiko", [round(min(100, max(0, h * 1.3 - 12)), 1) for h in hum_l])
            if cfg["fenster"]:
                self._emit_state(f"binary_sensor.pm_{slug}_fenster_offen", ["on" if w else "off" for w in win])
                raw = "binary_sensor.fenster_1_wohnzimmer" if slug == "wohnzimmer" else "binary_sensor.fenster_kuche"
                self._emit_state(raw, ["on" if w else "off" for w in win])
            if co2_l:
                self._emit_numeric("sensor.wetterstation_kohlendioxid", co2_l)

    @staticmethod
    def _window_open(slug: str, t: int) -> bool:
        local = datetime.fromtimestamp(t, TZ)
        mins = local.hour * 60 + local.minute
        if slug == "wohnzimmer":
            if 450 <= mins < 460 or 1140 <= mins < 1155:
                return True
            return local.weekday() == 5 and 840 <= mins < 935  # samstags 95 min
        if slug == "kuche":
            return 750 <= mins < 770
        return False

    def _emit_numeric(self, eid: str, values: list[float]) -> None:
        self._emit_state(eid, [str(v) for v in values])

    def _emit_state(self, eid: str, values: list[str]) -> None:
        rows: list[tuple[float, Any, dict[str, Any] | None]] = []
        for t, v in zip(self.times, values, strict=True):
            if rows and rows[-1][1] == v:
                continue
            rows.append((float(t), v, None))
        self.series[eid] = rows

    def _emit_climate(self, eid, name, soll, ist, action, hum) -> None:
        rows = []
        last = None
        for i, t in enumerate(self.times):
            attrs = {
                "hvac_action": action[i],
                "temperature": soll[i],
                "current_temperature": ist[i],
                "current_humidity": hum[i],
                "friendly_name": f"{name} Heizung",
            }
            key = (action[i], soll[i], ist[i], hum[i])
            if key == last:
                continue
            last = key
            rows.append((float(t), "auto", attrs))
        self.series[eid] = rows

    def last(self, eid: str) -> tuple[Any, dict[str, Any]]:
        rows = self.series.get(eid)
        if not rows:
            return None, {}
        return rows[-1][1], dict(rows[-1][2] or {})


KI_ANTWORT = json.dumps(
    {
        "zusammenfassung": "Die Räume sind überwiegend im Zielbereich. Im Badezimmer ist die Feuchte zeitweise hoch.",
        "tipps": [
            {
                "titel": "Bad nach dem Duschen lüften",
                "text": "Lüften Sie das Badezimmer nach dem Duschen etwa 10 Minuten.",
                "begruendung": "Die Feuchte lag mehrere Stunden über 70 %.",
                "prioritaet": 1,
                "raum": "badezimmer",
                "massnahme": {"typ": "lueften"},
            },
            {
                "titel": "Wohnzimmer abends etwas kühler",
                "text": "Senken Sie die Abendtemperatur im Wohnzimmer um 0,5 °C.",
                "begruendung": "Die Heizstunden sind hoch.",
                "prioritaet": 3,
                "raum": "wohnzimmer",
                "massnahme": {"typ": "steuerung", "temperatur": 20.5, "dauer": 60},
            },
        ],
    },
    ensure_ascii=False,
)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class FakeHA:
    def __init__(self, now: datetime | None = None) -> None:
        self.schedules = default_schedules()
        self.sim = Simulation(self.schedules, now)
        # Veränderlicher Zustand der climate.pm_*-Entitäten
        self.climate: dict[str, dict[str, Any]] = {
            slug: {
                "state": "auto",
                "overlay_bis": None,
                "boost_bis": None,
                "temperature": None,
                "anzeige": "Zeitplan",
                "grund": "zeitplan",
            }
            for slug in ROOMS
        }
        # Weitere Entitäten: entity_id -> (state, attributes)
        self.extra: dict[str, tuple[str, dict[str, Any]]] = {
            "switch.pm_heizung_aktiv": ("on", {"friendly_name": "PM Heizung aktiv"}),
            "sensor.pm_heizung_abwesenheitsphase": (
                "inaktiv",
                {
                    "gesperrt": False,
                    "sperre_grund": None,
                    "sperre_seit": None,
                    "sperre_wirkung": "aus",
                    "aktiv": False,
                    "beschreibung": "Keine Sperre aktiv",
                },
            ),
            "person.dominik": ("home", {"friendly_name": "Dominik"}),
            "person.gina_perina": ("not_home", {"friendly_name": "Gina Perina"}),
            "input_boolean.dominik_ist_zuhause": ("on", {}),
            "input_boolean.gina_ist_zuhause": ("off", {}),
            "weather.dwd_zuhause": ("cloudy", {"friendly_name": "DWD Zuhause", "temperature": 9.5}),
            "ai_task.claude_ai_task": ("unknown", {"friendly_name": "Claude AI Task"}),
        }
        self.forecast: list[dict[str, Any]] | None = None
        self.ki_antwort: Any = KI_ANTWORT
        self.ki_delay = 0.0
        self.ki_fehler = False
        # Langzeitstatistik ersetzen: entity_id -> Funktion(ts) -> Wert|None
        self.statistik: dict[str, Callable[[float], float | None]] = {}
        self.statistik_fehlt: set[str] = set()
        self.updates: list[dict[str, Any]] = []
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.ws_commands: list[dict[str, Any]] = []
        self.fail_notify: set[str] = set()
        self.fail_registry = False
        self.fail_config = 0  # Anzahl der nächsten /config-Aufrufe, die mit 502 scheitern
        self.app = web.Application()
        r = self.app.router
        r.add_get("/core/api/states", self.states)
        r.add_get("/core/api/config", self.config)
        r.add_post("/core/api/services/{domain}/{service}", self.service)
        r.add_get("/core/websocket", self.websocket)
        r.add_post("/_test/modify/{id}", self.modify)

    # ------------------------------------------------------------- REST

    def _auth(self, request: web.Request) -> None:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    def state_list(self) -> list[dict[str, Any]]:
        out = []

        def add(eid, state, attrs):
            out.append(
                {
                    "entity_id": eid,
                    "state": state,
                    "attributes": attrs,
                    "last_changed": self.sim.now.isoformat(),
                    "last_updated": self.sim.now.isoformat(),
                }
            )

        for slug, cfg in ROOMS.items():
            _, attrs = self.sim.last(f"climate.pm_{slug}")
            cl = self.climate[slug]
            attrs.update(
                {
                    "hvac_modes": ["auto", "heat", "off"],
                    "preset_mode": "zeitplan",
                    "anzeige": cl["anzeige"],
                    "grund": cl["grund"],
                    "min_temp": 5,
                    "max_temp": 25,
                    "target_temp_step": 0.5,
                    "zeitplan_temperatur": attrs.get("temperature"),
                    "overlay_bis": cl["overlay_bis"],
                    "boost_bis": cl["boost_bis"],
                    "fenster_offen": False,
                    "phase": "komfort",
                }
            )
            if cl["temperature"] is not None:
                attrs["temperature"] = cl["temperature"]
            add(f"climate.pm_{slug}", cl["state"], attrs)
            hum, _ = self.sim.last(cfg["feuchte"])
            add(
                cfg["feuchte"],
                hum,
                {
                    "device_class": "humidity",
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                    "friendly_name": f"{cfg['name']} Luftfeuchte",
                },
            )
            sch, _ = self.sim.last(f"sensor.pm_{slug}_schimmelrisiko")
            add(f"sensor.pm_{slug}_schimmelrisiko", sch, {"unit_of_measurement": "%", "state_class": "measurement"})
            lq = {"feuchte_innen": float(hum), "friendly_name": f"{cfg['name']} Luft Qualität"}
            if cfg["co2"]:
                co2, _ = self.sim.last("sensor.wetterstation_kohlendioxid")
                lq["co2"] = int(co2)
            add(f"sensor.pm_{slug}_luftqualitaet", "gut", lq)
            add(f"sensor.pm_{slug}_lueftdauer", "10", {"unit_of_measurement": "min"})
            add(f"binary_sensor.pm_{slug}_lueften_empfohlen", "off", {})
            add(f"schedule.heizplan_{slug}", "off", {"editable": True, "friendly_name": f"Heizplan {cfg['name']}"})
            if cfg["fenster"]:
                st, _ = self.sim.last(f"binary_sensor.pm_{slug}_fenster_offen")
                add(f"binary_sensor.pm_{slug}_fenster_offen", st, {"device_class": "window"})
        if "sensor.wohnzimmer_luftfeuchtigkeit" in self.sim.series:
            v, _ = self.sim.last("sensor.wohnzimmer_luftfeuchtigkeit")
            add("sensor.wohnzimmer_luftfeuchtigkeit", v, {"device_class": "humidity", "unit_of_measurement": "%"})
        for eid in ("binary_sensor.fenster_1_wohnzimmer", "binary_sensor.fenster_kuche"):
            v, _ = self.sim.last(eid)
            add(eid, v, {"device_class": "window"})
        co2, _ = self.sim.last("sensor.wetterstation_kohlendioxid")
        add("sensor.wetterstation_kohlendioxid", co2, {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm"})
        for eid in ("sensor.aussentemperatur", "sensor.aussenluftfeuchte"):
            v, _ = self.sim.last(eid)
            add(eid, v, {})
        add(
            "sensor.pm_klima_empfehlung",
            "Wohnzimmer: Wandfeuchte erhöht (76 %)",
            {
                "liste": [
                    {
                        "raum": "Wohnzimmer",
                        "thema": "schimmel_warnung",
                        "prioritaet": 11,
                        "text": "Wohnzimmer: Wandfeuchte erhöht (76 %)",
                    },
                    {
                        "raum": "Badezimmer",
                        "thema": "lueften",
                        "prioritaet": 20,
                        "text": "Badezimmer: Lüften empfohlen, etwa 10 Minuten",
                    },
                ],
                "beratung_aktiv": True,
            },
        )
        for eid, (state, attrs) in self.extra.items():
            add(eid, state, dict(attrs))
        return out

    async def states(self, request: web.Request) -> web.Response:
        self._auth(request)
        return web.json_response(self.state_list())

    async def config(self, request: web.Request) -> web.Response:
        self._auth(request)
        if self.fail_config > 0:
            self.fail_config -= 1
            return web.Response(status=502, text="Bad Gateway")
        return web.json_response({"time_zone": "Europe/Berlin", "version": "2026.9.3", "language": "de"})

    async def service(self, request: web.Request) -> web.Response:
        self._auth(request)
        domain, service = request.match_info["domain"], request.match_info["service"]
        data = await request.json()
        if domain == "notify" and service in self.fail_notify:
            return web.json_response({"message": f"Service {domain}.{service} not found."}, status=400)
        status, body = await self._dienst(domain, service, data, "return_response" in request.query)
        if status != 200:
            return web.json_response({"message": body}, status=status)
        if "return_response" in request.query:
            return web.json_response({"changed_states": [], "service_response": body})
        return web.json_response([])

    async def _dienst(self, domain: str, service: str, data: dict[str, Any], wants: bool) -> tuple[int, Any]:
        """Dienstaufruf (REST und WS). Rückgabe ``(status, response | Fehlermeldung)``."""
        self.service_calls.append((domain, service, data))
        if domain in ("weather", "ai_task"):
            if not wants:
                return 400, "Service call requires responses but caller did not ask for responses"
            if domain == "weather":
                eid = data.get("entity_id")
                if eid not in self.extra:
                    return 400, f"Entity {eid} not found"
                return 200, {eid: {"forecast": self._forecast()}}
            if self.ki_delay:
                await asyncio.sleep(self.ki_delay)
            if self.ki_fehler:
                return 500, self.ki_fehler if isinstance(self.ki_fehler, str) else "AI task failed"
            return 200, {"conversation_id": "abc", "data": self.ki_antwort}
        if wants:
            return 400, "Service does not support responses. Remove return_response from request."
        self._apply_service(domain, service, data)
        return 200, None

    def _forecast(self) -> list[dict[str, Any]]:
        if self.forecast is not None:
            return self.forecast
        base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            {
                "datetime": (base + timedelta(days=i)).astimezone(UTC).isoformat(),
                "condition": "rainy" if i == 1 else "cloudy",
                "temperature": 10.0 + i,
                "templow": 2.0 + i,
                "precipitation": 3.2 if i == 1 else 0.0,
                "precipitation_probability": 80 if i == 1 else 10,
            }
            for i in range(6)
        ]

    def _apply_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Wirkung der Dienste auf den Zustand (vereinfacht wie pm_heizung)."""
        eid = data.get("entity_id")
        if domain == "input_boolean" and service in ("turn_on", "turn_off") and eid in self.extra:
            state, attrs = self.extra[eid]
            if state == "unavailable":  # wie HA: Dienstaufruf ohne Wirkung
                return
            self.extra[eid] = ("on" if service == "turn_on" else "off", attrs)
            return
        if not isinstance(eid, str) or not eid.startswith("climate.pm_"):
            return
        cl = self.climate.get(eid[len("climate.pm_") :])
        if cl is None:
            return
        now = datetime.now(UTC)
        if domain == "climate" and service == "set_hvac_mode":
            cl.update(state=data["hvac_mode"], overlay_bis=None, boost_bis=None, temperature=None)
            cl.update(grund={"auto": "zeitplan", "heat": "manuell", "off": "aus"}[data["hvac_mode"]])
        elif domain == "climate" and service == "set_temperature":
            cl["temperature"] = data["temperature"]
        elif domain == "pm_heizung" and service == "set_overlay":
            if cl["state"] == "auto":
                dauer = data.get("dauer", 120)
                cl["overlay_bis"] = "dauerhaft" if dauer == 0 else (now + timedelta(minutes=dauer)).isoformat()
                cl.update(temperature=data["temperatur"], grund="overlay", anzeige=f"Zeitweise {data['temperatur']} °C")
            else:
                if cl["state"] == "off":
                    cl["state"] = "heat"
                cl["temperature"] = data["temperatur"]
        elif domain == "pm_heizung" and service == "boost":
            cl.update(boost_bis=(now + timedelta(minutes=data["dauer"])).isoformat(), temperature=25, grund="boost")
        elif domain == "pm_heizung" and service == "clear_overlay":
            cl.update(overlay_bis=None, boost_bis=None, temperature=None, grund="zeitplan", anzeige="Zeitplan")

    async def modify(self, request: web.Request) -> web.Response:
        """Testhilfe: Fremdänderung simulieren."""
        item = self.schedules[request.match_info["id"]]
        item["monday"] = [{"from": "05:00:00", "to": "06:00:00", "data": {"temperatur": 19}}, *item["monday"]]
        return web.json_response(item)

    # ------------------------------------------------------------- WebSocket

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.9.3"})
        msg = await ws.receive_json()
        if msg.get("type") != "auth" or msg.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.3"})
        async for raw in ws:
            if raw.type != WSMsgType.TEXT:
                continue
            msg = raw.json()
            self.ws_commands.append(msg)
            if msg.get("type") == "test/slow":
                asyncio.get_running_loop().create_task(self._slow_reply(ws, msg))
                continue
            if msg.get("type") == "call_service":
                asyncio.get_running_loop().create_task(self._ws_call_service(ws, msg))
                continue
            try:
                result = self.handle(msg)
                await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": result})
            except KeyError as err:
                await ws.send_json(
                    {"id": msg.get("id"), "type": "result", "success": False, "error": {"code": "not_found", "message": str(err)}}
                )
            except ValueError as err:
                await ws.send_json(
                    {
                        "id": msg.get("id"),
                        "type": "result",
                        "success": False,
                        "error": {"code": "invalid_format", "message": str(err)},
                    }
                )
        return ws

    async def _ws_call_service(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """WS ``call_service`` (websocket_api/commands.py): Ergebnis ``{"context", "response"}``."""
        status, body = await self._dienst(
            msg["domain"], msg["service"], dict(msg.get("service_data") or {}), bool(msg.get("return_response"))
        )
        if ws.closed:
            return
        if status != 200:
            code = "service_validation_error" if status == 400 else "home_assistant_error"
            await ws.send_json({"id": msg["id"], "type": "result", "success": False, "error": {"code": code, "message": body}})
            return
        result: dict[str, Any] = {"context": {"id": "01TEST", "parent_id": None, "user_id": None}}
        if msg.get("return_response"):
            result["response"] = body
        await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": result})

    async def _slow_reply(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        await asyncio.sleep(msg.get("delay", 0.3))
        if not ws.closed:
            await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": "langsam"})

    def handle(self, msg: dict[str, Any]) -> Any:
        typ = msg.get("type")
        if typ == "schedule/list":
            return [copy.deepcopy(i) for i in self.schedules.values()]
        if typ == "schedule/update":
            return self._schedule_update(msg)
        if typ == "config/entity_registry/get":
            if self.fail_registry:
                raise ValueError("registry temporarily unavailable")
            eid = msg["entity_id"]
            if eid.startswith("schedule."):
                return {"entity_id": eid, "platform": "schedule", "unique_id": eid.split(".", 1)[1]}
            return {"entity_id": eid, "platform": "pm_heizung", "unique_id": eid}
        if typ == "input_boolean/create":
            item_id = _slugify(msg["name"])
            if f"input_boolean.{item_id}" in self.extra:
                item_id += "_2"
            self.extra[f"input_boolean.{item_id}"] = ("off", {"friendly_name": msg["name"], "icon": msg.get("icon")})
            return {"id": item_id, "name": msg["name"], "icon": msg.get("icon")}
        if typ == "history/history_during_period":
            return self._history(msg)
        if typ == "recorder/statistics_during_period":
            return self._statistics(msg)
        raise ValueError(f"Unknown command {typ}")

    def _schedule_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        data = dict(msg)
        data.pop("id")
        data.pop("type")
        item_id = data.pop("schedule_id")
        if item_id not in self.schedules:
            raise KeyError(f"Unable to find schedule_id {item_id}")
        if not data.get("name"):
            raise ValueError("required key not provided @ data['name']")
        allowed = {"name", "icon", *DAYS}
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"extra keys not allowed @ data['{sorted(extra)[0]}']")
        new: dict[str, Any] = {"id": item_id, "name": data["name"]}
        if "icon" in data:
            new["icon"] = data["icon"]
        for day in DAYS:
            ranges = data.get(day, [])
            prev_to = None
            for rng in sorted(ranges, key=lambda r: r["from"]):
                if rng["from"] >= rng["to"]:
                    raise ValueError(f"Invalid time range, from {rng['from']} is after {rng['to']}")
                if prev_to is not None and prev_to > rng["from"]:
                    raise ValueError("Overlapping times found in schedule")
                for v in (rng.get("data") or {}).values():
                    if not isinstance(v, bool | str | int | float):
                        raise ValueError("invalid custom data")
                prev_to = rng["to"]
            new[day] = [{"from": r["from"], "to": r["to"], **({"data": r["data"]} if r.get("data") else {})} for r in ranges]
        self.schedules[item_id] = new
        self.updates.append(copy.deepcopy(msg))
        return copy.deepcopy(new)

    def _history(self, msg: dict[str, Any]) -> dict[str, Any]:
        start = datetime.fromisoformat(msg["start_time"]).timestamp()
        end = datetime.fromisoformat(msg["end_time"]).timestamp() if msg.get("end_time") else self.sim.now.timestamp()
        minimal = msg.get("minimal_response", False)
        no_attr = msg.get("no_attributes", False)
        out = {}
        for eid in msg["entity_ids"]:
            rows = self.sim.series.get(eid)
            if not rows:
                continue
            result = []
            prior = None
            for ts, state, attrs in rows:
                if ts <= start:
                    prior = (ts, state, attrs)
                    continue
                if ts > end:
                    break
                result.append((ts, state, attrs))
            if prior and msg.get("include_start_time_state", True):
                result.insert(0, (start, prior[1], prior[2]))
            comp = []
            for i, (ts, state, attrs) in enumerate(result):
                row: dict[str, Any] = {"s": state, "lu": ts}
                if not no_attr and (not minimal or i == 0 or eid.startswith("climate.")):
                    row["a"] = attrs or {}
                comp.append(row)
            if comp:
                out[eid] = comp
        return out

    def _statistics(self, msg: dict[str, Any]) -> dict[str, Any]:
        start = datetime.fromisoformat(msg["start_time"]).timestamp()
        end = datetime.fromisoformat(msg["end_time"]).timestamp() if msg.get("end_time") else self.sim.now.timestamp()
        out = {}
        for eid in msg["statistic_ids"]:
            if eid in self.statistik_fehlt:
                continue
            if eid in self.statistik:
                fn = self.statistik[eid]
                h = int(start // 3600 * 3600)
                vals = []
                while h < end:
                    v = fn(h)
                    if v is not None:
                        vals.append({"start": h * 1000.0, "end": (h + 3600) * 1000.0, "mean": v, "min": v, "max": v})
                    h += 3600
                out[eid] = vals
                continue
            rows = self.sim.series.get(eid)
            if not rows or eid.startswith(("climate.", "binary_sensor.")):
                continue
            hours: dict[int, list[float]] = {}
            idx = 0
            for t in self.sim.times:
                while idx + 1 < len(rows) and rows[idx + 1][0] <= t:
                    idx += 1
                if start <= t < end:
                    hours.setdefault(int(t // 3600 * 3600), []).append(float(rows[idx][1]))
            out[eid] = [
                {"start": h * 1000.0, "end": (h + 3600) * 1000.0, "mean": sum(v) / len(v), "min": min(v), "max": max(v)}
                for h, v in sorted(hours.items())
            ]
        return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    fake = FakeHA()
    web.run_app(fake.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
