"""Betriebsart generisch (Gerüst): Fake-HA, Raumerkennung per Bereich, Raumzustand, Aktionen -> 409."""

from __future__ import annotations

import logging

import pytest
from aiohttp import web

from fake_ha import FakeHA
from klimastudio.adapter.generisch import GenerischAdapter
from klimastudio.coach import ki
from klimastudio.config import Options
from klimastudio.ha_client import HAError

NICHT_VERFUEGBAR = {"fehler": "Im Modus Generisch noch nicht verfügbar.", "code": "nicht_verfuegbar"}


def _registries(fake: FakeHA) -> dict:
    return {
        "areas": fake.handle({"type": "config/area_registry/list"}),
        "entities": fake.handle({"type": "config/entity_registry/list"}),
        "devices": fake.handle({"type": "config/device_registry/list"}),
    }


def _erkennen(fake: FakeHA, **optionen) -> dict:
    adapter = GenerischAdapter(Options.from_dict({"betriebsart": "generisch", **optionen}), None)
    return {r.raum: r for r in adapter.raeume_erkennen(fake.state_list(), _registries(fake))}


def _zustand(fake: FakeHA, raum: str) -> dict:
    adapter = GenerischAdapter(Options.from_dict({"betriebsart": "generisch"}), None)
    states = fake.state_list()
    room = {r.raum: r for r in adapter.raeume_erkennen(states, _registries(fake))}[raum]
    return adapter.raum_zustand(room, {s["entity_id"]: s for s in states})


# ------------------------------------------------------------------ Fake-HA


def test_fake_generisch_ohne_pm_entitaeten():
    ids = {s["entity_id"] for s in FakeHA(pm_klima=False).state_list()}
    assert not [e for e in ids if "pm_" in e]
    assert {
        "climate.wohnzimmer_thermostat",
        "climate.kueche_heizkoerper",
        "climate.bad_klima",
        "climate.schlafzimmer_thermostat",
        "climate.gaestezimmer_heizung",
        "schedule.heizplan_wohnzimmer",
        "sensor.aussentemperatur",
    } <= ids


def test_fake_pm_unveraendert():
    fake = FakeHA()
    ids = {s["entity_id"] for s in fake.state_list()}
    assert "climate.pm_wohnzimmer" in ids
    assert "switch.pm_heizung_aktiv" in ids
    assert not [e for e in ids if e.startswith("climate.") and not e.startswith("climate.pm_")]
    assert fake.handle({"type": "config/area_registry/list"}) == []


async def test_fake_pm_heizung_dienst_nicht_gefunden(ha_client_generisch, fake_generisch):
    with pytest.raises(HAError) as err:
        await ha_client_generisch.call_service("pm_heizung", "boost", {"entity_id": "climate.kueche_heizkoerper", "dauer": 30})
    assert err.value.code == "400"
    assert "not found" in str(err.value)
    # über WebSocket: Fehlercode not_found
    with pytest.raises(HAError) as err:
        await ha_client_generisch.ws_command(
            {"type": "call_service", "domain": "pm_heizung", "service": "set_overlay", "service_data": {}}
        )
    assert "not found" in str(err.value)
    assert fake_generisch.generisch["climate.kueche_heizkoerper"]["attributes"].get("preset_mode") == "none"


async def test_fake_hvac_modes_werden_geprueft(ha_client_generisch, fake_generisch):
    eid = "climate.wohnzimmer_thermostat"
    with pytest.raises(HAError) as err:
        await ha_client_generisch.call_service("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "auto"})
    assert "not valid" in str(err.value)
    assert fake_generisch.generisch[eid]["state"] == "heat"
    await ha_client_generisch.call_service("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "off"})
    assert fake_generisch.generisch[eid]["state"] == "off"
    await ha_client_generisch.call_service("climate", "turn_on", {"entity_id": eid})
    assert fake_generisch.generisch[eid]["state"] == "heat"


async def test_fake_preset_und_temperatur(ha_client_generisch, fake_generisch):
    eid = "climate.kueche_heizkoerper"
    await ha_client_generisch.call_service("climate", "set_preset_mode", {"entity_id": eid, "preset_mode": "boost"})
    assert fake_generisch.generisch[eid]["attributes"]["preset_mode"] == "boost"
    with pytest.raises(HAError):
        await ha_client_generisch.call_service("climate", "set_preset_mode", {"entity_id": eid, "preset_mode": "turbo"})
    with pytest.raises(HAError):  # ohne PRESET_MODE
        await ha_client_generisch.call_service(
            "climate", "set_preset_mode", {"entity_id": "climate.wohnzimmer_thermostat", "preset_mode": "boost"}
        )
    await ha_client_generisch.call_service("climate", "set_temperature", {"entity_id": eid, "temperature": 22.5})
    assert fake_generisch.generisch[eid]["attributes"]["temperature"] == 22.5
    with pytest.raises(HAError):  # Bereichs-Thermostat ohne TARGET_TEMPERATURE
        await ha_client_generisch.call_service(
            "climate", "set_temperature", {"entity_id": "climate.bad_klima", "temperature": 21}
        )
    # nicht verfügbare Entitäten werden wie in HA übersprungen
    await ha_client_generisch.call_service(
        "climate", "set_temperature", {"entity_id": "climate.schlafzimmer_thermostat", "temperature": 21}
    )
    assert "temperature" not in fake_generisch.generisch["climate.schlafzimmer_thermostat"]["attributes"]


# ------------------------------------------------------------------ Raumerkennung


def test_raeume_aus_bereichen():
    rooms = _erkennen(FakeHA(pm_klima=False))
    assert set(rooms) == {"wohnzimmer", "kuche", "badezimmer", "schlafzimmer", "gaestezimmer_heizung"}
    wz = rooms["wohnzimmer"]
    assert (wz.name, wz.climate) == ("Wohnzimmer", "climate.wohnzimmer_thermostat")
    assert wz.schedule == "schedule.heizplan_wohnzimmer"
    # Bereich über das Gerät (Entität selbst ohne Bereich)
    assert (rooms["kuche"].name, rooms["kuche"].climate) == ("Küche", "climate.kueche_heizkoerper")
    assert rooms["schlafzimmer"].climate == "climate.schlafzimmer_thermostat"
    # Flur hat kein Thermostat und bildet keinen Raum
    assert "flur" not in rooms


def test_raum_ohne_bereich():
    gz = _erkennen(FakeHA(pm_klima=False))["gaestezimmer_heizung"]
    assert gz.name == "Gästezimmer"
    assert gz.climate == "climate.gaestezimmer_heizung"
    assert gz.schedule is None


def test_sensorzuordnung_nach_bereich_und_device_class():
    rooms = _erkennen(FakeHA(pm_klima=False))
    wz = rooms["wohnzimmer"]
    # Sensoren des Geräts „Raumklima Ost“ im Bereich Wohnzimmer (Namen ohne Raumkürzel)
    assert wz.feuchte == "sensor.raumklima_ost_feuchte"
    assert wz.co2 == "sensor.raumklima_ost_co2"
    assert wz.temperatur == "sensor.raumklima_ost_temperatur"
    assert rooms["kuche"].fenster == "binary_sensor.kontakt_12"
    # Rückfall: Sensor ohne Bereich mit dem Raumkürzel im Namen
    assert rooms["kuche"].feuchte == "sensor.kuche_luftfeuchtigkeit"
    assert rooms["badezimmer"].feuchte == "sensor.bad_feuchte"
    assert rooms["badezimmer"].temperatur == "sensor.bad_temperatur"
    # Sensor eines anderen Bereichs (Flur) wird keinem Raum zugeordnet
    assert all(r.feuchte != "sensor.flur_feuchte" for r in rooms.values())
    assert rooms["schlafzimmer"].feuchte is None
    assert rooms["gaestezimmer_heizung"].feuchte is None
    # Felder, die es nur mit PM Klima gibt
    assert all(r.schimmel is None and r.luftqualitaet is None and r.lueften is None for r in rooms.values())


def test_mehrere_thermostate_in_einem_bereich(caplog):
    fake = FakeHA(pm_klima=False)
    fake.generisch["climate.a_wohnzimmer_zusatz"] = {
        "state": "heat",
        "attributes": {"friendly_name": "Zusatz", "hvac_modes": ["heat", "off"], "supported_features": 385, "temperature": 20},
    }
    fake.registry["climate.a_wohnzimmer_zusatz"] = {"area_id": "wohnzimmer", "device_id": None, "hidden_by": None}
    with caplog.at_level(logging.INFO):
        rooms = _erkennen(fake)
    assert rooms["wohnzimmer"].climate == "climate.a_wohnzimmer_zusatz"  # erstes (sortiert)
    assert "mehrere Thermostate" in caplog.text
    assert len([r for r in rooms.values() if r.climate and "wohnzimmer" in r.climate]) == 1
    # Override wählt das gewünschte Thermostat
    rooms = _erkennen(fake, raeume_override=[{"raum": "wohnzimmer", "climate": "climate.wohnzimmer_thermostat"}])
    assert rooms["wohnzimmer"].climate == "climate.wohnzimmer_thermostat"


def test_overrides_wie_bisher():
    fake = FakeHA(pm_klima=False)
    rooms = _erkennen(
        fake,
        raeume_override=[
            {"raum": "kuche", "name": "Kochen", "feuchte": "sensor.bad_feuchte", "fenster": "kein gültiger wert"},
            {"raum": "gaestezimmer_heizung", "ausblenden": True},
            {"raum": "keller", "climate": "climate.keller", "schedule": "schedule.heizplan_keller"},
        ],
    )
    assert list(rooms) == ["kuche", "keller", "badezimmer", "schlafzimmer", "wohnzimmer"]
    assert rooms["kuche"].name == "Kochen"
    assert rooms["kuche"].feuchte == "sensor.bad_feuchte"
    assert rooms["kuche"].fenster == "binary_sensor.kontakt_12"
    assert rooms["keller"].climate == "climate.keller"


def test_ausgeblendete_entitaet_und_namenskollision():
    fake = FakeHA(pm_klima=False)
    fake.registry["climate.bad_klima"]["hidden_by"] = "user"
    # Thermostat ohne Bereich, dessen object_id einem Bereich entspricht
    fake.generisch["climate.kuche"] = {"state": "heat", "attributes": {"friendly_name": "Küche 2", "supported_features": 1}}
    rooms = _erkennen(fake)
    assert "badezimmer" not in rooms
    assert rooms["kuche"].climate == "climate.kueche_heizkoerper"
    assert rooms["kuche_2"].climate == "climate.kuche"


def test_ohne_registries_raeume_aus_object_id():
    adapter = GenerischAdapter(Options.from_dict({"betriebsart": "generisch"}), None)
    rooms = {r.raum: r for r in adapter.raeume_erkennen(FakeHA(pm_klima=False).state_list(), {})}
    assert set(rooms) == {
        "wohnzimmer_thermostat",
        "kueche_heizkoerper",
        "bad_klima",
        "schlafzimmer_thermostat",
        "gaestezimmer_heizung",
    }
    assert rooms["wohnzimmer_thermostat"].name == "Wohnzimmer"


async def test_registries_laden_mit_rueckfall(studio_generisch, fake_generisch):
    fake_generisch.fail_ws = {"config/entity_registry/list_for_display"}
    rooms = {r.raum: r for r in await studio_generisch.rooms(refresh=True)}
    assert rooms["kuche"].climate == "climate.kueche_heizkoerper"
    typen = [c["type"] for c in fake_generisch.ws_commands]
    assert "config/entity_registry/list" in typen


async def test_registries_nicht_lesbar(studio_generisch, fake_generisch):
    fake_generisch.fail_ws = {
        "config/area_registry/list",
        "config/entity_registry/list_for_display",
        "config/entity_registry/list",
        "config/device_registry/list",
    }
    rooms = await studio_generisch.rooms(refresh=True)
    assert "wohnzimmer_thermostat" in {r.raum for r in rooms}


async def test_pm_modus_liest_keine_registries(studio, fake):
    await studio.rooms(refresh=True)
    assert not [
        c for c in fake.ws_commands if c["type"].startswith(("config/area", "config/device", "config/entity_registry/list"))
    ]


# ------------------------------------------------------------------ Raumzustand


def test_zustand_steuerbar():
    z = _zustand(FakeHA(pm_klima=False), "wohnzimmer")
    assert z["modus"] == "heat"
    assert z["soll"] == 21.0
    assert z["ist"] == 20.4
    assert (z["min_temp"], z["max_temp"], z["schritt"]) == (7.0, 30.0, 0.5)
    assert z["steuerbar"] is True
    assert z["steuerbar_grund"] is None
    assert z["faehigkeiten"] == {"modi": ["heat", "off"], "presets": [], "boost": False, "aus": True}
    # PM-Felder vorhanden, aber leer
    assert z["overlay_bis"] is None
    assert z["boost_bis"] is None
    assert z["zeitplan_temperatur"] is None
    assert z["feuchte"] is not None  # aus dem Feuchtesensor des Bereichs


def test_zustand_presets_und_fenster():
    z = _zustand(FakeHA(pm_klima=False), "kuche")
    assert z["steuerbar"] is True
    assert z["preset"] == "none"
    assert z["faehigkeiten"]["boost"] is True
    assert z["feuchte"] == 55.0  # current_humidity des Thermostats vor dem Sensor
    assert z["fenster_offen"] in (True, False)


def test_zustand_bereichs_thermostat_nicht_steuerbar():
    z = _zustand(FakeHA(pm_klima=False), "badezimmer")
    assert z["steuerbar"] is False
    assert "Temperaturbereich" in z["steuerbar_grund"]
    assert z["soll"] is None
    assert z["soll_bereich"] == {"min": 20.0, "max": 24.0}


def test_zustand_nicht_verfuegbar():
    z = _zustand(FakeHA(pm_klima=False), "schlafzimmer")
    assert z["modus"] == "unavailable"
    assert z["steuerbar"] is False
    assert "nicht verfügbar" in z["steuerbar_grund"]


def test_zustand_ohne_solltemperatur_und_ist_aus_sensor():
    fake = FakeHA(pm_klima=False)
    a = fake.generisch["climate.wohnzimmer_thermostat"]["attributes"]
    a["supported_features"] = 128  # nur TURN_OFF
    a["current_temperature"] = None
    z = _zustand(fake, "wohnzimmer")
    assert z["steuerbar"] is False
    assert "Solltemperatur" in z["steuerbar_grund"]
    assert z["ist"] is not None  # Temperatursensor des Bereichs


def test_zustand_ohne_features_attribut():
    fake = FakeHA(pm_klima=False)
    del fake.generisch["climate.gaestezimmer_heizung"]["attributes"]["supported_features"]
    assert _zustand(fake, "gaestezimmer_heizung")["steuerbar"] is True


def test_zustand_ohne_thermostat():
    adapter = GenerischAdapter(Options.from_dict({"betriebsart": "generisch"}), None)
    from klimastudio.config import Room

    f = adapter.raum_faehigkeiten(Room(raum="keller", name="Keller"), {})
    assert f["steuerbar"] is False
    assert f["grund"] == "Kein Thermostat zugeordnet."
    f = adapter.raum_faehigkeiten(Room(raum="keller", name="Keller", climate="climate.keller"), {})
    assert "nicht vorhanden" in f["grund"]


async def test_api_steuerung_generisch(app_client_generisch, fake_generisch):
    resp = await app_client_generisch.get("/api/steuerung")
    d = await resp.json()
    assert resp.status == 200, d
    assert d["integration"] is None
    assert d["raeume"]["wohnzimmer"]["steuerbar"] is True
    assert d["raeume"]["badezimmer"]["steuerbar"] is False
    assert d["heizperiode"]["verknuepft"] is None


# ------------------------------------------------------------------ Aktionen und Heizperiode -> 409


@pytest.mark.parametrize(
    "body",
    [
        {"aktion": "overlay", "temperatur": 21, "dauer": 60},
        {"aktion": "temperatur", "temperatur": 21},
        {"aktion": "boost", "dauer": 30},
        {"aktion": "zurueck"},
        {"aktion": "modus", "modus": "heat"},
    ],
)
async def test_aktionen_generisch_409(app_client_generisch, fake_generisch, body):
    resp = await app_client_generisch.post("/api/steuerung/wohnzimmer", json=body)
    assert resp.status == 409
    assert await resp.json() == NICHT_VERFUEGBAR
    assert fake_generisch.service_calls == []


async def test_aktion_generisch_direkt_409(studio_generisch):
    room = await studio_generisch.room("wohnzimmer")
    with pytest.raises(web.HTTPConflict) as err:
        await studio_generisch.adapter.aktion(room, {"aktion": "zurueck"})
    assert err.value.status == 409


async def test_heizperiode_generisch(app_client_generisch, studio_generisch, fake_generisch):
    resp = await app_client_generisch.get("/api/heizperiode")
    assert resp.status == 200
    for pfad, body in (("/api/heizperiode", {"modus": "sommer"}), ("/api/heizperiode/einrichten", {})):
        resp = await app_client_generisch.post(pfad, json=body)
        assert resp.status == 409
        assert await resp.json() == NICHT_VERFUEGBAR
    assert fake_generisch.service_calls == []
    assert not [c for c in fake_generisch.ws_commands if c["type"] == "input_boolean/create"]
    studio_generisch.heizperiode.start()
    assert studio_generisch.heizperiode._task is None
    with pytest.raises(web.HTTPConflict) as err:
        await studio_generisch.adapter.heizperiode_anwenden(True, "input_boolean.pm_heizperiode")
    assert err.value.status == 409


# ------------------------------------------------------------------ Coach


async def test_coach_generisch(app_client_generisch, fake_generisch):
    resp = await app_client_generisch.get("/api/coach")
    d = await resp.json()
    assert resp.status == 200, d
    assert d["integration"] == []
    resp = await app_client_generisch.get("/api/coach/lagebericht")
    lage = (await resp.json())["lage"]
    assert resp.status == 200
    assert lage["integration"] == {"aktiv": None, "gesperrt": None, "sperre_grund": None, "sperre_wirkung": None}
    assert {r["raum"] for r in lage["raeume"]} >= {"wohnzimmer", "kuche"}
    resp = await app_client_generisch.post("/api/coach/ki", json={})
    assert resp.status == 200, await resp.text()
    anweisung = next(c[2]["instructions"] for c in fake_generisch.service_calls if c[0] == "ai_task")
    assert anweisung.startswith(ki.EINLEITUNG_GENERISCH)
    assert "Heizungsintegration PM Klima" not in anweisung


async def test_coach_pm_prompt_unveraendert(app_client, fake):
    resp = await app_client.post("/api/coach/ki", json={})
    assert resp.status == 200
    anweisung = next(c[2]["instructions"] for c in fake.service_calls if c[0] == "ai_task")
    assert anweisung.startswith(ki.PROMPT)
    assert ki.PROMPT.startswith("Sie sind der Klima-Coach für ein Zuhause mit der Heizungsintegration PM Klima.")
