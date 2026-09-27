"""Betriebsart generisch, Review 1.2.0: Schutz echter Heizungen (Erststart, Geräteprogramme, Übernahme,
Sommer, Raumerkennung, Registrierungen) und Ergänzungen der Schnittstelle."""

from __future__ import annotations

import copy
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from conftest import WISSEN_TEST
from fake_ha import F_AN, F_ZIEL, TZ, DienstFehler, FakeHA
from klimastudio.adapter.generisch import (
    HINWEIS_GERAETEPROGRAMM,
    HINWEIS_KUEHLEN,
    SOMMER_MAX_VERSUCHE,
    GenerischAdapter,
)
from klimastudio.coach.engine import WISSEN_DATEI, Regelwerk, auswerten
from klimastudio.coach.store import CoachStore
from klimastudio.config import Options, Room
from klimastudio.data import DataService
from klimastudio.plananwendung import HINWEIS_PLAN_LEER, HINWEIS_PLAN_OHNE_TEMP, PlanQuelle, plan_info
from klimastudio.server import KlimaStudio

T0 = datetime(2026, 1, 14, 11, 0, tzinfo=UTC)  # Mittwoch 12:00 Ortszeit: Absenkung 17 °C
WZ, KU, GZ = "climate.wohnzimmer_thermostat", "climate.kueche_heizkoerper", "climate.gaestezimmer_heizung"


class Uhr:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def weiter(self, **kw) -> None:
        self.t += timedelta(**kw)


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


async def aktion(ks: KlimaStudio, raum: str, body: dict) -> dict:
    return await ks.adapter.aktion(await ks.room(raum), body)


async def zustand(ks: KlimaStudio, raum: str) -> dict:
    by_id = {s["entity_id"]: s for s in await ks.client.get_states()}
    rooms = await ks.rooms()
    await ks.adapter.vorbereiten(rooms, by_id)
    return ks.adapter.raum_zustand(await ks.room(raum), by_id)


async def plan(ks: KlimaStudio, *raeume: str) -> None:
    for raum in raeume:
        await aktion(ks, raum, {"aktion": "modus", "modus": "plan"})


# ------------------------------------------------------------------ Erststart (Befund 2)


async def test_erststart_hand_schreibt_nichts(studio_generisch, fake_generisch, uhr):
    await studio_generisch.plananwendung.takt()
    assert rufe(fake_generisch) == []
    z = await zustand(studio_generisch, "wohnzimmer")
    assert (z["modus"], z["grund"], z["plan_soll"]) == ("hand", "manuell", 17.0)


async def test_sanfter_start_bei_plan(studio_generisch, fake_generisch, uhr):
    # Sollwert steht schon (innerhalb der Toleranz) auf dem Plan-Soll: nichts schreiben
    fake_generisch.generisch[WZ]["attributes"]["temperature"] = 17.0
    await plan(studio_generisch, "wohnzimmer")
    assert rufe(fake_generisch, "set_temperature") == []
    assert studio_generisch.adapter.app("wohnzimmer")["geschrieben"] == 17.0
    # sonst ausdrücklicher Nutzerwunsch: sofort schreiben
    fake_generisch.generisch[GZ]["attributes"]["temperature"] = 20.0
    await plan(studio_generisch, "gaestezimmer_heizung")  # ohne Heizplan: kein Plan-Soll, nichts schreiben
    assert rufe(fake_generisch, "set_temperature") == []
    fake_generisch.generisch[KU]["state"] = "heat"
    await plan(studio_generisch, "kuche")
    assert rufe(fake_generisch, "set_temperature") == [("climate", "set_temperature", {"entity_id": KU, "temperature": 17})]


# ------------------------------------------------------------------ Plan ohne Temperaturen (Befund 3)


def test_plan_info_ohne_temperatur():
    room = Room(raum="wz", name="WZ", schedule="schedule.heizplan_wz")
    jetzt = datetime(2026, 1, 14, 11, 0, tzinfo=UTC)  # außerhalb der Blöcke
    by_id = {"schedule.heizplan_wz": {"state": "off", "attributes": {}}}
    item = {"wednesday": [{"from": "06:00:00", "to": "08:00:00", "data": {}}]}
    info = plan_info(room, by_id, item, jetzt, TZ, 17.0)
    assert (info.soll, info.hinweis) == (None, HINWEIS_PLAN_OHNE_TEMP)  # auch keine Absenkung
    info = plan_info(room, by_id, {"id": "heizplan_wz"}, jetzt, TZ, 17.0)
    assert (info.soll, info.hinweis) == (None, HINWEIS_PLAN_LEER)
    info = plan_info(room, by_id, None, jetzt, TZ, 17.0)
    assert info.soll is None
    assert "nicht lesbar" in info.hinweis


async def test_plan_ohne_temperatur_schreibt_nichts(studio_generisch, fake_generisch, uhr):
    for tag in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        for rng in fake_generisch.schedules["heizplan_wohnzimmer"][tag]:
            rng.pop("data", None)
    await plan(studio_generisch, "wohnzimmer")
    await studio_generisch.plananwendung.takt()
    assert rufe(fake_generisch, "set_temperature", WZ) == []
    z = await zustand(studio_generisch, "wohnzimmer")
    assert z["plan_soll"] is None
    assert z["plan_hinweis"] == HINWEIS_PLAN_OHNE_TEMP


# ------------------------------------------------------------------ Geräteprogramme und Kühlen (Befund 4)


async def test_plan_stellt_auto_auf_heat(studio_generisch, fake_generisch, uhr):
    assert fake_generisch.generisch[KU]["state"] == "auto"
    befehl = await aktion(studio_generisch, "kuche", {"aktion": "modus", "modus": "plan"})
    assert befehl["daten"] == {"hvac_mode": "heat"}
    assert fake_generisch.generisch[KU]["state"] == "heat"
    assert soll(fake_generisch, KU) == 17.0
    texte = [e["text"] for e in await studio_generisch.store.ereignisse("plan")]
    assert any("von auto auf heat umgestellt" in t for t in texte)
    # erneuter Wechsel: kein weiterer Moduswechsel
    fake_generisch.service_calls.clear()
    await aktion(studio_generisch, "kuche", {"aktion": "modus", "modus": "hand"})
    assert rufe(fake_generisch, "set_hvac_mode") == []


async def test_auto_ohne_heat_bleibt_geraeteprogramm(studio_generisch, fake_generisch, uhr):
    a = fake_generisch.generisch[KU]["attributes"]
    a["hvac_modes"] = ["auto", "off"]
    await plan(studio_generisch, "kuche")
    await studio_generisch.plananwendung.takt()
    assert fake_generisch.generisch[KU]["state"] == "auto"
    assert rufe(fake_generisch, eid=KU) == []
    z = await zustand(studio_generisch, "kuche")
    assert z["plan_hinweis"] == HINWEIS_GERAETEPROGRAMM.format(modus="auto")


async def test_einschalten_bevorzugt_heat(studio_generisch, fake_generisch, uhr):
    await aktion(studio_generisch, "kuche", {"aktion": "modus", "modus": "aus"})
    assert studio_generisch.adapter.app("kuche")["hvac_vor_aus"] == "auto"
    await aktion(studio_generisch, "kuche", {"aktion": "modus", "modus": "hand"})
    assert fake_generisch.generisch[KU]["state"] == "heat"  # nicht das gemerkte Geräteprogramm auto


async def test_kuehlgeraet_nie_mit_plan_und_im_sommer_nicht_aus(studio_generisch, fake_generisch, uhr):
    wz = fake_generisch.generisch[WZ]
    wz["attributes"]["hvac_modes"] = ["heat", "cool", "off"]
    wz["state"] = "cool"
    await plan(studio_generisch, "wohnzimmer")
    assert fake_generisch.generisch[WZ]["state"] == "cool"  # Kühlen wird nicht auf heat umgestellt
    await studio_generisch.plananwendung.takt()
    assert rufe(fake_generisch, eid=WZ) == []
    assert (await zustand(studio_generisch, "wohnzimmer"))["plan_hinweis"] == HINWEIS_KUEHLEN.format(modus="cool")
    await studio_generisch.heizperiode.einstellen({"modus": "sommer"})
    assert fake_generisch.generisch[WZ]["state"] == "cool"
    assert rufe(fake_generisch, eid=WZ) == []


async def test_tado_hinweis(studio_generisch, fake_generisch, uhr):
    fake_generisch.generisch[WZ]["attributes"]["default_overlay_type"] = "TADO_MODE"
    z = await zustand(studio_generisch, "wohnzimmer")
    assert any("tado" in h and "TADO_MODE" in h for h in z["hinweise"])
    fake_generisch.generisch[WZ]["attributes"]["default_overlay_type"] = "MANUAL"
    assert not (await zustand(studio_generisch, "wohnzimmer"))["hinweise"]


# ------------------------------------------------------------------ Boost vor dem Ausschalten (Befund 5, 10)


async def test_temperatur_boost_vor_aus_zurueck(studio_generisch, fake_generisch, uhr):
    await aktion(studio_generisch, "wohnzimmer", {"aktion": "boost", "dauer": 30})
    assert soll(fake_generisch, WZ) == 25.0  # min(max_temp 30, 25)
    await aktion(studio_generisch, "wohnzimmer", {"aktion": "modus", "modus": "aus"})
    aufrufe = rufe(fake_generisch, eid=WZ)
    assert aufrufe[-2:] == [
        ("climate", "set_temperature", {"entity_id": WZ, "temperature": 21}),
        ("climate", "set_hvac_mode", {"entity_id": WZ, "hvac_mode": "off"}),
    ]
    a = studio_generisch.adapter.app("wohnzimmer")
    assert (a["boost_bis"], a["modus"]) == (None, "aus")


def test_boost_grenze():
    assert GenerischAdapter.boost_temp({"max_temp": 30}) == 25.0
    assert GenerischAdapter.boost_temp({"max_temp": 22, "min_temp": 5}) == 22.0
    assert GenerischAdapter.boost_temp({"max_temp": 28, "target_temp_step": 1}) == 25.0


# ------------------------------------------------------------------ Sommer (Befund 6, 11, 12)


async def test_sommer_ohne_ausschalten_absenkung(studio_generisch, fake_generisch, uhr):
    a = fake_generisch.generisch[WZ]["attributes"]
    a["hvac_modes"] = ["heat"]
    a["supported_features"] = F_ZIEL | F_AN  # kein TURN_OFF
    await plan(studio_generisch, "wohnzimmer")
    fake_generisch.generisch[WZ]["attributes"]["temperature"] = 21.0
    fake_generisch.service_calls.clear()
    await studio_generisch.heizperiode.einstellen({"modus": "sommer"})
    assert rufe(fake_generisch, eid=WZ) == [("climate", "set_temperature", {"entity_id": WZ, "temperature": 17})]
    assert studio_generisch.adapter.app("wohnzimmer")["sommer_aus_offen"] is False
    await studio_generisch.plananwendung.takt()
    assert len(rufe(fake_generisch, eid=WZ)) == 1


async def test_sommer_fehler_mit_obergrenze(studio_generisch, fake_generisch, uhr, monkeypatch):
    await plan(studio_generisch, "wohnzimmer")
    original = fake_generisch._generischer_dienst

    def ablehnen(service, data):
        if data.get("entity_id") == WZ:
            raise DienstFehler(400, "Gerät antwortet nicht")
        original(service, data)

    monkeypatch.setattr(fake_generisch, "_generischer_dienst", ablehnen)
    await studio_generisch.heizperiode.einstellen({"modus": "sommer"})
    pa = studio_generisch.plananwendung
    abstaende = []
    for _ in range(SOMMER_MAX_VERSUCHE + 2):
        if studio_generisch.adapter.app("wohnzimmer")["sommer_aus_offen"] is False:
            break
        uhr.weiter(minutes=61)
        await pa.takt()
        abstaende.append(pa._backoff.get("wohnzimmer"))
    a = studio_generisch.adapter.app("wohnzimmer")
    assert a["sommer_aus_offen"] is False
    assert fake_generisch.generisch[WZ]["state"] == "heat"
    texte = [e["text"] for e in await studio_generisch.store.ereignisse("plan")]
    assert any(f"nach {SOMMER_MAX_VERSUCHE} Versuchen" in t for t in texte)
    anzahl = len(rufe(fake_generisch, eid=WZ))
    uhr.weiter(minutes=61)
    await pa.takt()
    assert len(rufe(fake_generisch, eid=WZ)) == anzahl  # keine Endlosversuche


async def test_backoff_waechst_bis_obergrenze(studio_generisch):
    pa = studio_generisch.plananwendung
    minuten = [int(pa._pause_nach_fehler("x", T0).total_seconds() // 60) for _ in range(7)]
    assert minuten == [5, 10, 20, 40, 60, 60, 60]


async def test_sommer_ohne_plananwendung_schaltet_nichts(ha_client_generisch, fake_generisch, tmp_path):
    opts = Options.from_dict({"betriebsart": "generisch", "plan_anwenden": False})
    ks = KlimaStudio(opts, ha_client_generisch, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    try:
        await plan(ks, "wohnzimmer")
        fake_generisch.service_calls.clear()
        await ks.heizperiode.einstellen({"modus": "sommer"})
        assert rufe(fake_generisch) == []
        assert ks.adapter.pause is True
    finally:
        ks.store.close()


async def test_sommer_nur_plan_raeume(studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    await aktion(studio_generisch, "gaestezimmer_heizung", {"aktion": "modus", "modus": "hand"})
    await studio_generisch.heizperiode.einstellen({"modus": "sommer"})
    assert fake_generisch.generisch[WZ]["state"] == "off"
    assert fake_generisch.generisch[GZ]["state"] == "heat"


# ------------------------------------------------------------------ Handänderung und Übernahme (Befund 7)


def test_toleranz():
    assert GenerischAdapter.toleranz({"target_temp_step": 0.5}) == 0.25
    assert GenerischAdapter.toleranz({"target_temp_step": 0.1}) == 0.25
    assert GenerischAdapter.toleranz({"target_temp_step": 1}) == 0.5


async def test_geraet_rundet_keine_handaenderung(studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    fake_generisch.generisch[WZ]["attributes"]["temperature"] = 17.2  # Gerät meldet gerundet
    uhr.weiter(minutes=10)
    await studio_generisch.plananwendung.takt()
    assert studio_generisch.adapter.app("wohnzimmer")["overlay_bis"] is None


async def test_nicht_uebernommen_einmal_nachschreiben(studio_generisch, fake_generisch, uhr, monkeypatch):
    original = fake_generisch._generischer_dienst

    def ignorieren(service, data):  # Gerät bestätigt, übernimmt aber nicht
        if data.get("entity_id") == WZ and service == "set_temperature":
            return
        original(service, data)

    monkeypatch.setattr(fake_generisch, "_generischer_dienst", ignorieren)
    await plan(studio_generisch, "wohnzimmer")
    assert len(rufe(fake_generisch, "set_temperature", WZ)) == 1
    pa = studio_generisch.plananwendung
    uhr.weiter(minutes=2)
    await pa.takt()
    assert len(rufe(fake_generisch, "set_temperature", WZ)) == 1  # Karenz
    uhr.weiter(minutes=4)
    await pa.takt()
    assert len(rufe(fake_generisch, "set_temperature", WZ)) == 2  # einmal erneut
    uhr.weiter(minutes=6)
    await pa.takt()
    uhr.weiter(minutes=6)
    await pa.takt()
    assert len(rufe(fake_generisch, "set_temperature", WZ)) == 2
    a = studio_generisch.adapter.app("wohnzimmer")
    assert a["overlay_bis"] is None  # kein Overlay
    ereignisse = await studio_generisch.store.ereignisse("plan")
    assert sum("nicht übernommen" in e["text"] and "erneut" not in e["text"] for e in ereignisse) == 1
    z = await zustand(studio_generisch, "wohnzimmer")
    assert z["nicht_uebernommen"] is True
    assert any("nicht übernommen" in h for h in z["hinweise"])


async def test_handaenderung_nach_uebernahme(studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    uhr.weiter(minutes=1)
    await studio_generisch.plananwendung.takt()  # Übernahme beobachtet
    assert studio_generisch.adapter.app("wohnzimmer")["gesehen"] == 17.0
    fake_generisch.generisch[WZ]["attributes"]["temperature"] = 20.0
    uhr.weiter(minutes=1)
    await studio_generisch.plananwendung.takt()
    a = studio_generisch.adapter.app("wohnzimmer")
    assert (a["overlay_temp"], a["overlay_bis"]) == (20.0, "2026-01-14T15:00:00+00:00")


@pytest.mark.parametrize("stoerung", ["fenster", "preset"])
async def test_keine_handaenderung_bei_fenster_oder_preset(studio_generisch, fake_generisch, uhr, stoerung):
    fake_generisch.generisch[KU]["state"] = "heat"
    await plan(studio_generisch, "kuche")
    uhr.weiter(minutes=1)
    await studio_generisch.plananwendung.takt()
    if stoerung == "fenster":
        fake_generisch.sim.series["binary_sensor.kontakt_12"] = [(0.0, "on", None)]
        fake_generisch.generisch[KU]["attributes"]["temperature"] = 7.0  # Fensterabsenkung des Geräts
    else:
        fake_generisch.generisch[KU]["attributes"].update(preset_mode="eco", temperature=16.0)
    anzahl = len(rufe(fake_generisch))
    uhr.weiter(minutes=10)
    await studio_generisch.plananwendung.takt()
    assert studio_generisch.adapter.app("kuche")["overlay_bis"] is None
    assert len(rufe(fake_generisch)) == anzahl


# ------------------------------------------------------------------ Schreiben nur bei Änderung (Befund 20)


async def test_kein_befehl_bei_gleichem_wert(studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    fake_generisch.service_calls.clear()
    befehl = await aktion(studio_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 17, "dauer": 30})
    assert rufe(fake_generisch) == []
    assert befehl["domain"] == "klimastudio"
    assert studio_generisch.adapter.app("wohnzimmer")["overlay_bis"] is not None
    await aktion(studio_generisch, "wohnzimmer", {"aktion": "zurueck"})
    assert rufe(fake_generisch) == []


# ------------------------------------------------------------------ Raumerkennung (Befund 8, 14, 19)


def _registries(fake: FakeHA) -> dict:
    return {
        "areas": fake.handle({"type": "config/area_registry/list"}),
        "entities": fake.handle({"type": "config/entity_registry/list"}),
        "devices": fake.handle({"type": "config/device_registry/list"}),
    }


def _klima(fake: FakeHA, eid: str, area: str | None, platform: str | None = None, device: str | None = None) -> None:
    fake.generisch[eid] = copy.deepcopy(fake.generisch[WZ])
    fake.registry[eid] = {"area_id": area, "device_id": device, "hidden_by": None, "platform": platform}


def _erkennen(fake: FakeHA, pm_geladen: bool | None = None, **optionen) -> tuple[GenerischAdapter, dict[str, Room]]:
    ad = GenerischAdapter(Options.from_dict({"betriebsart": "generisch", **optionen}), None)
    ad._pm_geladen = pm_geladen
    return ad, {r.raum: r for r in ad.raeume_erkennen(fake.state_list(), _registries(fake))}


def test_pm_klima_entitaeten_ausgeschlossen():
    fake = FakeHA(pm_klima=False)
    _klima(fake, "climate.pm_wohnzimmer", "wohnzimmer", platform="pm_heizung")
    _klima(fake, "climate.pm_gast", None)
    _, rooms = _erkennen(fake)
    assert rooms["wohnzimmer"].climate == WZ
    assert "climate.pm_wohnzimmer" not in rooms["wohnzimmer"].climate_weitere
    assert "pm_gast" in rooms  # Präfix allein genügt nicht, solange PM Klima nicht geladen ist
    _, rooms = _erkennen(fake, pm_geladen=True)
    assert "pm_gast" not in rooms


def test_mehrere_thermostate_hinweis():
    fake = FakeHA(pm_klima=False)
    _klima(fake, "climate.wohnzimmer_zweiter", "wohnzimmer")
    ad, rooms = _erkennen(fake)
    wz = rooms["wohnzimmer"]
    assert (wz.climate, wz.climate_weitere) == ("climate.wohnzimmer_thermostat", ["climate.wohnzimmer_zweiter"])
    z = ad.raum_zustand(wz, {s["entity_id"]: s for s in fake.state_list()})
    assert z["climate_weitere"] == ["climate.wohnzimmer_zweiter"]
    assert any("weitere Thermostate" in h for h in z["hinweise"])
    assert wz.to_dict()["climate_weitere"] == ["climate.wohnzimmer_zweiter"]
    assert "climate_weitere" not in rooms["kuche"].to_dict()


def test_better_thermostat_huelle(caplog):
    fake = FakeHA(pm_klima=False)
    _klima(fake, "climate.zz_wohnzimmer_bt", "wohnzimmer", platform="better_thermostat")
    ad, rooms = _erkennen(fake)
    wz = rooms["wohnzimmer"]
    assert wz.climate == "climate.zz_wohnzimmer_bt"
    assert wz.climate_weitere == []  # Quell-TRV weder Raumthermostat noch weiteres Thermostat
    assert "wohnzimmer_thermostat" not in rooms
    z = ad.raum_zustand(wz, {s["entity_id"]: s for s in fake.state_list()})
    assert any("Better Thermostat" in h and WZ in h for h in z["hinweise"])
    assert "Quellgeräte" in caplog.text


def test_override_climate_nur_einmal(caplog):
    fake = FakeHA(pm_klima=False)
    _, rooms = _erkennen(fake, raeume_override=[{"raum": "buero", "climate": WZ}])
    assert rooms["buero"].climate == WZ
    assert "wohnzimmer" not in rooms  # automatisch erkannter Raum ohne Thermostat entfällt
    assert "nur in buero" in caplog.text


def test_bereichssensoren_zuerst():
    fake = FakeHA(pm_klima=False)
    fake.extra["sensor.wz_eigen_temp"] = ("20.5", {"device_class": "temperature"})
    fake.extra["sensor.wz_eigen_feuchte"] = ("48", {"device_class": "humidity"})
    for b in fake.bereiche:
        if b["area_id"] == "wohnzimmer":
            b.update(temperature_entity_id="sensor.wz_eigen_temp", humidity_entity_id="sensor.wz_eigen_feuchte")
    _, rooms = _erkennen(fake)
    assert (rooms["wohnzimmer"].temperatur, rooms["wohnzimmer"].feuchte) == ("sensor.wz_eigen_temp", "sensor.wz_eigen_feuchte")
    b = next(b for b in fake.bereiche if b["area_id"] == "wohnzimmer")
    b["temperature_entity_id"] = "sensor.fehlt"
    _, rooms = _erkennen(fake)
    assert rooms["wohnzimmer"].temperatur == "sensor.raumklima_ost_temperatur"


# ------------------------------------------------------------------ Registrierungen und App-Zustand (Befund 9, 13, 15)


async def test_registries_gecacht(studio_generisch, fake_generisch):
    rooms = {r.raum for r in await studio_generisch.rooms(refresh=True)}
    fake_generisch.fail_ws = {
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list_for_display",
    }
    fake_generisch.fail_ws.add("config/entity_registry/list")
    assert {r.raum for r in await studio_generisch.rooms(refresh=True)} == rooms


async def test_thermostat_gewechselt_zustand_verworfen(studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    await aktion(studio_generisch, "wohnzimmer", {"aktion": "overlay", "temperatur": 22, "dauer": 60})
    assert studio_generisch.adapter.app("wohnzimmer")["climate"] == WZ
    studio_generisch.opts.raeume.append({"raum": "wohnzimmer", "climate": GZ})
    await studio_generisch.rooms(refresh=True)
    await studio_generisch.plananwendung.takt()
    assert soll(fake_generisch, WZ) == 17.0  # Overlay am bisherigen Thermostat zurückgesetzt
    a = studio_generisch.adapter.app("wohnzimmer")
    assert (a["modus"], a["overlay_bis"], a["climate"]) == ("hand", None, None)
    assert any("Raumthermostat geändert" in e["text"] for e in await studio_generisch.store.ereignisse("plan"))


async def test_verwaister_raum_timer_geloescht(studio_generisch, fake_generisch, uhr):
    await aktion(studio_generisch, "wohnzimmer", {"aktion": "boost", "dauer": 60})
    assert soll(fake_generisch, WZ) == 25.0
    studio_generisch.opts.raeume.append({"raum": "wohnzimmer", "ausblenden": True})
    await studio_generisch.rooms(refresh=True)
    await studio_generisch.plananwendung.takt()
    assert soll(fake_generisch, WZ) == 21.0
    a = studio_generisch.adapter.app("wohnzimmer")
    assert (a["boost_bis"], a["modus"]) == (None, "hand")
    assert any("nicht mehr vorhanden" in e["text"] for e in await studio_generisch.store.ereignisse("plan"))


def test_store_migration_v2(tmp_path):
    pfad = tmp_path / "coach.db"
    conn = sqlite3.connect(pfad)
    conn.executescript(
        "CREATE TABLE app_raeume (raum TEXT PRIMARY KEY, modus TEXT NOT NULL DEFAULT 'plan', overlay_bis TEXT, "
        "overlay_temp REAL, boost_bis TEXT, boost_art TEXT, vor_temp REAL, vor_preset TEXT, geschrieben REAL, "
        "geschrieben_zeit TEXT, hvac_vor_aus TEXT, hvac_vor_sommer TEXT, sommer_aus_offen INTEGER NOT NULL DEFAULT 0, "
        "geaendert TEXT);"
        "INSERT INTO app_raeume (raum, modus, geschrieben) VALUES ('wz', 'plan', 19.5);"
        "PRAGMA user_version = 2;"
    )
    conn.close()
    st = CoachStore(pfad)
    try:
        assert st.schema_version() == 3
        z = st.app_raeume_lesen()["wz"]
        assert (z["modus"], z["geschrieben"], z["climate"], z["gesehen"], z["nachgeschrieben"]) == ("plan", 19.5, None, None, 0)
    finally:
        st.close()


async def test_alter_zustand_uebernimmt_climate(studio_generisch, fake_generisch, uhr):
    await studio_generisch.store.app_raum_setzen("wohnzimmer", {"modus": "plan"})
    studio_generisch.adapter._app = None
    await studio_generisch.plananwendung.takt()
    a = studio_generisch.adapter.app("wohnzimmer")
    assert (a["modus"], a["climate"]) == ("plan", WZ)


async def test_planquelle_ids_geleert(ha_client_generisch, fake_generisch):
    uhr = Uhr(T0)
    q = PlanQuelle(ha_client_generisch, uhr)
    room = Room(raum="wohnzimmer", name="WZ", schedule="schedule.heizplan_wohnzimmer")
    assert await q.items([room])
    assert q._ids
    q.ids_leeren()
    assert not q._ids
    await q.items([room])
    uhr.weiter(minutes=3)  # TTL abgelaufen: Zuordnung neu ermitteln
    zahl = len([c for c in fake_generisch.ws_commands if c["type"] == "config/entity_registry/get"])
    await q.items([room])
    assert len([c for c in fake_generisch.ws_commands if c["type"] == "config/entity_registry/get"]) == zahl + 1
    # schedule/list nicht lesbar: zuletzt gelesene Pläne gelten weiter
    fake_generisch.fail_ws = {"schedule/list"}
    uhr.weiter(minutes=3)
    assert await q.items([room])


# ------------------------------------------------------------------ Coach, Daten, Schnittstelle (Befund 17, 18)


def test_frost_regel_je_saison():
    werk = Regelwerk.laden(WISSEN_DATEI)
    app = {"betriebsart": "generisch", "plan_anwendung": True}
    raum = {"slug": "wz", "name": "WZ", "nassraum": False, "schlafraum": False, "modus": "aus", "ist": 12}
    for aktiv, erwartet, modus in (
        (True, "frost_raum_aus_generisch", "plan"),
        (False, "frost_raum_aus_sommer_generisch", "hand"),
    ):
        g = {"raum": None, "zeit": {"monat": 1, "stunde": 12, "wochentag": 2}, "heizperiode": {"aktiv": aktiv}}
        g |= {"app": app, "wetter": {"min_3tage": -4}}
        tipps = {t["regel"]: t for t in auswerten(werk, g, [{**g, "raum": raum}])}
        assert erwartet in tipps
        andere = "frost_raum_aus_sommer_generisch" if aktiv else "frost_raum_aus_generisch"
        assert andere not in tipps
        assert tipps[erwartet]["massnahme"]["modus"] == modus


def test_dataservice_pm_wie_1_1(studio, studio_generisch):
    assert DataService(None).generisch is False
    assert studio.data.generisch is False
    assert studio_generisch.data.generisch is True


async def test_api_aktuell_und_info_generisch(app_client_generisch, studio_generisch, fake_generisch, uhr):
    await plan(studio_generisch, "wohnzimmer")
    d = await (await app_client_generisch.get("/api/aktuell")).json()
    assert (d["raeume"]["wohnzimmer"]["app_modus"], d["raeume"]["wohnzimmer"]["plan_soll"]) == ("plan", 17.0)
    assert d["raeume"]["kuche"]["app_modus"] == "hand"
    info = await (await app_client_generisch.get("/api/info")).json()
    assert info["absenktemperatur"] == 17.0


async def test_api_aktuell_und_info_pm_unveraendert(app_client):
    d = await (await app_client.get("/api/aktuell")).json()
    assert not [r for r in d["raeume"].values() if "app_modus" in r or "plan_soll" in r]
    info = await (await app_client.get("/api/info")).json()
    assert "absenktemperatur" not in info
