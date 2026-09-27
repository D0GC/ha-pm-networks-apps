"""Betriebsart: Optionen, Adapter-Fabrik, Erkennung von PM Klima, /api/info und neue HA-Client-Befehle."""

from __future__ import annotations

import logging

import pytest

from klimastudio.adapter import (
    FAEHIGKEITEN,
    HINWEIS_PM_FEHLT,
    HINWEIS_PM_VORHANDEN,
    PmKlimaErkennung,
    adapter_fuer,
    hinweis_betriebsart,
    pm_klima_geladen,
)
from klimastudio.adapter.generisch import GenerischAdapter
from klimastudio.adapter.pm_klima import PmKlimaAdapter
from klimastudio.config import Options
from klimastudio.ha_client import HAError

# ------------------------------------------------------------------ Optionen


def test_optionen_standardwerte():
    o = Options.from_dict({})
    assert o.betriebsart == "pm_networks"
    assert o.plan_anwenden is True
    assert o.sommer_aktion == "plan_pausieren_und_aus"
    assert o.absenktemperatur == 17.0


def test_optionen_gueltige_werte():
    o = Options.from_dict(
        {"betriebsart": " Generisch ", "plan_anwenden": False, "sommer_aktion": "plan_pausieren", "absenktemperatur": 16.5}
    )
    assert o.betriebsart == "generisch"
    assert o.plan_anwenden is False
    assert o.sommer_aktion == "plan_pausieren"
    assert o.absenktemperatur == 16.5
    assert Options.from_dict({"absenktemperatur": 18}).absenktemperatur == 18.0


@pytest.mark.parametrize(
    ("raw", "feld", "erwartet"),
    [
        ({"betriebsart": "pm_klima"}, "betriebsart", "pm_networks"),
        ({"betriebsart": 1}, "betriebsart", "pm_networks"),
        ({"betriebsart": ""}, "betriebsart", "pm_networks"),
        ({"sommer_aktion": "aus"}, "sommer_aktion", "plan_pausieren_und_aus"),
        ({"plan_anwenden": "false"}, "plan_anwenden", True),
        ({"absenktemperatur": 30}, "absenktemperatur", 17.0),
        ({"absenktemperatur": 4.5}, "absenktemperatur", 17.0),
        ({"absenktemperatur": 17.3}, "absenktemperatur", 17.0),
        ({"absenktemperatur": "17"}, "absenktemperatur", 17.0),
        ({"absenktemperatur": True}, "absenktemperatur", 17.0),
        ({"absenktemperatur": float("nan")}, "absenktemperatur", 17.0),
    ],
)
def test_optionen_ungueltig_mit_warnung(raw, feld, erwartet, caplog):
    with caplog.at_level(logging.WARNING):
        o = Options.from_dict(raw)
    assert getattr(o, feld) == erwartet
    if raw[feld] != "":
        assert feld in caplog.text


# ------------------------------------------------------------------ Fabrik


def test_fabrik_waehlt_adapter():
    pm = adapter_fuer(Options.from_dict({}), None)
    gen = adapter_fuer(Options.from_dict({"betriebsart": "generisch"}), None)
    assert isinstance(pm, PmKlimaAdapter)
    assert pm.betriebsart == "pm_networks"
    assert pm.braucht_registries is False
    assert pm.heizperiode_wirksam is True
    assert isinstance(gen, GenerischAdapter)
    assert gen.betriebsart == "generisch"
    assert gen.braucht_registries is True
    assert gen.heizperiode_wirksam is True  # ab 1.2.0 Phase 3: interne Heizperiode
    assert tuple(pm.faehigkeiten()) == FAEHIGKEITEN
    assert tuple(gen.faehigkeiten()) == FAEHIGKEITEN
    assert pm.faehigkeiten()["freigabe"] is True
    # ab 1.2.0 Phase 3 nur plan_anwendung (Option plan_anwenden, Standard an)
    assert [k for k, v in gen.faehigkeiten().items() if v] == ["plan_anwendung"]


def test_studio_nutzt_adapter_der_betriebsart(studio, studio_generisch):
    assert studio.adapter.betriebsart == "pm_networks"
    assert studio.heizperiode.adapter is studio.adapter
    assert studio_generisch.adapter.betriebsart == "generisch"
    assert studio_generisch.heizperiode.adapter is studio_generisch.adapter


def test_hinweis_betriebsart():
    assert hinweis_betriebsart("pm_networks", True) is None
    assert hinweis_betriebsart("pm_networks", None) is None
    assert hinweis_betriebsart("pm_networks", False) == HINWEIS_PM_FEHLT
    assert hinweis_betriebsart("generisch", False) is None
    assert hinweis_betriebsart("generisch", True) == HINWEIS_PM_VORHANDEN


# ------------------------------------------------------------------ Erkennung PM Klima


async def test_erkennung_geladen(ha_client, fake):
    assert await pm_klima_geladen(ha_client) is True
    cmd = next(c for c in fake.ws_commands if c["type"] == "config_entries/get")
    assert cmd["domain"] == "pm_heizung"
    assert not [c for c in fake.ws_commands if c["type"] == "get_services"]


@pytest.mark.parametrize("zustand", ["not_loaded", "setup_error", "setup_retry", None])
async def test_erkennung_nicht_geladen(ha_client, fake, zustand):
    fake.pm_eintrag = zustand
    assert await pm_klima_geladen(ha_client) is False


async def test_erkennung_rueckfall_auf_dienste(ha_client, fake):
    fake.fail_ws = {"config_entries/get"}
    assert await pm_klima_geladen(ha_client) is True
    assert [c["type"] for c in fake.ws_commands][-2:] == ["config_entries/get", "get_services"]
    fake.pm_dienste = False
    assert await pm_klima_geladen(ha_client) is False


async def test_erkennung_unbekannt(ha_client, fake):
    fake.fail_ws = {"config_entries/get", "get_services"}
    assert await pm_klima_geladen(ha_client) is None


async def test_erkennung_wird_gecacht(ha_client, fake):
    erkennung = PmKlimaErkennung(ha_client, ttl=300)
    assert await erkennung.geladen() is True
    fake.pm_eintrag = "not_loaded"
    assert await erkennung.geladen() is True  # aus dem Cache
    assert len([c for c in fake.ws_commands if c["type"] == "config_entries/get"]) == 1
    assert await erkennung.geladen(refresh=True) is False
    erkennung.ttl = 0
    fake.pm_eintrag = "loaded"
    assert await erkennung.geladen() is True


async def test_erkennung_unbekannt_nicht_gecacht(ha_client, fake):
    erkennung = PmKlimaErkennung(ha_client, ttl=300)
    fake.fail_ws = {"config_entries/get", "get_services"}
    assert await erkennung.geladen() is None
    fake.fail_ws = set()
    assert await erkennung.geladen() is True


# ------------------------------------------------------------------ /api/info


async def test_info_pm_networks(app_client):
    info = await (await app_client.get("/api/info")).json()
    assert info["betriebsart"] == "pm_networks"
    assert info["pm_klima_geladen"] is True
    assert info["hinweis_betriebsart"] is None
    assert info["faehigkeiten"] == {
        "freigabe": True,
        "overlay_integration": True,
        "plan_anwendung": False,
        "empfehlungen_integration": True,
        "schimmel": True,
        "luftqualitaet": True,
    }
    # Räume unverändert (ohne das neue Feld temperatur)
    assert all("temperatur" not in r for r in info["raeume"])


async def test_info_pm_networks_ohne_pm_klima(app_client, fake):
    fake.pm_eintrag = "not_loaded"
    info = await (await app_client.get("/api/info")).json()
    assert info["betriebsart"] == "pm_networks"  # nie automatisch umschalten
    assert info["pm_klima_geladen"] is False
    assert info["hinweis_betriebsart"] == HINWEIS_PM_FEHLT
    assert "generisch" in info["hinweis_betriebsart"]


async def test_info_pm_klima_unbekannt(app_client, fake):
    fake.fail_ws = {"config_entries/get", "get_services"}
    info = await (await app_client.get("/api/info")).json()
    assert info["pm_klima_geladen"] is None
    assert info["hinweis_betriebsart"] is None


async def test_info_generisch(app_client_generisch):
    info = await (await app_client_generisch.get("/api/info")).json()
    assert info["betriebsart"] == "generisch"
    assert info["pm_klima_geladen"] is False
    assert info["hinweis_betriebsart"] is None
    assert set(info["faehigkeiten"]) == set(FAEHIGKEITEN)
    # ab 1.2.0 Phase 3 nur plan_anwendung (Option plan_anwenden, Standard an)
    assert [k for k, v in info["faehigkeiten"].items() if v] == ["plan_anwendung"]
    assert [r["raum"] for r in info["raeume"]] == ["badezimmer", "gaestezimmer_heizung", "kuche", "schlafzimmer", "wohnzimmer"]


async def test_info_generisch_mit_pm_klima(app_client_generisch, fake_generisch):
    fake_generisch.pm_eintrag = "loaded"
    info = await (await app_client_generisch.get("/api/info")).json()
    assert info["betriebsart"] == "generisch"
    assert info["pm_klima_geladen"] is True
    assert info["hinweis_betriebsart"] == HINWEIS_PM_VORHANDEN


# ------------------------------------------------------------------ HA-Client: neue Befehle


async def test_client_config_entries_get(ha_client, fake):
    eintraege = await ha_client.config_entries_get("pm_heizung")
    assert [e["domain"] for e in eintraege] == ["pm_heizung"]
    assert eintraege[0]["state"] == "loaded"
    assert {k: v for k, v in fake.ws_commands[-1].items() if k != "id"} == {
        "type": "config_entries/get",
        "domain": "pm_heizung",
    }
    alle = await ha_client.config_entries_get()
    assert "domain" not in fake.ws_commands[-1]
    assert len(alle) == 2


async def test_client_get_services(ha_client, ha_client_generisch):
    assert "pm_heizung" in await ha_client.get_services()
    dienste = await ha_client_generisch.get_services()
    assert "pm_heizung" not in dienste
    assert "set_preset_mode" in dienste["climate"]


async def test_client_registries(ha_client_generisch, fake_generisch):
    areas = await ha_client_generisch.area_registry_list()
    assert {a["area_id"] for a in areas} >= {"wohnzimmer", "kuche", "flur"}
    geraete = await ha_client_generisch.device_registry_list()
    assert {"id": "dev_kueche_heizkoerper", "name": "Heizkörper Küche", "area_id": "kuche"} in geraete
    voll = {e["entity_id"]: e for e in await ha_client_generisch.entity_registry_list()}
    assert voll["climate.kueche_heizkoerper"]["area_id"] is None
    assert voll["climate.kueche_heizkoerper"]["device_id"] == "dev_kueche_heizkoerper"
    fake_generisch.registry["climate.bad_klima"]["hidden_by"] = "user"
    kompakt = {e["entity_id"]: e for e in await ha_client_generisch.entity_registry_list_for_display()}
    assert kompakt["climate.wohnzimmer_thermostat"] == {
        "entity_id": "climate.wohnzimmer_thermostat",
        "platform": "demo",
        "area_id": "wohnzimmer",
        "device_id": "dev_wz_thermostat",
        "hidden": False,
    }
    assert kompakt["climate.bad_klima"]["hidden"] is True
    assert kompakt["weather.dwd_zuhause"]["area_id"] is None
    assert set(kompakt) == set(voll)


async def test_client_registry_fehler(ha_client, fake):
    fake.fail_ws = {"config/area_registry/list"}
    with pytest.raises(HAError):
        await ha_client.area_registry_list()
    # PM-Instanz: keine Bereiche
    fake.fail_ws = set()
    assert await ha_client.area_registry_list() == []
