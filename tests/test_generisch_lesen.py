"""Betriebsart generisch, Lesen (Phase 2): Regel-Kennzeichnung, Lage-Pfade, Lagebericht, Daten und Bericht.

Vertrag mit der Steuerung (Phase 3): ``adapter.raum_zustand`` liefert im Modus generisch
``modus`` ∈ plan/hand/aus sowie ``overlay_bis`` (ISO, "dauerhaft" oder None) und ``boost_bis``
(ISO oder None) aus den Rück-Timern der App. Die Tests setzen diese Felder gezielt über einen
umhüllten ``raum_zustand``.
"""

from __future__ import annotations

import copy
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import LOCAL
from klimastudio.coach import ki
from klimastudio.coach.engine import (
    BETRIEBSARTEN,
    WISSEN_DATEI,
    RegelFehler,
    Regelwerk,
    auswerten,
    pruefe_regel,
    regel_gilt,
)
from klimastudio.coach.lage import APP_PFADE, NUR_PM_RAUM, lagebericht
from klimastudio.config import Room
from klimastudio.data import soll_aus_attributen, soll_bereich
from klimastudio.report import build_report
from klimastudio.server import KlimaStudio, create_app

# Regeln, die nur mit PM Klima gelten (Integration, Freigabe, PM-Modi auto/heat/off, Schimmel-Schätzung,
# Luftqualitäts- und Lüftungssensoren der Integration)
NUR_PM = {
    "integration_pausiert",
    "integration_gesperrt_kalt",
    "heizperiode_nicht_eingerichtet",
    "frost_ohne_heizperiode",
    "sommer_raum_kalt",
    "sommer_handbetrieb_heizt",
    "sommer_ueberbrueckung_heizt",
    "ueberbrueckung_dauerhaft",
    "ueberbrueckung_hoch",
    "handbetrieb_dauerhaft",
    "abwesend_handbetrieb",
    "frost_raum_aus",
    "raum_aus_feucht",
    "raum_aus_kalt",
    "plan_leer",
    "plan_tage_ohne_block",
    "schimmel_akut",
    "schimmel_erhoeht",
    "schimmel_woche_80",
    "schimmel_woche_70",
    "luftqualitaet_schlecht",
    "lueften_empfohlen",
}
# Generische Gegenstücke: <id>_generisch
GEGENSTUECKE = {
    "frost_ohne_heizperiode",
    "sommer_handbetrieb_heizt",
    "sommer_ueberbrueckung_heizt",
    "ueberbrueckung_dauerhaft",
    "ueberbrueckung_hoch",
    "handbetrieb_dauerhaft",
    "abwesend_handbetrieb",
    "frost_raum_aus",
    "raum_aus_feucht",
    "raum_aus_kalt",
    "plan_leer",
    "plan_tage_ohne_block",
}
REGELN_1_1 = 70  # Anzahl der Regeln bis 1.1.x (alle gelten weiterhin im Modus pm_networks)


@pytest.fixture(scope="module")
def werk() -> Regelwerk:
    w = Regelwerk.laden(WISSEN_DATEI)
    assert w.fehler == []
    return w


# ------------------------------------------------------------------ Kennzeichnung


def test_kennzeichnung_der_regeln(werk):
    nach_art = {art: {r["id"] for r in werk.regeln if r["betriebsart"] == art} for art in (*BETRIEBSARTEN, None)}
    assert nach_art["pm_networks"] == NUR_PM
    # frost_raum_aus_generisch gilt in der Heizperiode (Maßnahme plan), das Sommer-Gegenstück empfiehlt hand
    assert nach_art["generisch"] == {f"{i}_generisch" for i in GEGENSTUECKE} | {"frost_raum_aus_sommer_generisch"}
    assert len(nach_art["pm_networks"]) + len(nach_art[None]) == REGELN_1_1
    for rid in GEGENSTUECKE:
        pm = next(r for r in werk.regeln if r["id"] == rid)
        gen = next(r for r in werk.regeln if r["id"] == f"{rid}_generisch")
        assert (gen["bereich"], gen["saison"]) == (pm["bereich"], pm["saison"]), rid


def test_generische_regeln_ohne_pm_begriffe(werk):
    for r in werk.regeln:
        if r["betriebsart"] != "generisch":
            continue
        text = " ".join((r["titel"], r["text"], r["begruendung"]))
        for begriff in ("PM Klima", "Integration", "Freigabe", "Sperre", "Zeitplanbetrieb", "zeitplan_temperatur", "schimmel"):
            assert begriff not in text, (r["id"], begriff)
        m = r["massnahme"] or {}
        if m.get("aktion") == "modus":
            assert m["modus"] in ("plan", "hand", "aus"), r["id"]


def test_pm_regeln_mit_pm_modi(werk):
    for r in werk.regeln:
        m = r["massnahme"] or {}
        if r["betriebsart"] != "generisch" and m.get("aktion") == "modus":
            assert m["modus"] in ("auto", "heat", "off"), r["id"]


def test_plananwendung_abgeleitet(werk):
    regeln = {r["id"]: r for r in werk.regeln}
    # raum.plan.* in der Bedingung -> nur mit Plananwendung (generisch)
    for rid in ("plan_leer_generisch", "plan_tage_ohne_block_generisch", "wohnraum_plan_zu_warm", "plan_heizt_nachts"):
        assert regeln[rid]["plananwendung"] is True, rid
    # ausdrücklich gesetzt: Plan/Hand hat ohne Plananwendung keine Bedeutung
    for rid in ("handbetrieb_dauerhaft_generisch", "abwesend_handbetrieb_generisch", "sommer_handbetrieb_heizt_generisch"):
        assert regeln[rid]["plananwendung"] is True, rid
    for rid in ("frost_raum_aus_generisch", "raum_aus_kalt_generisch", "ueberbrueckung_dauerhaft_generisch"):
        assert regeln[rid]["plananwendung"] is False, rid


def _regel(**kw):
    r = {
        "id": "x",
        "bereich": "raum",
        "thema": "heizen",
        "prioritaet": 2,
        "wenn": {"wert": "raum.ist", "op": "<", "ref": 15},
        "titel": "T",
        "text": "Text",
    }
    r.update(kw)
    return r


@pytest.mark.parametrize(
    ("regel", "meldung"),
    [
        (_regel(betriebsart="pm_klima"), "betriebsart"),
        (_regel(betriebsart=None, plananwendung="ja"), "plananwendung"),
        (_regel(massnahme={"typ": "steuerung", "aktion": "modus", "modus": "plan"}), "modus"),
        (_regel(betriebsart="pm_networks", massnahme={"typ": "steuerung", "aktion": "modus", "modus": "hand"}), "modus"),
        (_regel(betriebsart="generisch", massnahme={"typ": "steuerung", "aktion": "modus", "modus": "auto"}), "modus"),
    ],
)
def test_validierung_betriebsart(regel, meldung):
    with pytest.raises(RegelFehler, match=meldung):
        pruefe_regel(regel)


def test_validierung_gueltig():
    r = pruefe_regel(_regel(betriebsart="generisch", massnahme={"typ": "steuerung", "aktion": "modus", "modus": "aus"}))
    assert (r["betriebsart"], r["plananwendung"], r["massnahme"]["modus"]) == ("generisch", False, "aus")
    r = pruefe_regel(_regel())
    assert (r["betriebsart"], r["plananwendung"]) == (None, False)
    r = pruefe_regel(_regel(wenn={"nicht": {"wert": "raum.x", "op": "<", "pfad": "raum.plan.max_temp"}}))
    assert r["plananwendung"] is True


def test_regel_gilt():
    pm, gen = pruefe_regel(_regel(betriebsart="pm_networks")), pruefe_regel(_regel(betriebsart="generisch"))
    beide, plan = pruefe_regel(_regel()), pruefe_regel(_regel(plananwendung=True))
    assert [regel_gilt(r, "pm_networks", False) for r in (pm, gen, beide, plan)] == [True, False, True, True]
    assert [regel_gilt(r, "generisch", False) for r in (pm, gen, beide, plan)] == [False, True, True, False]
    assert [regel_gilt(r, "generisch", True) for r in (pm, gen, beide, plan)] == [False, True, True, True]


# ------------------------------------------------------------------ Filter in auswerten


def _immer_wahr(werk: Regelwerk) -> Regelwerk:
    """Kopie des Regelwerks, in der jede Bedingung und Saison zutrifft (prüft nur den Filter)."""
    kopie = Regelwerk(version=werk.version)
    for r in werk.regeln:
        r = dict(r)
        r["wenn"] = {"wert": "raum.slug" if r["bereich"] == "raum" else "zeit.monat", "op": "vorhanden"}
        r["saison"], r["monate"] = "immer", None
        kopie.regeln.append(r)
    return kopie


def _ctxs(app: dict | None, raum: dict | None = None, **global_werte):
    g = {"raum": None, "zeit": {"monat": 1, "stunde": 12, "wochentag": 2}, "heizperiode": {"aktiv": True}, **global_werte}
    if app is not None:
        g["app"] = app
    r = {**g, "raum": {"slug": "wohnzimmer", "name": "Wohnzimmer", "nassraum": False, "schlafraum": False, **(raum or {})}}
    return g, [r]


@pytest.mark.parametrize(
    ("app", "erwartet"),
    [
        (None, "pm"),
        ({"betriebsart": "pm_networks", "plan_anwendung": False}, "pm"),
        ({"betriebsart": "pm_networks", "plan_anwendung": True}, "pm"),
        ({"betriebsart": "generisch", "plan_anwendung": True}, "gen_plan"),
        ({"betriebsart": "generisch", "plan_anwendung": False}, "gen"),
        ({"betriebsart": "unbekannt"}, "pm"),
    ],
)
def test_filter_je_betriebsart(werk, app, erwartet):
    g, raeume = _ctxs(app)
    ids = {t["regel"] for t in auswerten(_immer_wahr(werk), g, raeume)}
    alle = {r["id"]: r for r in werk.regeln}
    if erwartet == "pm":
        soll = {i for i, r in alle.items() if r["betriebsart"] != "generisch"}
        assert len(soll) == REGELN_1_1
        assert not any(i.endswith("_generisch") for i in ids)
    else:
        soll = {
            i for i, r in alle.items() if r["betriebsart"] != "pm_networks" and (erwartet == "gen_plan" or not r["plananwendung"])
        }
        assert not ids & NUR_PM
    assert ids == soll


GEN = {"betriebsart": "generisch", "plan_anwendung": True}
PLAN_OK = {"max_temp": 21, "min_temp": 17, "stunden_woche": 60, "nacht": False, "leer": False, "tage_ohne_block": 0}


def _ids(werk, app, raum, **g):
    g_ctx, raeume = _ctxs(app, raum, **g)
    return {t["id"] for t in auswerten(werk, g_ctx, raeume)}


@pytest.mark.parametrize(
    ("raum", "g", "regel"),
    [
        (
            {"modus": "hand", "soll": 21, "plan": PLAN_OK},
            {"anwesenheit": {"jemand_zuhause": True}},
            "handbetrieb_dauerhaft_generisch",
        ),
        (
            {"modus": "hand", "soll": 21, "plan": PLAN_OK},
            {"anwesenheit": {"jemand_zuhause": False}},
            "abwesend_handbetrieb_generisch",
        ),
        ({"modus": "aus", "ist": 13}, {"wetter": {"min_3tage": -3}}, "frost_raum_aus_generisch"),
        ({"modus": "aus", "ist": 14, "feuchte": 68}, {}, "raum_aus_feucht_generisch"),
        ({"modus": "aus", "ist": 14, "feuchte": 50}, {}, "raum_aus_kalt_generisch"),
        ({"modus": "plan", "plan": {**PLAN_OK, "leer": True}}, {}, "plan_leer_generisch"),
        ({"modus": "plan", "plan": {**PLAN_OK, "tage_ohne_block": 2}}, {}, "plan_tage_ohne_block_generisch"),
        ({"overlay_aktiv": True, "overlay_dauerhaft": True, "soll": 22}, {}, "ueberbrueckung_dauerhaft_generisch"),
        ({"overlay_aktiv": True, "overlay_dauerhaft": False, "soll": 24}, {}, "ueberbrueckung_hoch_generisch"),
    ],
)
def test_generische_gegenstuecke_feuern(werk, raum, g, regel):
    ids = _ids(werk, GEN, raum, **g)
    assert f"{regel}:wohnzimmer" in ids
    assert not {i.split(":")[0] for i in ids} & NUR_PM
    # dieselbe Lage im Modus pm_networks: kein generisches Gegenstück
    assert not [i for i in _ids(werk, {"betriebsart": "pm_networks"}, raum, **g) if "_generisch" in i]


def test_handbetrieb_generisch_nur_mit_heizplan(werk):
    """Nach dem Erststart stehen alle Räume auf hand: Aufforderung zur Planwahl nur bei vorhandenem Heizplan."""
    zuhause = {"anwesenheit": {"jemand_zuhause": True}}
    hand = {"modus": "hand", "soll": 21}
    rid = "handbetrieb_dauerhaft_generisch:wohnzimmer"
    assert rid in _ids(werk, GEN, {**hand, "plan": PLAN_OK}, **zuhause)
    # kein Heizplan (alle Planwerte None) bzw. Heizplan ohne Blöcke: kein Hinweis
    assert rid not in _ids(werk, GEN, hand, **zuhause)
    assert rid not in _ids(werk, GEN, {**hand, "plan": dict.fromkeys(PLAN_OK)}, **zuhause)
    assert rid not in _ids(werk, GEN, {**hand, "plan": {**PLAN_OK, "leer": True}}, **zuhause)
    # ohne Plananwendung: kein Hinweis
    ohne = {"betriebsart": "generisch", "plan_anwendung": False}
    assert rid not in _ids(werk, ohne, {**hand, "plan": PLAN_OK}, **zuhause)
    regel = next(r for r in werk.regeln if r["id"] == "handbetrieb_dauerhaft_generisch")
    assert regel["titel"] == "Heizplan noch nicht aktiv"
    assert "dauerhaft" not in regel["text"]
    assert "rund um die Uhr" not in regel["begruendung"]
    assert "Wählen Sie Plan" in regel["text"]


def test_hand_hinweise_generisch_nur_mit_heizplan(werk):
    """Nach dem Erststart stehen alle Räume auf hand: Abwesenheits- und Sommerhinweis nur mit Heizplan."""
    abwesend = {"anwesenheit": {"jemand_zuhause": False}}
    hand = {"modus": "hand", "soll": 21}
    rid = "abwesend_handbetrieb_generisch:wohnzimmer"
    assert rid in _ids(werk, GEN, {**hand, "plan": PLAN_OK}, **abwesend)
    assert rid not in _ids(werk, GEN, hand, **abwesend)
    assert rid not in _ids(werk, GEN, {**hand, "plan": dict.fromkeys(PLAN_OK)}, **abwesend)
    assert rid not in _ids(werk, GEN, {**hand, "plan": {**PLAN_OK, "leer": True}}, **abwesend)

    sommer = {"heizperiode": {"aktiv": False}, "zeit": {"monat": 7, "stunde": 12, "wochentag": 2}}
    heizt = {"modus": "hand", "soll": 20, "ist": 19, "soll_ist_abstand": 1, "overlay_aktiv": False}
    rid = "sommer_handbetrieb_heizt_generisch:wohnzimmer"
    assert rid in _ids(werk, GEN, {**heizt, "plan": PLAN_OK}, **sommer)
    assert rid not in _ids(werk, GEN, heizt, **sommer)
    assert rid not in _ids(werk, GEN, {**heizt, "plan": {**PLAN_OK, "leer": True}}, **sommer)
    # Raum wärmer als der Sollwert und Thermostat heizt nicht: kein Hinweis
    warm = {**heizt, "ist": 24, "soll_ist_abstand": -4, "hvac_action": "idle", "plan": PLAN_OK}
    assert rid not in _ids(werk, GEN, warm, **sommer)
    # Thermostat meldet heating: Hinweis
    assert rid in _ids(werk, GEN, {**warm, "hvac_action": "heating"}, **sommer)


def test_generische_regeln_im_sommer(werk):
    sommer = {"heizperiode": {"aktiv": False}, "zeit": {"monat": 7, "stunde": 12, "wochentag": 2}}
    raum = {"modus": "hand", "soll": 20, "ist": 19, "soll_ist_abstand": 1, "overlay_aktiv": False, "plan": PLAN_OK}
    ids = _ids(werk, GEN, raum, **sommer)
    assert "sommer_handbetrieb_heizt_generisch:wohnzimmer" in ids
    ids = _ids(werk, GEN, {"overlay_aktiv": True, "soll": 21}, aussen={"temperatur": 18}, **sommer)
    assert "sommer_ueberbrueckung_heizt_generisch:wohnzimmer" in ids
    ids = _ids(werk, GEN, {}, wetter={"min_morgen": -1}, **sommer)
    assert "frost_ohne_heizperiode_generisch:global" in ids
    assert "frost_ohne_heizperiode:global" not in ids


def test_hand_und_plan_nur_mit_plananwendung(werk):
    ohne = {"betriebsart": "generisch", "plan_anwendung": False}
    assert "handbetrieb_dauerhaft_generisch:wohnzimmer" not in _ids(werk, ohne, {"modus": "hand", "soll": 21})
    assert "plan_leer_generisch:wohnzimmer" not in _ids(werk, ohne, {"modus": "plan", "plan": {**PLAN_OK, "leer": True}})
    # Planbewertung (beide Betriebsarten) ebenfalls nur mit Plananwendung
    zu_warm = {"plan": {**PLAN_OK, "max_temp": 24}}
    assert "wohnraum_plan_zu_warm:wohnzimmer" not in _ids(werk, ohne, zu_warm)
    assert "wohnraum_plan_zu_warm:wohnzimmer" in _ids(werk, GEN, zu_warm)
    assert "wohnraum_plan_zu_warm:wohnzimmer" in _ids(werk, {"betriebsart": "pm_networks"}, zu_warm)
    # „aus“ gilt unabhängig von der Plananwendung
    assert "raum_aus_kalt_generisch:wohnzimmer" in _ids(werk, ohne, {"modus": "aus", "ist": 14})


def test_pm_modi_feuern_generisch_nicht(werk):
    """PM-Werte (auto/heat/off) lösen im Modus generisch keine Regel aus, auch wenn sie im Kontext stünden."""
    for modus in ("auto", "heat", "off"):
        ids = {i.split(":")[0] for i in _ids(werk, GEN, {"modus": modus, "soll": 21, "ist": 13, "feuchte": 70})}
        assert not ids & NUR_PM, modus
        assert not [i for i in ids if i.endswith("_generisch") and "modus" in i]


# ------------------------------------------------------------------ Lage generisch


def _zustand_setzen(ks, monkeypatch, werte: dict[str, dict]):
    """Vertrag Phase 3: modus/overlay_bis/boost_bis kommen aus adapter.raum_zustand."""
    original = ks.adapter.raum_zustand

    def raum_zustand(room, by_id):
        z = dict(original(room, by_id))
        z.update(werte.get(room.raum, {}))
        return z

    monkeypatch.setattr(ks.adapter, "raum_zustand", raum_zustand)


def _plan_anwendung(ks, monkeypatch, wert: bool):
    original = ks.adapter.faehigkeiten
    monkeypatch.setattr(ks.adapter, "faehigkeiten", lambda: {**original(), "plan_anwendung": wert})


async def test_lage_generisch_pfade(studio_generisch, monkeypatch):
    ks = studio_generisch
    bis = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _zustand_setzen(
        ks,
        monkeypatch,
        {
            "wohnzimmer": {"modus": "hand", "overlay_bis": None, "boost_bis": bis},
            "kuche": {"modus": "plan", "overlay_bis": "dauerhaft", "boost_bis": None},
            "gaestezimmer_heizung": {"modus": "aus"},
            "badezimmer": {"modus": "heat_cool"},  # kein App-Modus -> None
        },
    )
    _plan_anwendung(ks, monkeypatch, True)
    # PM-Sensoren, selbst wenn per Override gesetzt, gelten generisch nicht
    rooms = await ks.rooms()
    next(r for r in rooms if r.raum == "kuche").luftqualitaet = "sensor.raumklima_ost_co2"
    lage = await ks.coach.lage.erstellen()
    g = lage["global"]
    assert g["app"] == {"betriebsart": "generisch", "plan_anwendung": True}
    assert g["integration"] == {"aktiv": None, "gesperrt": None}
    assert lage["extra"]["empfehlungen"] == []
    raeume = {c["raum"]["slug"]: c["raum"] for c in lage["raeume"]}
    for c in lage["raeume"]:
        for pfad in APP_PFADE:
            teil, name = pfad.split(".")
            assert name in c[teil], pfad
    wz, ku = raeume["wohnzimmer"], raeume["kuche"]
    assert (wz["modus"], wz["boost_aktiv"], wz["overlay_aktiv"], wz["overlay_dauerhaft"]) == ("hand", True, False, False)
    assert (ku["modus"], ku["boost_aktiv"], ku["overlay_aktiv"], ku["overlay_dauerhaft"]) == ("plan", False, True, True)
    assert raeume["gaestezimmer_heizung"]["modus"] == "aus"
    assert raeume["badezimmer"]["modus"] is None
    for r in raeume.values():
        assert all(r[k] is None for k in NUR_PM_RAUM)
        assert r["schimmel"] is None  # kein Sensor
    assert wz["co2"] is not None
    assert wz["plan"]["leer"] is False  # Heizplan schedule.heizplan_wohnzimmer


async def test_lage_generisch_hvac_modus_ohne_vertrag(studio_generisch, monkeypatch):
    """Liefert der Adapter (noch) den hvac-Zustand, bleibt raum.modus leer statt PM-Werte zu übernehmen."""
    _zustand_setzen(studio_generisch, monkeypatch, {"wohnzimmer": {"modus": "heat"}, "kuche": {"modus": "off"}})
    lage = await studio_generisch.coach.lage.erstellen()
    raeume = {c["raum"]["slug"]: c["raum"] for c in lage["raeume"]}
    assert raeume["wohnzimmer"]["modus"] is None
    assert raeume["kuche"]["modus"] is None


async def test_lage_pm_app_pfade(studio):
    lage = await studio.coach.lage.erstellen()
    assert lage["global"]["app"] == {"betriebsart": "pm_networks", "plan_anwendung": False}
    assert {c["raum"]["modus"] for c in lage["raeume"]} <= {"auto", "heat", "off", None}
    bericht = lagebericht(lage, [], True)
    assert "betriebsart" not in bericht
    assert "integration" in bericht
    assert "empfehlungen_integration" in bericht


async def test_coach_generisch_mit_produktivem_wissen(ha_client_generisch, options_generisch, tmp_path, monkeypatch):
    ks = KlimaStudio(options_generisch, ha_client_generisch, data_dir=tmp_path)
    try:
        _zustand_setzen(ks, monkeypatch, {"wohnzimmer": {"modus": "hand"}, "kuche": {"modus": "aus"}})
        _plan_anwendung(ks, monkeypatch, True)
        lage = await ks.coach.lage.erstellen()
        for ctx in (lage["global"], *lage["raeume"]):
            ctx["heizperiode"] = {"aktiv": True, "modus": "automatik"}
            ctx["anwesenheit"] = {"jemand_zuhause": True, "anzahl_zuhause": 1}
        ids = {t["id"] for t in auswerten(ks.coach.regelwerk, lage["global"], lage["raeume"])}
        assert "handbetrieb_dauerhaft_generisch:wohnzimmer" in ids
        assert not {i.split(":")[0] for i in ids} & NUR_PM
        uebersicht = await ks.coach.uebersicht()
        assert uebersicht["integration"] == []
        assert not {t["regel"] for t in uebersicht["tipps"]} & NUR_PM
    finally:
        await ks.heizperiode.stop()
        ks.store.close()


# ------------------------------------------------------------------ Lagebericht und KI


async def test_lagebericht_generisch(app_client_generisch, studio_generisch, fake_generisch, monkeypatch):
    _zustand_setzen(studio_generisch, monkeypatch, {"wohnzimmer": {"modus": "plan"}})
    resp = await app_client_generisch.get("/api/coach/lagebericht")
    assert resp.status == 200
    lage = (await resp.json())["lage"]
    assert "integration" not in lage
    assert "empfehlungen_integration" not in lage
    assert lage["betriebsart"] == "generisch"
    assert lage["plan_anwendung"] is bool(studio_generisch.adapter.faehigkeiten()["plan_anwendung"])
    wz = next(r for r in lage["raeume"] if r["raum"] == "wohnzimmer")
    assert wz["aktuell"]["modus"] == "plan"
    for r in lage["raeume"]:
        assert not set(NUR_PM_RAUM) & set(r["aktuell"])
    resp = await app_client_generisch.post("/api/coach/ki", json={})
    assert resp.status == 200, await resp.text()
    anweisung = next(c[2]["instructions"] for c in fake_generisch.service_calls if c[0] == "ai_task")
    assert anweisung.startswith(ki.PROMPT_GENERISCH)
    assert '"integration"' not in anweisung
    assert '"betriebsart":"generisch"' in anweisung


def test_prompt_generisch():
    p = ki.PROMPT_GENERISCH
    assert "Heizungsintegration PM Klima" not in p
    for wort in ("plan =", "hand =", "aus =", "plan_anwendung"):
        assert wort in p
    assert "!" not in ki.EINLEITUNG_GENERISCH
    assert ki.PROMPT.startswith(ki.EINLEITUNG_PM)


# ------------------------------------------------------------------ Daten, Übersicht, Bericht


def test_soll_aus_attributen():
    assert soll_aus_attributen({"temperature": 21}) == 21
    assert soll_aus_attributen({"temperature": None, "target_temp_low": 20, "target_temp_high": 24}) == 22
    assert soll_aus_attributen({"target_temp_low": 19}) == 19
    assert soll_aus_attributen({}) is None
    assert soll_bereich({"temperature": 21, "target_temp_low": 20}) is None
    assert soll_bereich({"target_temp_low": 20, "target_temp_high": 24}) == {"min": 20, "max": 24}
    assert soll_bereich({}) is None


def _bad_klima_verlauf(fake, mit_action: bool = False):
    """Historie für das Bereichs-Thermostat (heat_cool, ohne hvac_action)."""
    rows = []
    for i, t in enumerate(fake.sim.times[::12]):
        attrs = {"target_temp_low": 20.0, "target_temp_high": 24.0 if i % 2 else 23.0, "current_temperature": 22.0}
        if mit_action:
            attrs["hvac_action"] = "heating"
        rows.append((float(t), "heat_cool", attrs))
    fake.sim.series["climate.bad_klima"] = rows


async def test_auswertung_heat_cool_ohne_hvac_action(app_client_generisch, fake_generisch):
    _bad_klima_verlauf(fake_generisch)
    resp = await app_client_generisch.get("/api/auswertung/badezimmer?bereich=24h")
    d = await resp.json()
    assert resp.status == 200, d
    assert d["heizstunden"] is None
    werte = {v for _, v in d["soll_ist"]["soll"] if v is not None}
    assert werte
    assert werte <= {21.5, 22.0}
    assert d["soll_ist"]["heizt"] == []
    # mit hvac_action: Heizstunden wie gewohnt
    wz = await (await app_client_generisch.get("/api/auswertung/wohnzimmer?bereich=24h")).json()
    assert wz["heizstunden"] is not None
    assert wz["heizstunden"] > 0


async def test_auswertung_hvac_action_vorhanden(app_client_generisch, fake_generisch):
    _bad_klima_verlauf(fake_generisch, mit_action=True)
    d = await (await app_client_generisch.get("/api/auswertung/badezimmer?bereich=24h")).json()
    assert d["heizstunden"] > 20


async def test_aktuell_generisch(app_client_generisch, fake_generisch):
    fake_generisch.generisch["climate.gaestezimmer_heizung"]["attributes"].pop("hvac_action")
    resp = await app_client_generisch.get("/api/aktuell")
    d = await resp.json()
    assert resp.status == 200, d
    bad = d["raeume"]["badezimmer"]
    assert bad["soll"] is None
    assert bad["soll_bereich"] == {"min": 20.0, "max": 24.0}
    assert d["raeume"]["gaestezimmer_heizung"]["hvac_action"] is None
    assert "soll_bereich" not in d["raeume"]["wohnzimmer"]
    assert d["raeume"]["schlafzimmer"]["modus"] == "unavailable"
    for r in d["raeume"].values():
        assert (r["grund"], r["anzeige"], r["schimmel"], r["lueften"]) == (None, None, None, None)
    assert d["empfehlungen"] == []


async def test_aktuell_ist_aus_raumsensor(app_client_generisch, fake_generisch):
    fake_generisch.generisch["climate.wohnzimmer_thermostat"]["attributes"]["current_temperature"] = None
    d = await (await app_client_generisch.get("/api/aktuell")).json()
    assert d["raeume"]["wohnzimmer"]["ist"] is not None


async def test_aktuell_pm_ohne_neue_felder(app_client):
    d = await (await app_client.get("/api/aktuell")).json()
    for r in d["raeume"].values():
        assert "soll_bereich" not in r
        assert set(r) == {
            "modus",
            "hvac_action",
            "anzeige",
            "grund",
            "preset",
            "ist",
            "soll",
            "feuchte",
            "schimmel",
            "co2",
            "luftqualitaet",
            "fenster",
            "lueften",
        }


@pytest.mark.parametrize("bereich", ["24h", "7d", "30d"])
async def test_uebersicht_generisch(app_client_generisch, fake_generisch, bereich):
    _bad_klima_verlauf(fake_generisch)
    resp = await app_client_generisch.get(f"/api/uebersicht?bereich={bereich}")
    d = await resp.json()
    assert resp.status == 200, d
    kacheln = {k["raum"]: k for k in d["raeume"]}
    assert {"wohnzimmer", "kuche", "badezimmer", "schlafzimmer", "gaestezimmer_heizung"} <= set(kacheln)
    assert kacheln["badezimmer"]["heizstunden"] is None
    assert all("schimmel" not in k for k in kacheln.values())


async def test_bericht_generisch(app_client_generisch, fake_generisch):
    _bad_klima_verlauf(fake_generisch)
    resp = await app_client_generisch.post("/api/berichte", json={"push": False})
    d = await resp.json()
    assert resp.status == 200, d
    raeume = {r["raum"]: r for r in d["raeume"]}
    assert raeume["badezimmer"]["heizstunden"] is None
    assert "Bad" in d["text"] or "Badezimmer" in d["text"]
    assert "keine Daten" in d["text"]
    assert "!" not in d["text"]


def test_bericht_ohne_schimmel_text():
    tz = ZoneInfo("Europe/Berlin")
    ende = datetime(2026, 1, 14, 12, tzinfo=UTC)
    analysen = [{"raum": "wz", "name": "Wohnzimmer", "heizstunden": None}]
    mit = build_report(analysen, ende - timedelta(days=7), ende, tz, rng=random.Random(1))
    ohne = build_report(analysen, ende - timedelta(days=7), ende, tz, rng=random.Random(1), schimmel=False)
    assert "Schimmelrisiko" in mit["text"]
    assert "Schimmelrisiko" not in ohne["text"]
    assert "Wohnzimmer: keine Daten" in ohne["text"]


async def test_steuerung_und_coach_ohne_registrierte_raeume(aiohttp_client, ha_client_generisch, fake_generisch, tmp_path):
    """Ohne Thermostate (leeres Zuhause) liefern Coach und Übersicht leere Ergebnisse statt Fehler."""
    from klimastudio.config import Options

    fake_generisch.generisch.clear()
    opts = Options.from_dict({"betriebsart": "generisch"})
    ks = KlimaStudio(opts, ha_client_generisch, data_dir=tmp_path)
    try:
        app = await aiohttp_client(create_app(ks, networks=LOCAL))
        for pfad in ("/api/coach", "/api/coach/lagebericht", "/api/uebersicht", "/api/aktuell"):
            resp = await app.get(pfad)
            assert resp.status == 200, (pfad, await resp.text())
    finally:
        await ks.heizperiode.stop()
        ks.store.close()


def test_room_ohne_pm_felder_im_kontext():
    """raum_kontext mit einem minimalen Adapter-Double (nur raum_zustand, betriebsart)."""
    from klimastudio.coach.lage import raum_kontext

    class Double:
        betriebsart = "generisch"

        def raum_zustand(self, room, by_id):
            return {
                "modus": "hand",
                "preset": None,
                "grund": "manuell",  # würde ein Adapter PM-Werte liefern, bleiben sie generisch leer
                "hvac_action": None,
                "ist": 20.0,
                "soll": 21.0,
                "zeitplan_temperatur": None,
                "feuchte": 50.0,
                "fenster_offen": None,
                "overlay_bis": None,
                "boost_bis": None,
            }

    room = Room(raum="wz", name="WZ", climate="climate.x", lueften="binary_sensor.l", luftqualitaet="sensor.lq")
    by_id = {
        "climate.x": {"state": "heat", "attributes": {}},
        "binary_sensor.l": {"state": "on"},
        "sensor.lq": {"state": "schlecht"},
    }
    ctx = raum_kontext(room, copy.deepcopy(by_id), Double(), {}, {})
    assert ctx["modus"] == "hand"
    assert (ctx["grund"], ctx["luftqualitaet"], ctx["lueften_empfohlen"]) == (None, None, None)
    assert ctx["soll_ist_abstand"] == 1.0
