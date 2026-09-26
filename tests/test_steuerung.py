"""Raumsteuerung über pm_heizung.* / climate.* (nie direkt an Thermostat-Backends)."""

from __future__ import annotations

import pytest

from klimastudio.steuerung import pruefe_befehl


async def _post(client, raum, body):
    resp = await client.post(f"/api/steuerung/{raum}", json=body)
    return resp.status, await resp.json()


async def test_get_steuerung(app_client, fake):
    resp = await app_client.get("/api/steuerung")
    d = await resp.json()
    assert resp.status == 200, d
    wz = d["raeume"]["wohnzimmer"]
    assert wz["climate"] == "climate.pm_wohnzimmer"
    assert wz["modus"] == "auto"
    assert wz["steuerbar"] is True
    assert wz["schritt"] == 0.5
    assert wz["overlay_bis"] is None
    assert set(wz) >= {
        "name",
        "preset",
        "grund",
        "anzeige",
        "hvac_action",
        "ist",
        "soll",
        "feuchte",
        "zeitplan_temperatur",
        "naechster_wechsel",
        "naechster_wechsel_grund",
        "boost_bis",
        "fenster_offen",
        "min_temp",
        "max_temp",
    }
    assert d["integration"] == {
        "aktiv": True,
        "gesperrt": False,
        "sperre_grund": None,
        "sperre_wirkung": "aus",
        "beschreibung": "Keine Sperre aktiv",
    }
    assert d["heizperiode"]["entitaet"] == "input_boolean.pm_heizperiode"
    assert d["heizperiode"]["vorhanden"] is False


async def test_overlay_with_and_without_duration(app_client, fake, studio):
    status, d = await _post(app_client, "wohnzimmer", {"aktion": "overlay", "temperatur": 21.5, "dauer": 60})
    assert status == 200, d
    assert fake.service_calls[-1] == (
        "pm_heizung",
        "set_overlay",
        {"entity_id": "climate.pm_wohnzimmer", "temperatur": 21.5, "dauer": 60},
    )
    assert d["ok"] is True
    assert d["raum"]["overlay_bis"] is not None
    assert d["raum"]["soll"] == 21.5
    # dauer=null -> Feld weglassen
    status, d = await _post(app_client, "kuche", {"aktion": "overlay", "temperatur": 20, "dauer": None})
    assert status == 200
    assert fake.service_calls[-1][2] == {"entity_id": "climate.pm_kuche", "temperatur": 20}
    # dauerhaft
    status, d = await _post(app_client, "kuche", {"aktion": "overlay", "temperatur": 20, "dauer": 0})
    assert d["raum"]["overlay_bis"] == "dauerhaft"
    ereignisse = await studio.store.ereignisse("steuerung")
    assert ereignisse[0]["raum"] == "kuche"
    assert "dauerhaft" in ereignisse[0]["text"]
    assert len(ereignisse) == 3


async def test_overlay_requires_auto(app_client, fake):
    fake.climate["wohnzimmer"]["state"] = "heat"
    status, d = await _post(app_client, "wohnzimmer", {"aktion": "overlay", "temperatur": 21})
    assert status == 409
    assert d["code"] == "modus"
    assert fake.service_calls == []


async def test_temperatur_requires_heat(app_client, fake):
    status, d = await _post(app_client, "wohnzimmer", {"aktion": "temperatur", "temperatur": 21})
    assert status == 409
    assert d["code"] == "modus"
    fake.climate["wohnzimmer"]["state"] = "heat"
    status, d = await _post(app_client, "wohnzimmer", {"aktion": "temperatur", "temperatur": 21})
    assert status == 200, d
    assert fake.service_calls[-1] == ("climate", "set_temperature", {"entity_id": "climate.pm_wohnzimmer", "temperature": 21})
    assert d["raum"]["soll"] == 21
    # max_temp des Raums (25) begrenzt
    status, d = await _post(app_client, "wohnzimmer", {"aktion": "temperatur", "temperatur": 28})
    assert status == 400


async def test_boost_zurueck_modus(app_client, fake):
    status, d = await _post(app_client, "badezimmer", {"aktion": "boost", "dauer": 30})
    assert status == 200
    assert fake.service_calls[-1] == ("pm_heizung", "boost", {"entity_id": "climate.pm_badezimmer", "dauer": 30})
    assert d["raum"]["boost_bis"] is not None
    status, d = await _post(app_client, "badezimmer", {"aktion": "zurueck"})
    assert status == 200
    assert fake.service_calls[-1] == ("pm_heizung", "clear_overlay", {"entity_id": "climate.pm_badezimmer"})
    assert d["raum"]["boost_bis"] is None
    status, d = await _post(app_client, "badezimmer", {"aktion": "modus", "modus": "off"})
    assert status == 200
    assert fake.service_calls[-1] == ("climate", "set_hvac_mode", {"entity_id": "climate.pm_badezimmer", "hvac_mode": "off"})
    assert d["raum"]["modus"] == "off"
    # Nur pm_heizung.* und climate.* auf climate.pm_*
    for domain, _, data in fake.service_calls:
        assert domain in ("pm_heizung", "climate")
        assert data["entity_id"].startswith("climate.pm_")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"aktion": "loeschen"},
        {"aktion": "overlay"},
        {"aktion": "overlay", "temperatur": "21"},
        {"aktion": "overlay", "temperatur": True},
        {"aktion": "overlay", "temperatur": 4.5},
        {"aktion": "overlay", "temperatur": 30.5},
        {"aktion": "overlay", "temperatur": 21.3},
        {"aktion": "overlay", "temperatur": 21, "dauer": -1},
        {"aktion": "overlay", "temperatur": 21, "dauer": 1441},
        {"aktion": "overlay", "temperatur": 21, "dauer": 10.5},
        {"aktion": "overlay", "temperatur": 21, "dauer": "60"},
        {"aktion": "overlay", "temperatur": 21, "entity_id": "climate.anderes"},
        {"aktion": "boost"},
        {"aktion": "boost", "dauer": 3},
        {"aktion": "boost", "dauer": 241},
        {"aktion": "boost", "dauer": 32},
        {"aktion": "modus", "modus": "cool"},
        {"aktion": "zurueck", "x": 1},
    ],
)
async def test_validation_errors(app_client, fake, body):
    status, d = await _post(app_client, "wohnzimmer", body)
    assert status == 400, body
    assert d["code"] == "ungueltig"
    assert d["fehler"]
    assert fake.service_calls == []


async def test_unknown_room_and_bad_slug(app_client, fake):
    resp = await app_client.post("/api/steuerung/keller", json={"aktion": "zurueck"})
    assert resp.status == 404
    resp = await app_client.post("/api/steuerung/Wohn%20zimmer", json={"aktion": "zurueck"})
    assert resp.status == 400
    resp = await app_client.post("/api/steuerung/wohnzimmer", data="aktion=zurueck")
    assert resp.status == 415
    assert fake.service_calls == []


async def test_room_not_controlled_by_pm_klima(aiohttp_client, ha_client, fake, tmp_path):
    from conftest import LOCAL, WISSEN_TEST
    from klimastudio.config import Options
    from klimastudio.server import KlimaStudio, create_app

    opts = Options.from_dict({"raeume_override": [{"raum": "buro", "climate": "climate.buro_thermostat"}]})
    ks = KlimaStudio(opts, ha_client, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    client = await aiohttp_client(create_app(ks, networks=LOCAL))
    resp = await client.post("/api/steuerung/buro", json={"aktion": "zurueck"})
    body = await resp.json()
    assert resp.status == 409
    assert body["code"] == "nicht_gesteuert"
    d = await (await client.get("/api/steuerung")).json()
    assert d["raeume"]["buro"]["steuerbar"] is False
    assert fake.service_calls == []
    ks.store.close()


async def test_unavailable_climate(app_client, fake):
    fake.climate["kuche"]["state"] = "unavailable"
    status, d = await _post(app_client, "kuche", {"aktion": "zurueck"})
    assert status == 409
    assert d["code"] == "nicht_verfuegbar"


def test_pruefe_befehl_texts():
    b = pruefe_befehl({"aktion": "overlay", "temperatur": 21.0, "dauer": 90})
    assert b["daten"] == {"temperatur": 21, "dauer": 90}
    assert b["text"] == "Temperatur 21 °C für 90 Minuten"
    b = pruefe_befehl({"aktion": "overlay", "temperatur": 20.5})
    assert b["text"] == "Temperatur 20,5 °C bis zum nächsten Planwechsel"
    assert "!" not in b["text"]
