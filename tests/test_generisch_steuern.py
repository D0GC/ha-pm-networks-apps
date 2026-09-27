"""Betriebsart generisch, Phase 3: Aktionen, Rück-Timer, Plananwendung, Heizperiode (intern)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from conftest import WISSEN_TEST
from fake_ha import TZ, DienstFehler, FakeHA
from klimastudio.coach import store as store_mod
from klimastudio.coach.store import CoachStore
from klimastudio.config import Options, Room
from klimastudio.plananwendung import block_jetzt, naechster_wechsel, plan_info
from klimastudio.server import KlimaStudio

T0 = datetime(2026, 1, 14, 11, 0, tzinfo=UTC)  # Mittwoch 12:00 Ortszeit: kein Planblock (Absenkung)
WZ, KU, GZ = "climate.wohnzimmer_thermostat", "climate.kueche_heizkoerper", "climate.gaestezimmer_heizung"


class Uhr:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def weiter(self, **kw) -> None:
        self.t += timedelta(**kw)

    def lokal(self, stunde: int, minute: int = 0) -> None:
        self.t = self.t.astimezone(TZ).replace(hour=stunde, minute=minute).astimezone(UTC)


@pytest.fixture
def uhr(studio_generisch, fake_generisch) -> Uhr:
    u = Uhr(T0)
    studio_generisch.adapter.uhr = u
    fake_generisch.uhr = u
    return u


def rufe(fake: FakeHA, service: str | None = None, eid: str | None = None) -> list[tuple[str, str, dict]]:
    return [
        c
        for c in fake.service_calls
        if c[0] == "climate" and (service is None or c[1] == service) and (eid is None or c[2].get("entity_id") == eid)
    ]


def soll(fake: FakeHA, eid: str) -> float | None:
    return fake.generisch[eid]["attributes"].get("temperature")


async def post(client, raum: str, body: dict) -> tuple[int, dict]:
    resp = await client.post(f"/api/steuerung/{raum}", json=body)
    return resp.status, await resp.json()


async def takt(ks: KlimaStudio, **kw) -> None:
    assert ks.plananwendung is not None
    await ks.plananwendung.takt(**kw)


# ------------------------------------------------------------------ Planauswertung (rein)


def test_block_und_naechster_wechsel():
    item = {"wednesday": [{"from": "06:00:00", "to": "08:30:00", "data": {"temperatur": 21.5}}], "thursday": []}
    lokal = datetime(2026, 1, 14, 7, 0, tzinfo=TZ)
    assert block_jetzt(item, lokal) == (True, 21.5)
    assert block_jetzt(item, lokal.replace(hour=9)) == (False, None)
    assert naechster_wechsel(item, lokal, TZ) == lokal.replace(hour=8, minute=30)
    # nächster Block erst in einer Woche
    assert naechster_wechsel(item, lokal.replace(hour=9), TZ) == datetime(2026, 1, 21, 6, 0, tzinfo=TZ)
    assert naechster_wechsel({}, lokal, TZ) is None


def test_plan_info_quellen():
    room = Room(raum="wz", name="WZ", schedule="schedule.heizplan_wz", absenk=16.0)
    item = {"wednesday": [{"from": "06:00:00", "to": "08:30:00", "data": {"temperatur": 21}}]}
    jetzt = datetime(2026, 1, 14, 6, 0, tzinfo=UTC)  # 07:00 Ortszeit
    # Zustand der Entität bestimmt Block/Absenkung, Temperatur aus dem Attribut
    by_id = {"schedule.heizplan_wz": {"state": "on", "attributes": {"temperatur": 22}}}
    assert plan_info(room, by_id, item, jetzt, TZ, 17.0).soll == 22.0
    # ohne Attribut: Blocktemperatur aus schedule/list
    by_id = {"schedule.heizplan_wz": {"state": "on", "attributes": {}}}
    assert plan_info(room, by_id, item, jetzt, TZ, 17.0).soll == 21.0
    # Entität aus: Absenktemperatur des Raums
    by_id = {"schedule.heizplan_wz": {"state": "off", "attributes": {}}}
    info = plan_info(room, by_id, item, jetzt, TZ, 17.0)
    assert (info.soll, info.im_block) == (16.0, False)
    # Entität nicht verfügbar: aus schedule/list
    by_id = {"schedule.heizplan_wz": {"state": "unavailable", "attributes": {}}}
    assert plan_info(room, by_id, item, jetzt, TZ, 17.0).soll == 21.0
    assert plan_info(room, by_id, None, jetzt, TZ, 17.0).soll is None
    # ohne Heizplan kein Soll
    assert plan_info(Room(raum="x", name="X"), {}, None, jetzt, TZ, 17.0).soll is None


# ------------------------------------------------------------------ Zustand, Info, Optionen


async def test_api_steuerung_generisch_felder(app_client_generisch, uhr):
    d = await (await app_client_generisch.get("/api/steuerung")).json()
    wz = d["raeume"]["wohnzimmer"]
    assert (wz["modus"], wz["hvac_modus"]) == ("plan", "heat")
    assert wz["plan_soll"] == 17.0  # 12:00 Uhr: Absenkung
    assert wz["naechster_wechsel"].startswith("2026-01-14T16:00")
    assert wz["modi"] == ["plan", "hand", "aus"]
    assert (wz["min_temp"], wz["max_temp"], wz["schritt"]) == (7.0, 30.0, 0.5)
    assert wz["boost_art"] == "temperatur"
    assert d["raeume"]["kuche"]["boost_art"] == "preset"
    assert wz["overlay_bis"] is None
    assert wz["boost_bis"] is None
    assert d["raeume"]["gaestezimmer_heizung"]["plan_soll"] is None  # ohne Heizplan
    assert d["raeume"]["badezimmer"]["steuerbar"] is False
    assert d["heizperiode"]["intern"] is True
    info = await (await app_client_generisch.get("/api/info")).json()
    assert info["faehigkeiten"]["plan_anwendung"] is True
    assert info["faehigkeiten"]["freigabe"] is False


def test_absenk_override(caplog):
    opts = Options.from_dict({"raeume_override": [{"raum": "wohnzimmer", "absenk": 16.5}, {"raum": "kuche", "absenk": 30}]})
    from klimastudio.config import overrides_anwenden

    with caplog.at_level(logging.WARNING):
        rooms = {r.raum: r for r in overrides_anwenden({}, opts)}
    assert rooms["wohnzimmer"].absenk == 16.5
    assert rooms["kuche"].absenk is None
    assert "absenk" in caplog.text
    assert rooms["wohnzimmer"].to_dict()["absenk"] == 16.5
    assert "absenk" not in rooms["kuche"].to_dict()


async def test_pm_modus_ohne_plananwendung(studio, fake):
    assert studio.plananwendung is None
    assert studio.adapter.faehigkeiten()["plan_anwendung"] is False
    await studio.start()
    await asyncio.sleep(0.05)
    assert studio.plananwendung is None
    assert not [c for c in fake.service_calls if c[1] == "set_temperature"]


async def test_plan_anwenden_aus(ha_client_generisch, fake_generisch, tmp_path):
    opts = Options.from_dict({"betriebsart": "generisch", "plan_anwenden": False})
    ks = KlimaStudio(opts, ha_client_generisch, data_dir=tmp_path / "b", wissen_pfad=WISSEN_TEST)
    try:
        assert ks.plananwendung is None
        assert ks.adapter.faehigkeiten()["plan_anwendung"] is False
        # Modus plan wird trotzdem angenommen: ausgeschalteter Raum wird eingeschaltet, kein Plan-Soll
        fake_generisch.generisch[WZ]["state"] = "off"
        room = await ks.room("wohnzimmer")
        befehl = await ks.adapter.aktion(room, {"aktion": "modus", "modus": "plan"})
        assert befehl["service"] == "set_hvac_mode"
        assert fake_generisch.generisch[WZ]["state"] == "heat"
        assert not rufe(fake_generisch, "set_temperature")
        assert ks.adapter.app("wohnzimmer")["modus"] == "plan"
    finally:
        ks.store.close()


async def test_start_stop_plananwendung(studio_generisch, fake_generisch, uhr):
    await studio_generisch.start()
    pa = studio_generisch.plananwendung
    try:
        assert pa.laeuft
        for _ in range(250):
            if soll(fake_generisch, WZ) == 17.0:
                break
            await asyncio.sleep(0.02)
        assert soll(fake_generisch, WZ) == 17.0
    finally:
        await pa.stop()
    assert not pa.laeuft


# ------------------------------------------------------------------ Aktionen


async def test_modus_hand_aus_plan(app_client_generisch, studio_generisch, fake_generisch, uhr):
    status, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "hand"})
    assert status == 200, d
    assert d["raum"]["modus"] == "hand"
    assert rufe(fake_generisch) == []  # Thermostat läuft bereits
    status, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "aus"})
    assert status == 200, d
    assert rufe(fake_generisch)[-1] == ("climate", "set_hvac_mode", {"entity_id": WZ, "hvac_mode": "off"})
    assert (d["raum"]["modus"], d["raum"]["hvac_modus"]) == ("aus", "off")
    # Plananwendung schreibt nicht in ausgeschaltete Räume
    await takt(studio_generisch)
    assert not rufe(fake_generisch, "set_temperature", WZ)
    status, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "plan"})
    assert status == 200, d
    assert ("climate", "set_hvac_mode", {"entity_id": WZ, "hvac_mode": "heat"}) in rufe(fake_generisch)
    assert soll(fake_generisch, WZ) == 17.0  # Plan sofort angewendet (Absenkung)
    assert d["raum"]["modus"] == "plan"
    assert d["raum"]["soll"] == 17.0
    plan = await studio_generisch.store.ereignisse("plan")
    assert plan[0]["raum"] == "wohnzimmer"
    assert "Absenkung 17 °C" in plan[0]["text"]
    steuerung = await studio_generisch.store.ereignisse("steuerung")
    assert [e["text"] for e in steuerung][:1] == ["Wohnzimmer: Modus Plan (Heizplan)"]


async def test_modus_aus_mit_turn_off(app_client_generisch, fake_generisch, uhr):
    a = fake_generisch.generisch[GZ]["attributes"]
    a["hvac_modes"] = ["heat"]  # nur über TURN_OFF ausschaltbar
    status, d = await post(app_client_generisch, "gaestezimmer_heizung", {"aktion": "modus", "modus": "aus"})
    assert status == 200, d
    assert rufe(fake_generisch)[-1] == ("climate", "turn_off", {"entity_id": GZ})


@pytest.mark.parametrize(
    ("raum", "body", "status", "code", "text"),
    [
        ("wohnzimmer", {"aktion": "overlay", "temperatur": 31}, 400, "ungueltig", "zwischen 7 und 30"),
        ("wohnzimmer", {"aktion": "overlay", "temperatur": 20.3}, 400, "ungueltig", "Vielfaches von 0,5"),
        ("wohnzimmer", {"aktion": "overlay", "temperatur": "20"}, 400, "ungueltig", "Zahl"),
        ("wohnzimmer", {"aktion": "overlay", "temperatur": 20, "dauer": 2000}, 400, "ungueltig", "dauer"),
        ("wohnzimmer", {"aktion": "overlay", "temperatur": 20, "x": 1}, 400, "ungueltig", "Unbekannte Felder"),
        ("wohnzimmer", {"aktion": "boost", "dauer": 3}, 400, "ungueltig", "5 und 240"),
        ("wohnzimmer", {"aktion": "boost", "dauer": 250}, 400, "ungueltig", "5 und 240"),
        ("wohnzimmer", {"aktion": "modus", "modus": "auto"}, 400, "ungueltig", "plan, hand oder aus"),
        ("wohnzimmer", {"aktion": "springen"}, 400, "ungueltig", "aktion"),
        ("wohnzimmer", {"aktion": "temperatur", "temperatur": 20}, 409, "modus", "Modus Hand"),
        ("gaestezimmer_heizung", {"aktion": "overlay", "temperatur": 26}, 400, "ungueltig", "zwischen 5 und 25"),
        ("badezimmer", {"aktion": "modus", "modus": "aus"}, 409, "nicht_steuerbar", "Temperaturbereich"),
        ("schlafzimmer", {"aktion": "zurueck"}, 409, "nicht_verfuegbar", "nicht verfügbar"),
    ],
)
async def test_validierung(app_client_generisch, fake_generisch, uhr, raum, body, status, code, text):
    st, d = await post(app_client_generisch, raum, body)
    assert st == status, d
    assert d["code"] == code
    assert text in d["fehler"]
    assert rufe(fake_generisch) == []


async def test_validierung_schrittweite_und_modus(app_client_generisch, fake_generisch, uhr):
    fake_generisch.generisch[KU]["attributes"]["target_temp_step"] = 1
    st, d = await post(app_client_generisch, "kuche", {"aktion": "overlay", "temperatur": 20.5})
    assert (st, d["code"]) == (400, "ungueltig")
    assert "Vielfaches von 1 °C" in d["fehler"]
    await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "hand"})
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 20})
    assert (st, d["code"]) == (409, "modus")
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "temperatur", "temperatur": 19.5})
    assert st == 200, d
    assert soll(fake_generisch, WZ) == 19.5
    assert d["raum"]["grund"] == "manuell"
    await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "aus"})
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "boost", "dauer": 30})
    assert (st, d["code"]) == (409, "modus")
    resp = await app_client_generisch.post("/api/steuerung/keller", json={"aktion": "zurueck"})
    assert resp.status == 404


async def test_overlay_dauer_und_ablauf(app_client_generisch, studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 22, "dauer": 60})
    assert st == 200, d
    assert rufe(fake_generisch)[-1] == ("climate", "set_temperature", {"entity_id": WZ, "temperature": 22})
    assert d["raum"]["overlay_bis"] == (T0 + timedelta(minutes=60)).isoformat()
    assert d["raum"]["grund"] == "overlay"
    anzahl = len(rufe(fake_generisch))
    uhr.weiter(minutes=30)
    await takt(studio_generisch)
    assert len(rufe(fake_generisch)) == anzahl  # Overlay läuft, Plan pausiert
    uhr.weiter(minutes=31)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0
    assert "Zeit abgelaufen, zurück auf 17 °C (Heizplan)" in (await studio_generisch.store.ereignisse("plan"))[0]["text"]
    d = (await (await app_client_generisch.get("/api/steuerung")).json())["raeume"]["wohnzimmer"]
    assert d["overlay_bis"] is None


async def test_overlay_bis_planwechsel_dauerhaft_zurueck(app_client_generisch, studio_generisch, fake_generisch, uhr):
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 20, "dauer": None})
    assert st == 200, d
    assert d["raum"]["overlay_bis"] == "2026-01-14T15:00:00+00:00"  # 16:00 Ortszeit (Blockbeginn)
    uhr.lokal(16, 1)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 21.0  # Planblock
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 19, "dauer": 0})
    assert d["raum"]["overlay_bis"] == "dauerhaft"
    uhr.weiter(days=2)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 19.0
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "zurueck"})
    assert st == 200, d
    assert d["raum"]["overlay_bis"] is None
    assert soll(fake_generisch, WZ) == d["raum"]["plan_soll"]


async def test_timer_ueberstehen_neustart(
    app_client_generisch, studio_generisch, ha_client_generisch, fake_generisch, uhr, tmp_path
):
    await takt(studio_generisch)
    st, d = await post(app_client_generisch, "kuche", {"aktion": "overlay", "temperatur": 22.5, "dauer": 90})
    assert st == 200, d
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "hand"})
    studio_generisch.store.close()
    ks = KlimaStudio(studio_generisch.opts, ha_client_generisch, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    ks.adapter.uhr = uhr
    try:
        by_id = {s["entity_id"]: s for s in fake_generisch.state_list()}
        rooms = {r.raum: r for r in await ks.rooms()}
        z = ks.adapter.raum_zustand(rooms["kuche"], by_id)
        assert z["overlay_bis"] == (T0 + timedelta(minutes=90)).isoformat()
        assert ks.adapter.raum_zustand(rooms["wohnzimmer"], by_id)["modus"] == "hand"
        assert ks.adapter.app("kuche")["geschrieben"] == 22.5
        uhr.weiter(minutes=91)
        await ks.plananwendung.takt()
        assert soll(fake_generisch, KU) == 17.0
    finally:
        ks.store.close()


# ------------------------------------------------------------------ Boost


async def test_boost_mit_preset(app_client_generisch, studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    fake_generisch.generisch[KU]["attributes"]["preset_mode"] = "eco"
    st, d = await post(app_client_generisch, "kuche", {"aktion": "boost", "dauer": 30})
    assert st == 200, d
    assert rufe(fake_generisch)[-1] == ("climate", "set_preset_mode", {"entity_id": KU, "preset_mode": "boost"})
    assert d["raum"]["boost_bis"] == (T0 + timedelta(minutes=30)).isoformat()
    assert d["raum"]["grund"] == "boost"
    uhr.lokal(16, 5)  # Boost abgelaufen, inzwischen Planblock 21 °C
    await takt(studio_generisch)
    assert fake_generisch.generisch[KU]["attributes"]["preset_mode"] == "eco"
    assert soll(fake_generisch, KU) == 21.0


async def test_boost_ohne_preset(app_client_generisch, studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "boost", "dauer": 15})
    assert st == 200, d
    assert rufe(fake_generisch)[-1] == ("climate", "set_temperature", {"entity_id": WZ, "temperature": 30})
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 30.0  # keine Handänderung erkannt, Plan pausiert
    uhr.weiter(minutes=16)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0
    # Hand-Modus: Rückkehr zum vorherigen Wert
    await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "hand"})
    await post(app_client_generisch, "wohnzimmer", {"aktion": "temperatur", "temperatur": 20.5})
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "boost", "dauer": 5})
    assert soll(fake_generisch, WZ) == 30.0
    st, d = await post(app_client_generisch, "wohnzimmer", {"aktion": "zurueck"})
    assert st == 200, d
    assert soll(fake_generisch, WZ) == 20.5
    assert d["raum"]["boost_bis"] is None


# ------------------------------------------------------------------ Plananwendung


async def test_plan_blockwechsel_nur_bei_aenderung(studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0
    assert soll(fake_generisch, KU) == 17.0
    assert soll(fake_generisch, GZ) == 18.0  # ohne Heizplan unverändert
    anzahl = len(rufe(fake_generisch))
    uhr.weiter(minutes=10)
    await takt(studio_generisch)
    assert len(rufe(fake_generisch)) == anzahl  # Soll unverändert: nichts schreiben
    uhr.lokal(16, 0)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 21.0
    assert soll(fake_generisch, KU) == 21.0
    uhr.lokal(22, 0)  # Küche endet um 22:00, Wohnzimmer um 22:30
    await takt(studio_generisch)
    assert (soll(fake_generisch, WZ), soll(fake_generisch, KU)) == (21.0, 17.0)
    ereignisse = await studio_generisch.store.ereignisse("plan")
    assert len(ereignisse) == 5
    assert ereignisse[0]["daten"] == {"temperatur": 17.0, "dienst": "climate.set_temperature"}
    assert not [c for c in fake_generisch.service_calls if c[2].get("entity_id") in ("climate.bad_klima",)]


async def test_absenk_je_raum(studio_generisch, fake_generisch, uhr):
    studio_generisch.opts.raeume.append({"raum": "wohnzimmer", "absenk": 16})
    await studio_generisch.rooms(refresh=True)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 16.0
    assert soll(fake_generisch, KU) == 17.0


async def test_fenster_offen_nicht_erhoehen(studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    fenster = fake_generisch.sim.series
    fenster["binary_sensor.kontakt_12"] = [(0.0, "on", None)]
    uhr.lokal(16, 0)
    await takt(studio_generisch)
    assert soll(fake_generisch, KU) == 17.0  # Fenster offen: nicht erhöhen
    assert soll(fake_generisch, WZ) == 21.0
    fenster["binary_sensor.kontakt_12"] = [(0.0, "off", None)]
    uhr.lokal(16, 1)
    await takt(studio_generisch)
    assert soll(fake_generisch, KU) == 21.0  # Fenster zu: nachholen
    fenster["binary_sensor.kontakt_12"] = [(0.0, "on", None)]
    uhr.lokal(22, 0)
    await takt(studio_generisch)
    assert soll(fake_generisch, KU) == 17.0  # Absenken auch bei offenem Fenster


async def test_unavailable_backoff(studio_generisch, fake_generisch, uhr):
    fake_generisch.generisch[WZ]["state"] = "unavailable"
    await takt(studio_generisch)
    assert not rufe(fake_generisch, eid=WZ)
    fake_generisch.generisch[WZ]["state"] = "heat"
    uhr.weiter(minutes=1)
    await takt(studio_generisch)
    assert not rufe(fake_generisch, eid=WZ)  # noch in der 5-Minuten-Pause
    uhr.weiter(minutes=5)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0


async def test_schreibfehler_backoff(studio_generisch, fake_generisch, uhr, monkeypatch):
    original = fake_generisch._generischer_dienst

    def ablehnen(service, data):
        if data.get("entity_id") == WZ:
            raise DienstFehler(400, "Thermostat antwortet nicht")
        original(service, data)

    monkeypatch.setattr(fake_generisch, "_generischer_dienst", ablehnen)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 21.0
    assert soll(fake_generisch, KU) == 17.0  # andere Räume weiter
    fehler = len(rufe(fake_generisch, eid=WZ))
    assert fehler == 1
    uhr.weiter(minutes=2)
    await takt(studio_generisch)
    assert len(rufe(fake_generisch, eid=WZ)) == fehler  # Pause nach Fehler
    monkeypatch.setattr(fake_generisch, "_generischer_dienst", original)
    uhr.weiter(minutes=4)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0


async def test_handaenderung_wird_overlay(studio_generisch, fake_generisch, uhr):
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 17.0
    uhr.weiter(minutes=2)
    fake_generisch.generisch[WZ]["attributes"]["temperature"] = 19.5  # am Gerät verstellt
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 19.5  # innerhalb der Karenz: nichts tun
    assert studio_generisch.adapter.app("wohnzimmer")["overlay_bis"] is None
    uhr.weiter(minutes=10)
    anzahl = len(rufe(fake_generisch))
    await takt(studio_generisch)
    a = studio_generisch.adapter.app("wohnzimmer")
    assert a["overlay_bis"] == "2026-01-14T15:00:00+00:00"  # bis zum nächsten Planblock (16:00)
    assert a["overlay_temp"] == 19.5
    assert len(rufe(fake_generisch)) == anzahl
    assert "Handänderung am Thermostat auf 19,5 °C" in (await studio_generisch.store.ereignisse("plan"))[0]["text"]
    uhr.lokal(15, 0)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 19.5
    uhr.lokal(16, 0)
    await takt(studio_generisch)
    assert soll(fake_generisch, WZ) == 21.0
    assert studio_generisch.adapter.app("wohnzimmer")["overlay_bis"] is None


async def test_off_bleibt_off_und_hand_unberuehrt(app_client_generisch, studio_generisch, fake_generisch, uhr):
    fake_generisch.generisch[WZ]["state"] = "off"
    await post(app_client_generisch, "kuche", {"aktion": "modus", "modus": "hand"})
    await takt(studio_generisch)
    uhr.lokal(16, 0)
    await takt(studio_generisch)
    assert fake_generisch.generisch[WZ]["state"] == "off"
    assert not rufe(fake_generisch, eid=WZ)
    assert not rufe(fake_generisch, eid=KU)
    assert soll(fake_generisch, KU) == 20.0


# ------------------------------------------------------------------ Heizperiode generisch


async def test_sommer_aus_und_rueckkehr(app_client_generisch, studio_generisch, fake_generisch, uhr):
    fake_generisch.extra["input_boolean.pm_heizperiode"] = ("on", {"friendly_name": "PM Heizperiode"})
    await post(app_client_generisch, "wohnzimmer", {"aktion": "modus", "modus": "hand"})
    await post(app_client_generisch, "wohnzimmer", {"aktion": "temperatur", "temperatur": 20})
    await takt(studio_generisch)
    fake_generisch.service_calls.clear()
    resp = await app_client_generisch.post("/api/heizperiode", json={"modus": "sommer"})
    d = await resp.json()
    assert resp.status == 200, d
    assert (d["aktiv"], d["zustand"], d["intern"], d["spiegel"]) == (False, "off", True, "off")
    assert fake_generisch.generisch[KU]["state"] == "off"
    assert fake_generisch.generisch[GZ]["state"] == "off"
    assert fake_generisch.generisch[WZ]["state"] == "heat"  # Hand-Raum unberührt
    assert not rufe(fake_generisch, eid=WZ)
    assert studio_generisch.adapter.app("kuche")["hvac_vor_sommer"] == "auto"
    z = (await (await app_client_generisch.get("/api/steuerung")).json())["raeume"]
    assert (z["kuche"]["modus"], z["kuche"]["hvac_modus"], z["kuche"]["grund"]) == ("plan", "off", "sommer")
    # Sommer-Pause: keine Plananwendung
    uhr.lokal(16, 0)
    fake_generisch.service_calls.clear()
    await takt(studio_generisch)
    assert rufe(fake_generisch) == []
    # Heizperiode: gemerkten Modus wiederherstellen, Plan anwenden
    resp = await app_client_generisch.post("/api/heizperiode", json={"modus": "heizperiode"})
    d = await resp.json()
    assert resp.status == 200, d
    assert (d["aktiv"], d["spiegel"]) == (True, "on")
    assert fake_generisch.generisch[KU]["state"] == "auto"
    assert fake_generisch.generisch[GZ]["state"] == "heat"
    assert soll(fake_generisch, KU) == 21.0
    assert soll(fake_generisch, WZ) == 20.0
    assert not rufe(fake_generisch, eid=WZ)
    assert studio_generisch.adapter.app("kuche")["hvac_vor_sommer"] is None
    texte = [e["text"] for e in await studio_generisch.store.ereignisse("heizperiode")]
    assert any(t.startswith("Heizperiode beendet (Sommer)") for t in texte)
    assert any(t.startswith("Heizperiode begonnen") for t in texte)


async def test_sommer_nur_pausieren(app_client_generisch, studio_generisch, fake_generisch, uhr):
    studio_generisch.opts.sommer_aktion = "plan_pausieren"
    await takt(studio_generisch)
    fake_generisch.service_calls.clear()
    resp = await app_client_generisch.post("/api/heizperiode", json={"modus": "sommer"})
    assert resp.status == 200
    assert rufe(fake_generisch) == []
    uhr.lokal(16, 0)
    await takt(studio_generisch)
    assert rufe(fake_generisch) == []
    assert fake_generisch.generisch[KU]["state"] == "auto"
    resp = await app_client_generisch.post("/api/heizperiode", json={"modus": "heizperiode"})
    assert resp.status == 200
    assert soll(fake_generisch, KU) == 21.0


async def test_sommer_raum_bereits_aus_bleibt_aus(app_client_generisch, studio_generisch, fake_generisch, uhr):
    fake_generisch.generisch[GZ]["state"] = "off"
    await app_client_generisch.post("/api/heizperiode", json={"modus": "sommer"})
    await app_client_generisch.post("/api/heizperiode", json={"modus": "heizperiode"})
    assert fake_generisch.generisch[GZ]["state"] == "off"
    assert fake_generisch.generisch[KU]["state"] == "auto"


async def test_sommer_thermostat_nicht_verfuegbar(app_client_generisch, studio_generisch, fake_generisch, uhr):
    fake_generisch.generisch[KU]["state"] = "unavailable"
    await app_client_generisch.post("/api/heizperiode", json={"modus": "sommer"})
    assert studio_generisch.adapter.app("kuche")["sommer_aus_offen"] is True
    fake_generisch.generisch[KU]["state"] = "heat"
    uhr.weiter(minutes=6)
    await takt(studio_generisch)
    assert fake_generisch.generisch[KU]["state"] == "off"
    assert studio_generisch.adapter.app("kuche")["hvac_vor_sommer"] == "heat"


async def test_heizperiode_automatik_generisch(studio_generisch, fake_generisch, uhr):
    heute = datetime.now(TZ).date()
    fake_generisch.statistik["sensor.aussentemperatur"] = lambda ts: (
        20.0 if datetime.fromtimestamp(ts, TZ).date() < heute else None
    )
    ergebnis = await studio_generisch.heizperiode.pruefen()
    assert ergebnis["aktiv"] is False
    assert studio_generisch.adapter.pause is True
    assert fake_generisch.generisch[KU]["state"] == "off"
    status = await studio_generisch.heizperiode.status()
    assert status["vorhanden"] is False  # Spiegel-Entität ist keine Voraussetzung
    assert status["abweichung"] is None
    # Hintergrundprüfung läuft generisch
    studio_generisch.heizperiode.start()
    assert studio_generisch.heizperiode._task is not None


async def test_heizperiode_einrichten_spiegel(app_client_generisch, fake_generisch, uhr):
    resp = await app_client_generisch.post("/api/heizperiode/einrichten", json={})
    d = await resp.json()
    assert resp.status == 200, d
    assert "Spiegel" in d["hinweis"]
    assert fake_generisch.extra["input_boolean.pm_heizperiode"][0] == "on"
    resp = await app_client_generisch.post("/api/heizperiode/einrichten", json={})
    assert resp.status == 409
    assert (await resp.json())["code"] == "vorhanden"


# ------------------------------------------------------------------ Speicher


def test_store_migration_v1(tmp_path):
    pfad = tmp_path / "coach.db"
    conn = sqlite3.connect(pfad)
    conn.executescript(
        "CREATE TABLE einstellungen (schluessel TEXT PRIMARY KEY, wert TEXT NOT NULL);"
        "INSERT INTO einstellungen VALUES ('heizperiode', '{\"modus\": \"sommer\"}');"
        "PRAGMA user_version = 1;"
    )
    conn.close()
    st = CoachStore(pfad)
    try:
        assert st.schema_version() == store_mod.SCHEMA_VERSION == 2
        assert st.app_raeume_lesen() == {}
        assert st.einstellung_lesen("heizperiode") == {"modus": "sommer"}
    finally:
        st.close()


async def test_store_app_raum(tmp_path):
    st = CoachStore(tmp_path / "coach.db")
    try:
        await st.app_raum_setzen("wz", {"modus": "hand", "geschrieben": 20.5, "sommer_aus_offen": True})
        await st.app_raum_setzen("wz", {"modus": "aus", "hvac_vor_aus": "heat"})
        z = st.app_raeume_lesen()["wz"]
        assert (z["modus"], z["hvac_vor_aus"], z["geschrieben"], z["sommer_aus_offen"]) == ("aus", "heat", None, False)
    finally:
        st.close()
