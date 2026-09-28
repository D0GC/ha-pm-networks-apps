"""Heizperiode / Sommerautomatik: Entscheidung, Schalten der Freigabe, API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from fake_ha import TZ
from klimastudio import heizperiode as hpm
from klimastudio.heizperiode import STANDARD, eindeutig, entscheide, tagesmittel_aus_stunden

HP = "input_boolean.pm_heizperiode"


def einst(**kw):
    return {**STANDARD, **kw}


# ------------------------------------------------------------------ Entscheidung (rein)


def test_start_when_last_days_below_limit():
    aktiv, grund = entscheide([16, 16, 16, 16, 16, 12, 12.9], einst(), False)
    assert aktiv is True
    assert "unter der Heizgrenze" in grund
    # Nur ein Tag kalt reicht nicht (tage_start=2)
    assert entscheide([16, 16, 16, 16, 16, 16, 10], einst(), False)[0] is False


def test_end_requires_limit_plus_hysteresis():
    aktiv, grund = entscheide([10, 10, 10, 10, 15, 15, 15.2], einst(), True)
    assert aktiv is False
    assert "Hysterese" in grund
    # 14,9 liegt unter 13 + 2 -> Übergangsbereich, letzte Entscheidung der Automatik bleibt
    aktiv, grund = entscheide([10, 10, 10, 10, 15, 15, 14.9], einst(), True)
    assert aktiv is True
    assert grund == "Übergangsbereich: letzte Entscheidung der Automatik (Heizperiode) bleibt."
    assert entscheide([10, 10, 10, 10, 15, 15, 14.9], einst(), False)[0] is False


def test_hysteresis_zone_without_automatic_decision():
    # Ohne frühere Automatik-Entscheidung: jüngstes Tagesmittel < 13 + 2/2 -> Heizperiode
    zone = [14, 14, 14, 14, 14, 14, 14]
    aktiv, grund = entscheide([*zone[:-1], 13.7], einst(), None)
    assert aktiv is True
    assert grund == (
        "Übergangsbereich, keine frühere Automatik-Entscheidung: Tagesmittel 13,7 °C unter 14,0 °C, daher Heizperiode."
    )
    aktiv, grund = entscheide(zone, einst(), None)
    assert aktiv is False
    assert "nicht unter 14,0 °C, daher Sommer" in grund
    assert "älter als 14 Tage" in entscheide(zone, einst(), None, automatik_veraltet=True)[1]


def test_too_little_data():
    aktiv, grund = entscheide([None] * 7, einst(), True)
    assert aktiv is None
    assert "Zu wenige Daten" in grund
    assert entscheide([10, 10, 10, 10, 10, None, 10], einst(tage_start=2, tage_ende=3), None)[0] is None
    assert entscheide([], einst(), None)[0] is None
    # Start-Fenster vollständig, Ende-Fenster nicht: Start zählt
    assert entscheide([None, None, None, None, None, 5, 5], einst(tage_ende=3), None)[0] is True


def test_too_little_data_on_switch_to_automatic():
    # Wechsel in die Automatik: immer eine Entscheidung, ohne Daten im Zweifel Heizperiode
    aktiv, grund = entscheide([None] * 7, einst(), None, wechsel=True)
    assert aktiv is True
    assert "im Zweifel Heizperiode" in grund
    assert entscheide([None] * 7, einst(), False, wechsel=True)[0] is False
    assert entscheide([20, 20, 20, 20, 20, None, 20], einst(), None, wechsel=True)[0] is False


def test_reported_transition_values():
    # Beobachtet: Tagesmittel 11,3 … 13,7 °C bei Heizgrenze 13, Hysterese 2, 2/3 Tagen
    werte = [11.3, 11.2, 10, 11.8, 10.5, 11.5, 13.7]
    assert eindeutig(werte, einst()) is None
    assert entscheide(werte, einst(), None)[0] is True
    assert entscheide(werte, einst(), None, wechsel=True)[0] is True


def test_daily_mean_from_hourly_statistics():
    day = datetime(2026, 1, 5, tzinfo=TZ)
    rows = [{"start": (day + timedelta(hours=h)).timestamp() * 1000, "mean": float(h)} for h in range(24)]
    rows += [{"start": (day + timedelta(days=1, hours=h)).timestamp() * 1000, "mean": 5.0} for h in range(6)]
    out = tagesmittel_aus_stunden(rows, [day.date(), day.date() + timedelta(days=1)], TZ)
    assert out == [11.5, None]  # zweiter Tag: zu wenige Stunden


# ------------------------------------------------------------------ Mit Fake-HA


def setze_tagesmittel(fake, studio, werte):
    heute = datetime.now(TZ).date()
    tage = {heute - timedelta(days=len(werte) - i): v for i, v in enumerate(werte)}
    fake.statistik["sensor.aussentemperatur"] = lambda ts: tage.get(datetime.fromtimestamp(ts, TZ).date())
    studio.heizperiode._mittel = None


def helfer(fake, state="off"):
    fake.extra[HP] = (state, {"friendly_name": "PM Heizperiode"})


def schaltungen(fake):
    return [(d, s, data) for d, s, data in fake.service_calls if d == "input_boolean"]


async def test_automatic_switches_only_on_change(studio, fake):
    helfer(fake, "off")
    setze_tagesmittel(fake, studio, [8] * 7)
    res = await studio.heizperiode.pruefen()
    assert res["aktiv"] is True
    assert schaltungen(fake) == [("input_boolean", "turn_on", {"entity_id": HP})]
    assert fake.extra[HP][0] == "on"
    ev = await studio.store.ereignisse("heizperiode")
    assert ev[0]["daten"]["aktiv"] is True
    assert "Heizgrenze" in ev[0]["text"]
    # Gleiche Entscheidung: nichts schreiben, auch nicht nach manueller Umschaltung in HA
    await studio.heizperiode.pruefen()
    helfer(fake, "off")
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 1
    assert fake.extra[HP][0] == "off"
    abw = (await studio.heizperiode.status())["abweichung"]
    assert (abw["ist"], abw["soll"], abw["ausstehend"]) == ("off", "on", False)
    assert abw["seit"]
    assert abw["winterschutz_ab"]
    # Neue Entscheidung (warm) -> Helfer ist schon aus, kein Schreiben nötig
    setze_tagesmittel(fake, studio, [18] * 7)
    assert (await studio.heizperiode.pruefen())["aktiv"] is False
    assert len(schaltungen(fake)) == 1
    # Wieder kalt -> neue Entscheidung, Helfer wird eingeschaltet
    setze_tagesmittel(fake, studio, [18, 18, 18, 18, 18, 9, 9])
    await studio.heizperiode.pruefen()
    assert schaltungen(fake)[-1][1] == "turn_on"
    status = await studio.heizperiode.status()
    assert status["letzte_aenderung"] is not None


async def test_end_of_heating_season_with_hysteresis(studio, fake):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()
    assert schaltungen(fake) == []  # bereits an
    setze_tagesmittel(fake, studio, [14.5] * 7)  # Übergangsbereich
    assert (await studio.heizperiode.pruefen())["aktiv"] is True
    assert schaltungen(fake) == []
    setze_tagesmittel(fake, studio, [14.5, 14.5, 14.5, 14.5, 15, 16, 17])
    res = await studio.heizperiode.pruefen()
    assert res["aktiv"] is False
    assert schaltungen(fake) == [("input_boolean", "turn_off", {"entity_id": HP})]


async def test_too_little_data_changes_nothing(studio, fake):
    helfer(fake, "on")
    fake.statistik_fehlt.add("sensor.aussentemperatur")
    fake.sim.series.pop("sensor.aussentemperatur")
    res = await studio.heizperiode.pruefen()
    assert res["aktiv"] is None
    assert "Zu wenige Daten" in res["grund"]
    assert schaltungen(fake) == []
    status = await studio.heizperiode.status()
    assert all(t["mittel"] is None for t in status["tagesmittel"])
    assert status["entscheidung"]["aktiv"] is None


async def test_history_fallback(studio, fake):
    fake.statistik_fehlt.add("sensor.aussentemperatur")
    tage, werte = await studio.heizperiode.tagesmittel()
    assert len(tage) == 7
    assert all(v is not None and 5 < v < 11 for v in werte)
    assert tage[-1] == datetime.now(TZ).date() - timedelta(days=1)


async def test_missing_entity_is_not_written(studio, fake):
    setze_tagesmittel(fake, studio, [8] * 7)
    res = await studio.heizperiode.pruefen()
    assert "fehlt" in res["grund"]
    assert schaltungen(fake) == []


async def test_api_get_and_settings(app_client, fake, studio):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [8, 9, 10, 11, 12, 11, 10])
    d = await (await app_client.get("/api/heizperiode")).json()
    assert d["modus"] == "automatik"
    assert (d["heizgrenze"], d["hysterese"], d["tage_start"], d["tage_ende"]) == (13.0, 2.0, 2, 3)
    assert d["vorhanden"] is True
    assert d["zustand"] == "on"
    assert d["aktiv"] is True
    assert d["verknuepft"] is None
    assert [t["mittel"] for t in d["tagesmittel"]] == [8, 9, 10, 11, 12, 11, 10]
    assert d["tagesmittel"][0]["datum"] < d["tagesmittel"][-1]["datum"]
    assert d["entscheidung"]["aktiv"] is True
    assert d["aussen_aktuell"] is not None
    assert set(d) >= {"letzte_aenderung", "naechste_pruefung"}
    resp = await app_client.post("/api/heizperiode", json={"heizgrenze": 14.5, "hysterese": 1, "tage_ende": 4})
    d = await resp.json()
    assert resp.status == 200, d
    assert (d["heizgrenze"], d["hysterese"], d["tage_ende"]) == (14.5, 1.0, 4)
    assert await studio.store.einstellung("heizperiode") == {
        "modus": "automatik",
        "heizgrenze": 14.5,
        "hysterese": 1.0,
        "tage_start": 2,
        "tage_ende": 4,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"modus": "winter"},
        {"heizgrenze": 25},
        {"heizgrenze": "13"},
        {"hysterese": -1},
        {"tage_start": 0},
        {"tage_ende": 1.5},
        {"tage_start": True},
        {"unbekannt": 1},
    ],
)
async def test_api_settings_validation(app_client, fake, studio, body):
    resp = await app_client.post("/api/heizperiode", json=body)
    assert resp.status == 400
    assert (await resp.json())["code"] == "ungueltig"
    assert await studio.store.einstellung("heizperiode") is None


async def test_mode_change_switches_immediately(app_client, fake, studio):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()
    d = await (await app_client.post("/api/heizperiode", json={"modus": "sommer"})).json()
    assert d["modus"] == "sommer"
    assert d["zustand"] == "off"
    assert d["entscheidung"] == {"aktiv": False, "grund": "Manuell auf Sommer gestellt."}
    assert schaltungen(fake) == [("input_boolean", "turn_off", {"entity_id": HP})]
    # Feste Modi werden bei jeder Prüfung durchgesetzt, aber nur bei Abweichung geschrieben
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 1
    helfer(fake, "on")
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 2
    assert fake.extra[HP][0] == "off"
    # Zurück auf Automatik: Entscheidung wird sofort angewendet (Moduswechsel)
    helfer(fake, "off")
    d = await (await app_client.post("/api/heizperiode", json={"modus": "automatik"})).json()
    assert d["zustand"] == "on"
    assert schaltungen(fake)[-1][1] == "turn_on"
    d = await (await app_client.post("/api/heizperiode", json={"modus": "heizperiode"})).json()
    assert d["entscheidung"]["aktiv"] is True


async def test_verknuepft(app_client, fake, studio):
    helfer(fake, "off")
    await studio.store.einstellung_setzen("heizperiode", {**STANDARD, "modus": "sommer"})
    d = await (await app_client.get("/api/heizperiode")).json()
    assert d["verknuepft"] is False
    state, attrs = fake.extra["sensor.pm_heizung_abwesenheitsphase"]
    fake.extra["sensor.pm_heizung_abwesenheitsphase"] = (state, {**attrs, "gesperrt": True, "sperre_grund": "freigabe_aus"})
    d = await (await app_client.get("/api/heizperiode")).json()
    assert d["verknuepft"] is True


async def test_einrichten(app_client, fake, studio):
    setze_tagesmittel(fake, studio, [8] * 7)
    resp = await app_client.post("/api/heizperiode/einrichten", json={})
    d = await resp.json()
    assert resp.status == 200, d
    create = [c for c in fake.ws_commands if c["type"] == "input_boolean/create"]
    assert create[0]["name"] == "PM Heizperiode"
    assert create[0]["icon"] == "mdi:radiator"
    assert d["vorhanden"] is True
    assert d["zustand"] == "on"
    assert schaltungen(fake) == [("input_boolean", "turn_on", {"entity_id": HP})]
    assert "Freigabe" in d["hinweis"]
    assert HP in d["hinweis"]
    resp = await app_client.post("/api/heizperiode/einrichten", json={})
    assert resp.status == 409
    assert (await resp.json())["code"] == "vorhanden"


async def test_einrichten_summer_decision(app_client, fake, studio):
    setze_tagesmittel(fake, studio, [20] * 7)
    d = await (await app_client.post("/api/heizperiode/einrichten", json={})).json()
    assert d["zustand"] == "off"


async def test_read_only_domain(aiohttp_client, ha_client, fake, tmp_path):
    from conftest import LOCAL, WISSEN_TEST
    from klimastudio.config import Options
    from klimastudio.server import KlimaStudio, create_app

    fake.extra["binary_sensor.freigabe"] = ("on", {})
    opts = Options.from_dict({"heizperiode_entitaet": "binary_sensor.freigabe"})
    ks = KlimaStudio(opts, ha_client, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    client = await aiohttp_client(create_app(ks, networks=LOCAL))
    resp = await client.post("/api/heizperiode", json={"modus": "sommer"})
    assert resp.status == 409
    assert (await resp.json())["code"] == "nur_lesen"
    fake.statistik["sensor.aussentemperatur"] = lambda ts: 20.0
    await ks.heizperiode.pruefen()
    assert schaltungen(fake) == []
    resp = await client.post("/api/heizperiode/einrichten", json={})
    assert resp.status == 409
    ks.store.close()


async def test_background_task_runs_and_stops(studio, fake, monkeypatch):
    monkeypatch.setattr(hpm, "INTERVALL", 0.05)
    helfer(fake, "off")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.start()
    for _ in range(100):
        if schaltungen(fake):
            break
        await asyncio.sleep(0.02)
    assert schaltungen(fake)[0][1] == "turn_on"
    assert studio.heizperiode.naechste is not None
    await studio.heizperiode.stop()
    assert studio.heizperiode._task is None


async def test_background_task_survives_errors(studio, fake, monkeypatch):
    monkeypatch.setattr(hpm, "INTERVALL", 0.02)
    calls = 0

    async def kaputt(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("kaputt")

    monkeypatch.setattr(studio.heizperiode, "pruefen", kaputt)
    studio._tz_loaded = True
    studio.heizperiode.start()
    for _ in range(100):
        if calls >= 3:
            break
        await asyncio.sleep(0.02)
    assert calls >= 3
    await studio.heizperiode.stop()


async def _seit_verschieben(studio, stunden):
    z = await studio.store.einstellung("heizperiode_zustand")
    z["abweichung_seit"] = (datetime.now(TZ) - timedelta(hours=stunden)).isoformat(timespec="seconds")
    await studio.store.einstellung_setzen("heizperiode_zustand", z)


async def test_winter_protection_after_six_hours(studio, fake):
    helfer(fake, "off")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()
    helfer(fake, "off")  # manuell in HA ausgeschaltet
    await studio.heizperiode.pruefen()
    await _seit_verschieben(studio, 5)
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 1
    await _seit_verschieben(studio, 6.1)
    res = await studio.heizperiode.pruefen()
    assert "Winterschutz" in res["grund"]
    assert fake.extra[HP][0] == "on"
    assert len(schaltungen(fake)) == 2
    ev = await studio.store.ereignisse("heizperiode")
    assert "Winterschutz" in ev[0]["text"]
    assert (await studio.heizperiode.status())["abweichung"] is None


async def test_manual_summer_switch_is_respected_without_winter_protection(studio, fake):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [18] * 7)
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "off"
    helfer(fake, "on")  # manuell eingeschaltet, Automatik sagt Sommer
    await _seit_verschieben(studio, 0)
    await studio.heizperiode.pruefen()
    await _seit_verschieben(studio, 30)
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "on"
    abw = (await studio.heizperiode.status())["abweichung"]
    assert abw["soll"] == "off"
    assert abw["winterschutz_ab"] is None


async def test_write_into_void_is_not_done(studio, fake):
    helfer(fake, "unavailable")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 1
    assert (await studio.store.einstellung("heizperiode_zustand"))["angewendet"] is False
    assert (await studio.store.ereignisse("heizperiode")) == []
    status = await studio.heizperiode.status()
    assert status["abweichung"]["ist"] == "unavailable"
    assert status["abweichung"]["ausstehend"] is True
    # Nächste Prüfung versucht es erneut; Entität wieder da (aus) -> wird eingeschaltet
    await studio.heizperiode.pruefen()
    assert len(schaltungen(fake)) == 2
    helfer(fake, "off")
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "on"
    assert (await studio.store.einstellung("heizperiode_zustand"))["angewendet"] is True


async def test_decision_bound_to_entity(studio, fake):
    helfer(fake, "on")
    # Gespeicherte Entscheidung einer anderen Entität wird verworfen
    await studio.store.einstellung_setzen(
        "heizperiode_zustand",
        {
            "entitaet": "input_boolean.alt",
            "entscheidung": False,
            "angewendet": True,
            "automatik_entscheidung": False,
            "automatik_zeit": datetime.now(TZ).isoformat(timespec="seconds"),
        },
    )
    setze_tagesmittel(fake, studio, [14, 14, 14, 14, 14, 14, 13.5])  # Übergangsbereich
    res = await studio.heizperiode.pruefen()
    assert res["aktiv"] is True  # Mitte-Regel, nicht die fremde Entscheidung
    assert schaltungen(fake) == []
    z = await studio.store.einstellung("heizperiode_zustand")
    assert (z["entitaet"], z["entscheidung"], z["angewendet"]) == (HP, True, True)


async def test_failed_forced_apply_is_retried(app_client, fake, studio, monkeypatch):
    from klimastudio.ha_client import HAError

    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()
    original = studio.client.call_service

    async def kaputt(*a, **kw):
        raise HAError("POST services/input_boolean/turn_off: HTTP 500", "500")

    monkeypatch.setattr(studio.client, "call_service", kaputt)
    resp = await app_client.post("/api/heizperiode", json={"modus": "sommer"})
    assert resp.status == 502
    assert "nächsten Prüfung" in (await resp.json())["fehler"]
    assert fake.extra[HP][0] == "on"
    assert (await studio.store.einstellung("heizperiode"))["modus"] == "sommer"
    monkeypatch.setattr(studio.client, "call_service", original)
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "off"
    # Zurück auf Automatik scheitert ebenfalls -> ausstehender Zwangslauf wird nachgeholt
    monkeypatch.setattr(studio.client, "call_service", kaputt)
    resp = await app_client.post("/api/heizperiode", json={"modus": "automatik"})
    assert resp.status == 502
    assert (await studio.store.einstellung("heizperiode_zustand"))["angewendet"] is False
    monkeypatch.setattr(studio.client, "call_service", original)
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "on"


async def test_einrichten_transition_zone_sets_on(app_client, fake, studio):
    # Letztes Tagesmittel über der Mitte, aber kein eindeutiges Ende -> trotzdem Heizperiode
    setze_tagesmittel(fake, studio, [14.5] * 7)
    d = await (await app_client.post("/api/heizperiode/einrichten", json={})).json()
    assert d["zustand"] == "on"
    assert "Auto sofort ab" in d["hinweis"]
    z = await studio.store.einstellung("heizperiode_zustand")
    assert z == {
        "entitaet": HP,
        "entscheidung": True,
        "angewendet": True,
        "letzte_aenderung": z["letzte_aenderung"],
        "automatik_entscheidung": True,
        "automatik_zeit": z["automatik_zeit"],
    }
    # Folgeprüfung im Übergangsbereich: die Startentscheidung der Automatik bleibt (kein Umschalten)
    assert (await studio.heizperiode.pruefen())["aktiv"] is True
    assert fake.extra[HP][0] == "on"


async def test_einrichten_without_data_sets_on(app_client, fake, studio):
    fake.statistik_fehlt.add("sensor.aussentemperatur")
    fake.sim.series.pop("sensor.aussentemperatur")
    d = await (await app_client.post("/api/heizperiode/einrichten", json={})).json()
    assert d["zustand"] == "on"


# ------------------------------------------------------------------ Automatik nach festen Modi (1.2.1)

BEOBACHTET = [11.3, 11.2, 10, 11.8, 10.5, 11.5, 13.7]  # Übergangsbereich, jüngstes Mittel unter 14,0 °C


async def modus(app_client, wert):
    resp = await app_client.post("/api/heizperiode", json={"modus": wert})
    d = await resp.json()
    assert resp.status == 200, d
    return d


async def test_fixed_summer_does_not_hold_automatic_in_transition(app_client, fake, studio):
    # Beobachtet: Sommer -> Heizperiode -> Sommer -> Automatik, Freigabe blieb aus
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, BEOBACHTET)
    await modus(app_client, "sommer")
    await modus(app_client, "heizperiode")
    d = await modus(app_client, "sommer")
    assert d["zustand"] == "off"
    d = await modus(app_client, "automatik")
    assert d["zustand"] == "on"
    assert schaltungen(fake)[-1][1] == "turn_on"
    assert d["entscheidung"] == {
        "aktiv": True,
        "grund": "Übergangsbereich: letzte Entscheidung der Automatik (Heizperiode) bleibt.",
    }
    ev = await studio.store.ereignisse("heizperiode")
    assert ev[0]["daten"]["grund"] == (
        "Übergangsbereich, keine frühere Automatik-Entscheidung: Tagesmittel 13,7 °C unter 14,0 °C, daher Heizperiode."
    )
    # Folgeprüfungen bleiben dabei
    await studio.heizperiode.pruefen()
    assert fake.extra[HP][0] == "on"


async def test_fixed_heating_to_automatic_with_clear_end_switches_off(app_client, fake, studio):
    helfer(fake, "off")
    setze_tagesmittel(fake, studio, [8] * 7)
    await studio.heizperiode.pruefen()  # Automatik: Heizperiode
    d = await modus(app_client, "heizperiode")
    assert d["zustand"] == "on"
    setze_tagesmittel(fake, studio, [16] * 7)  # eindeutiges Ende
    d = await modus(app_client, "automatik")
    assert d["zustand"] == "off"
    assert d["entscheidung"]["aktiv"] is False
    assert "Hysterese" in d["entscheidung"]["grund"]


async def test_automatic_decision_survives_fixed_modes(app_client, fake, studio):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [18] * 7)
    await studio.heizperiode.pruefen()  # Automatik: Sommer
    assert fake.extra[HP][0] == "off"
    await modus(app_client, "heizperiode")
    await modus(app_client, "sommer")
    d = await modus(app_client, "heizperiode")
    assert d["zustand"] == "on"
    z = await studio.store.einstellung("heizperiode_zustand")
    assert (z["entscheidung"], z["automatik_entscheidung"]) == (True, False)
    # Übergangsbereich (Mitte-Regel ergäbe Heizperiode): die Automatik-Entscheidung Sommer zählt
    setze_tagesmittel(fake, studio, BEOBACHTET)
    d = await modus(app_client, "automatik")
    assert d["zustand"] == "off"
    assert d["entscheidung"]["grund"] == "Übergangsbereich: letzte Entscheidung der Automatik (Sommer) bleibt."


async def test_automatic_decision_older_than_14_days_uses_middle_rule(app_client, fake, studio):
    helfer(fake, "on")
    setze_tagesmittel(fake, studio, [18] * 7)
    await studio.heizperiode.pruefen()  # Automatik: Sommer
    await modus(app_client, "sommer")
    z = await studio.store.einstellung("heizperiode_zustand")
    z["automatik_zeit"] = (datetime.now(TZ) - timedelta(days=15)).isoformat(timespec="seconds")
    await studio.store.einstellung_setzen("heizperiode_zustand", z)
    setze_tagesmittel(fake, studio, BEOBACHTET)
    await modus(app_client, "automatik")
    assert fake.extra[HP][0] == "on"
    ev = await studio.store.ereignisse("heizperiode")
    assert ev[0]["daten"]["grund"].startswith("Übergangsbereich, letzte Entscheidung der Automatik älter als 14 Tage:")
    z = await studio.store.einstellung("heizperiode_zustand")
    assert z["automatik_entscheidung"] is True


async def test_switch_to_automatic_without_data_means_heating(app_client, fake, studio):
    helfer(fake, "on")
    fake.statistik_fehlt.add("sensor.aussentemperatur")
    fake.sim.series.pop("sensor.aussentemperatur")
    d = await modus(app_client, "sommer")
    assert d["zustand"] == "off"
    await modus(app_client, "automatik")
    assert fake.extra[HP][0] == "on"
    ev = await studio.store.ereignisse("heizperiode")
    assert "im Zweifel Heizperiode" in ev[0]["daten"]["grund"]


async def test_running_day_is_not_counted(studio, fake):
    heute = datetime.now(TZ).date()
    fake.statistik["sensor.aussentemperatur"] = lambda ts: 30.0 if datetime.fromtimestamp(ts, TZ).date() >= heute else 8.0
    studio.heizperiode._mittel = None
    tage, werte = await studio.heizperiode.tagesmittel()
    assert tage[-1] == heute - timedelta(days=1)
    assert werte == [8.0] * 7
