"""Optionen und automatische Raumerkennung."""

from __future__ import annotations

import json

from fake_ha import FakeHA
from klimastudio.config import Options, discover_rooms


def test_options_defaults_and_normalisation(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(
        json.dumps(
            {
                "bericht_tag": "Freitag",
                "bericht_uhrzeit": "25:00",
                "bericht_notify": ["notify.mobile_app_bewohner_a", "  mobile_app_tablet ", ""],
                "raeume_override": [{"raum": "buro", "climate": "climate.buro_thermostat"}, {"name": "ohne raum"}],
                "log_level": "LAUT",
            }
        )
    )
    o = Options.load(p)
    assert o.bericht_tag == "freitag"
    assert o.bericht_weekday == 4
    assert o.bericht_uhrzeit == "18:00"
    assert o.bericht_notify == ["mobile_app_bewohner_a", "mobile_app_tablet"]
    assert len(o.raeume) == 1
    assert o.log_level == "info"
    assert Options.load(tmp_path / "fehlt.json").bericht_tag == "sonntag"


def test_discovery_matches_live_layout():
    states = FakeHA().state_list()
    rooms = {r.raum: r for r in discover_rooms(states, Options())}
    assert set(rooms) == {"wohnzimmer", "kuche", "schlafzimmer", "badezimmer"}
    wz = rooms["wohnzimmer"]
    assert wz.name == "Wohnzimmer"
    assert wz.schedule == "schedule.heizplan_wohnzimmer"
    # Zwei Feuchtesensoren enthalten "wohnzimmer": gewählt wird der vom Thermostat gemeldete Wert
    assert wz.feuchte == "sensor.wohnzimmerluftfeuchte"
    # CO2-Sensor ohne Raumnamen wird über das co2-Attribut der Luftqualität gefunden
    assert wz.co2 == "sensor.wetterstation_kohlendioxid"
    assert wz.fenster == "binary_sensor.pm_wohnzimmer_fenster_offen"
    assert rooms["kuche"].name == "Küche"
    assert rooms["kuche"].fenster == "binary_sensor.pm_kuche_fenster_offen"
    assert rooms["schlafzimmer"].fenster is None
    assert rooms["badezimmer"].co2 is None
    assert rooms["badezimmer"].schimmel == "sensor.pm_badezimmer_schimmelrisiko"


def test_overrides_prefix_hide_and_order():
    states = FakeHA().state_list()
    opts = Options.from_dict(
        {
            "raeume_override": [
                {"raum": "badezimmer", "name": "Bad", "co2": "sensor.bad_co2"},
                {"raum": "schlafzimmer", "ausblenden": True},
                {"raum": "kuche", "feuchte": "kein gültiger wert"},
            ]
        }
    )
    rooms = discover_rooms(states, opts)
    assert [r.raum for r in rooms][:2] == ["badezimmer", "kuche"]
    assert "schlafzimmer" not in [r.raum for r in rooms]
    bad = rooms[0]
    assert bad.name == "Bad"
    assert bad.co2 == "sensor.bad_co2"
    assert rooms[1].feuchte == "sensor.kuche_luftfeuchtigkeit"


def test_custom_prefix():
    states = [
        {"entity_id": "climate.heiz_flur", "state": "auto", "attributes": {"friendly_name": "Flur Thermostat"}},
        {"entity_id": "schedule.plan_flur", "state": "off", "attributes": {}},
        {"entity_id": "climate.pm_wohnzimmer", "state": "auto", "attributes": {}},
    ]
    rooms = discover_rooms(states, Options.from_dict({"praefix_klima": "climate.heiz_", "praefix_heizplan": "schedule.plan_"}))
    assert [(r.raum, r.name, r.schedule) for r in rooms] == [("flur", "Flur", "schedule.plan_flur")]


def test_override_domains_are_enforced():
    states = FakeHA().state_list()
    opts = Options.from_dict(
        {
            "raeume_override": [
                {
                    "raum": "kuche",
                    "schedule": "input_datetime.heizplan_kuche",
                    "fenster": "sensor.kein_binary",
                    "co2": "sensor.kuche_co2",
                },
                {"raum": "badezimmer", "schedule": "schedule.bad_neu", "climate": "switch.bad"},
            ]
        }
    )
    rooms = {r.raum: r for r in discover_rooms(states, opts)}
    assert rooms["kuche"].schedule == "schedule.heizplan_kuche"
    assert rooms["kuche"].fenster == "binary_sensor.pm_kuche_fenster_offen"
    assert rooms["kuche"].co2 == "sensor.kuche_co2"
    assert rooms["badezimmer"].schedule == "schedule.bad_neu"
    assert rooms["badezimmer"].climate == "climate.pm_badezimmer"


def test_schedule_prefix_must_be_schedule_domain():
    states = [
        {"entity_id": "climate.pm_flur", "state": "auto", "attributes": {}},
        {"entity_id": "input_text.plan_flur", "state": "x", "attributes": {}},
    ]
    rooms = discover_rooms(states, Options.from_dict({"praefix_heizplan": "input_text.plan_"}))
    assert rooms[0].schedule is None
