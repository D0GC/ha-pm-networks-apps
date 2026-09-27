"""Klima-Coach: Regel-Engine, Ablage, Lage-Kontext, KI und API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time

import pytest

from conftest import LOCAL, WISSEN_TEST
from fake_ha import TOKEN
from klimastudio import __version__
from klimastudio.coach import ki
from klimastudio.coach import store as store_mod
from klimastudio.coach.engine import (
    WISSEN_DATEI,
    Regelwerk,
    auswerten,
    bedingung_erfuellt,
    format_wert,
    formatiere,
    pruefe_bedingung,
    wert,
)
from klimastudio.coach.lage import GLOBAL_PFADE, RAUM_PFADE, anwesenheit, plan_kennzahlen
from klimastudio.coach.store import CoachStore
from klimastudio.config import Options
from klimastudio.server import KlimaStudio, create_app

# Pfade aus dem Entwurf D1 (verbindlich, die Wissensdatenbank nutzt genau diese)
_SPEC_TEXT = """
raum.slug raum.name raum.nassraum raum.schlafraum raum.modus raum.preset raum.grund raum.hvac_action raum.ist
raum.soll raum.zeitplan_temperatur raum.feuchte raum.schimmel raum.co2 raum.luftqualitaet raum.fenster_offen
raum.lueften_empfohlen raum.overlay_aktiv raum.overlay_dauerhaft raum.boost_aktiv raum.soll_ist_abstand
raum.woche.heizstunden raum.woche.feuchte_mittel raum.woche.feuchte_max raum.woche.feuchte_stunden_70
raum.woche.feuchte_stunden_80 raum.woche.schimmel_max raum.woche.schimmel_stunden_70
raum.woche.schimmel_stunden_80 raum.woche.co2_max raum.woche.co2_stunden_1000 raum.woche.co2_stunden_1400
raum.woche.fenster_anzahl raum.woche.fenster_offen_stunden raum.woche.fenster_laengste_min
raum.woche.lueftung_bewertung raum.woche.lueftung_anzahl raum.plan.max_temp raum.plan.min_temp
raum.plan.stunden_woche raum.plan.nacht raum.plan.leer raum.plan.tage_ohne_block aussen.temperatur
aussen.feuchte aussen.tagesmittel_gestern wetter.min_morgen wetter.max_morgen wetter.min_3tage
wetter.regen_morgen zeit.monat zeit.stunde zeit.wochentag heizperiode.aktiv heizperiode.modus
integration.aktiv integration.gesperrt anwesenheit.jemand_zuhause anwesenheit.anzahl_zuhause
"""
SPEC_PFADE = set(_SPEC_TEXT.split())

CTX = {
    "raum": {"slug": "bad", "ist": 20.0, "soll": 21.5, "modus": "auto", "nass": True, "leer": None, "woche": {"h": 3}},
    "aussen": {"temperatur": 5.24},
}


def B(w, op, **kw):
    return {"wert": w, "op": op, **kw}


# ------------------------------------------------------------------ Engine


@pytest.mark.parametrize(
    ("bed", "erwartet"),
    [
        (B("raum.ist", "<", ref=21), True),
        (B("raum.ist", "<", ref=20), False),
        (B("raum.ist", "<=", ref=20), True),
        (B("raum.ist", ">", ref=19.9), True),
        (B("raum.ist", ">=", ref=20.1), False),
        (B("raum.ist", "==", ref=20), True),
        (B("raum.ist", "!=", ref=20), False),
        (B("raum.modus", "==", ref="auto"), True),
        (B("raum.modus", "!=", ref="heat"), True),
        (B("raum.nass", "==", ref=True), True),
        (B("raum.nass", "==", ref=1), False),  # bool ist keine Zahl
        (B("raum.modus", "in", ref=["heat", "auto"]), True),
        (B("raum.modus", "in", ref=["off"]), False),
        (B("raum.ist", "vorhanden"), True),
        (B("raum.leer", "vorhanden"), False),
        (B("raum.leer", "fehlt"), True),
        (B("raum.gibtesnicht", "fehlt"), True),
        (B("raum.ist", "fehlt"), False),
        (B("raum.woche.h", ">=", ref=3), True),
        # fehlender Wert -> Atom falsch, auch für != und in
        (B("raum.leer", "<", ref=100), False),
        (B("raum.leer", "!=", ref=1), False),
        (B("raum.leer", "in", ref=[1, 2]), False),
        (B("raum.soll", "!=", pfad="raum.leer"), False),
        ({"nicht": B("raum.leer", "!=", ref=1)}, True),
        # vorhandener Wert, anderer Typ: == falsch, != wahr
        (B("raum.modus", "!=", ref=1), True),
        (B("raum.modus", "==", ref=1), False),
        # Pfadvergleich mit Offset
        (B("raum.soll", ">", pfad="raum.ist"), True),
        (B("raum.soll", ">", pfad="raum.ist", plus=1.5), False),
        (B("raum.soll", ">=", pfad="raum.ist", plus=1.5), True),
        (B("raum.soll", ">", pfad="raum.leer"), False),
        (B("raum.soll", ">", pfad="raum.modus", plus=1), False),
        # Verknüpfungen
        ({"alle": [B("raum.ist", "<", ref=21), B("raum.modus", "==", ref="auto")]}, True),
        ({"alle": [B("raum.ist", "<", ref=21), B("raum.modus", "==", ref="off")]}, False),
        ({"eine": [B("raum.leer", ">", ref=1), B("raum.modus", "==", ref="auto")]}, True),
        ({"eine": [B("raum.leer", ">", ref=1), B("raum.modus", "==", ref="off")]}, False),
        ({"nicht": B("raum.modus", "==", ref="off")}, True),
        ({"nicht": B("raum.leer", ">", ref=1)}, True),  # nicht(fehlender Wert) ist wahr
        ({"nicht": {"alle": [B("raum.ist", "vorhanden"), B("aussen.temperatur", "<", ref=10)]}}, False),
        # Kein Attributzugriff auf Python-Objekte
        (B("raum.__class__", "vorhanden"), False),
    ],
)
def test_conditions(bed, erwartet):
    pruefe_bedingung(bed)
    assert bedingung_erfuellt(bed, CTX) is erwartet


@pytest.mark.parametrize(
    "bed",
    [
        None,
        {},
        {"alle": []},
        {"alle": [B("a.b", "==", ref=1)], "eine": []},
        {"nicht": [B("a.b", "==", ref=1)]},
        B("a.b", "~", ref=1),
        B("a.b", "<", ref="x"),
        B("a.b", "<"),
        B("a.b", "<", ref=None),
        B("a.b", "in", ref=1),
        B("a.b", "in", ref=[]),
        B("a.b", "<", ref=1, pfad="c.d"),
        B("a.b", "<", pfad="c.d", plus="1"),
        B("A.B", "==", ref=1),
        B("a.b", "==", ref=1, code="import os"),
        B("a b", "vorhanden"),
    ],
)
def test_invalid_conditions(bed):
    from klimastudio.coach.engine import RegelFehler

    with pytest.raises(RegelFehler):
        pruefe_bedingung(bed)


def test_condition_depth_limit():
    from klimastudio.coach.engine import RegelFehler

    bed = B("a.b", "vorhanden")
    for _ in range(20):
        bed = {"nicht": bed}
    with pytest.raises(RegelFehler):
        pruefe_bedingung(bed)


def test_placeholder_formatting():
    assert format_wert(21.46) == "21,5"
    assert format_wert(21.0) == "21"
    assert format_wert(7) == "7"
    assert format_wert(-0.04) == "0"
    assert format_wert(True) == "ja"
    assert format_wert(False) == "nein"
    assert format_wert(None) == "–"
    assert format_wert("mäßig") == "mäßig"
    text = formatiere("{raum.slug}: {aussen.temperatur} °C, nass {raum.nass}, {raum.leer}, {raum.fehlt} {kein pfad}", CTX)
    assert text == "bad: 5,2 °C, nass ja, –, – {kein pfad}"
    assert wert(CTX, "raum.woche.h") == 3
    assert wert(CTX, "raum.ist.x") is None


def regel(**kw):
    base = {
        "id": "r",
        "bereich": "raum",
        "thema": "heizen",
        "prioritaet": 2,
        "saison": "immer",
        "wenn": B("raum.ist", "vorhanden"),
        "titel": "T {raum.name}",
        "text": "Ist {raum.ist} °C",
        "begruendung": "B",
    }
    return {**base, **kw}


def test_rule_validation_skips_invalid(caplog):
    caplog.set_level(logging.WARNING)
    werk = Regelwerk.aus_daten(
        {
            "version": "x",
            "regeln": [
                regel(id="gut", massnahme={"typ": "steuerung", "aktion": "overlay", "temperatur": 21.3, "dauer": 60}),
                regel(id="gut", thema="lueften"),  # doppelt
                regel(id="Böse"),
                regel(id="prio", prioritaet=True),
                regel(id="thema", thema="kochen"),
                regel(id="saison", saison="winter"),
                regel(id="monate", monate=[0]),
                regel(id="wenn", wenn={"wert": "raum.ist", "op": "eval"}),
                regel(id="massnahme", massnahme={"typ": "ausfuehren"}),
                regel(id="massnahme2", massnahme={"typ": "steuerung", "temperatur": 99}),
                regel(id="titel", titel=""),
                "keine regel",
                regel(id="global_ok", bereich="global", monate=[1, 2]),
            ],
        }
    )
    assert [r["id"] for r in werk.regeln] == ["gut", "global_ok"]
    assert werk.regeln[0]["massnahme"] == {"typ": "steuerung", "aktion": "overlay", "temperatur": 21.5, "dauer": 60}
    assert len(werk.fehler) == 11
    assert "übersprungen" in caplog.text


def test_missing_knowledge_file(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    werk = Regelwerk.laden(tmp_path / "fehlt.json")
    assert werk.regeln == []
    assert "fehlt" in caplog.text
    (tmp_path / "kaputt.json").write_text("{kein json")
    assert Regelwerk.laden(tmp_path / "kaputt.json").regeln == []
    (tmp_path / "leer.json").write_text("[]")
    assert Regelwerk.laden(tmp_path / "leer.json").regeln == []


def _ctx(raum=None, aktiv=True, monat=11):
    g = {"raum": None, "heizperiode": {"aktiv": aktiv, "modus": "automatik"}, "zeit": {"monat": monat}, "aussen": {}}
    return g if raum is None else {**g, "raum": raum}


def test_evaluate_season_months_sort_and_hide():
    werk = Regelwerk.aus_daten(
        {
            "regeln": [
                regel(id="heiz", saison="heizperiode", prioritaet=2, thema="lueften"),
                regel(id="sommer", saison="sommer"),
                regel(id="immer", prioritaet=3),
                regel(id="dringend", prioritaet=1),
                regel(id="thema_a", prioritaet=2, thema="energie"),
                regel(id="nur_januar", monate=[1]),
                regel(id="glob", bereich="global", wenn=B("raum.ist", "fehlt"), text="G {raum.name}"),
            ]
        }
    )
    raeume = [_ctx({"slug": "bad", "name": "Bad", "ist": 20.0}), _ctx({"slug": "kueche", "name": "Küche", "ist": None})]
    tipps = auswerten(werk, _ctx(), raeume)
    assert [t["id"] for t in tipps] == ["dringend:bad", "thema_a:bad", "glob:global", "heiz:bad", "immer:bad"]
    bad = tipps[0]
    assert bad["raum"] == "bad"
    assert bad["raum_name"] == "Bad"
    assert bad["titel"] == "T Bad"
    assert bad["text"] == "Ist 20 °C"
    assert tipps[2]["raum"] is None
    assert tipps[2]["text"] == "G –"
    assert set(bad) == {"id", "regel", "raum", "raum_name", "thema", "prioritaet", "titel", "text", "begruendung", "massnahme"}
    # Sommer
    ids = [t["id"] for t in auswerten(werk, _ctx(aktiv=False), [_ctx({"slug": "bad", "ist": 1}, aktiv=False)])]
    assert "sommer:bad" in ids
    assert "heiz:bad" not in ids
    # Unbekannte Heizperiode: Monat entscheidet (Juli = Sommer, Januar = Heizperiode)
    ids = [t["id"] for t in auswerten(werk, _ctx(aktiv=None, monat=7), [_ctx({"slug": "bad", "ist": 1}, None, 7)])]
    assert "sommer:bad" in ids
    ids = [t["id"] for t in auswerten(werk, _ctx(aktiv=None, monat=1), [_ctx({"slug": "bad", "ist": 1}, None, 1)])]
    assert {"heiz:bad", "nur_januar:bad"} <= set(ids)
    # Ausblenden
    ids = [t["id"] for t in auswerten(werk, _ctx(), raeume, ausgeblendet={"dringend:bad"})]
    assert "dringend:bad" not in ids


def test_real_knowledge_file_is_valid_and_uses_spec_paths():
    werk = Regelwerk.laden(WISSEN_DATEI)
    assert werk.fehler == []
    assert len(werk.regeln) > 0
    assert werk.version
    benutzt: set[str] = set()

    def sammle(b):
        for key in ("alle", "eine"):
            if key in b:
                for x in b[key]:
                    sammle(x)
                return
        if "nicht" in b:
            sammle(b["nicht"])
            return
        benutzt.add(b["wert"])
        if "pfad" in b:
            benutzt.add(b["pfad"])

    for r in werk.regeln:
        sammle(r["wenn"])
        for feld in ("titel", "text", "begruendung"):
            benutzt.update(re.findall(r"\{([a-z_.0-9]+)\}", r[feld]))
    assert benutzt <= SPEC_PFADE


def test_lage_paths_cover_spec():
    assert set(RAUM_PFADE) | set(GLOBAL_PFADE) == SPEC_PFADE


# ------------------------------------------------------------------ Ablage


async def test_store_schema_and_settings(tmp_path):
    st = CoachStore(tmp_path / "sub" / "coach.db")
    assert await st.einstellung("x") is None
    await st.einstellung_setzen("x", {"a": 1, "ä": [1.5]})
    await st.einstellung_setzen("x", {"a": 2})
    assert await st.einstellung("x") == {"a": 2}
    assert st.schema_version() == store_mod.SCHEMA_VERSION
    st.close()
    conn = sqlite3.connect(tmp_path / "sub" / "coach.db")
    tabellen = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"einstellungen", "rueckmeldungen", "ki_laeufe", "ereignisse"} <= tabellen
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    # Wiederöffnen
    st = CoachStore(tmp_path / "sub" / "coach.db")
    assert await st.einstellung("x") == {"a": 2}
    st.close()


async def test_store_feedback_and_retention(tmp_path, monkeypatch):
    st = CoachStore(tmp_path / "coach.db")
    now = time.time()
    await st.rueckmeldung("a:bad", "ausblenden", now + 3600)
    await st.rueckmeldung("b:bad", "ausblenden", now - 10)
    await st.rueckmeldung("c:bad", "hilfreich", None)
    assert await st.ausgeblendet() == {"a:bad"}
    assert await st.ausgeblendet(now + 7200) == set()

    monkeypatch.setattr(store_mod, "MAX_EREIGNISSE", 10)
    for i in range(15):
        await st.ereignis("steuerung" if i % 2 else "heizperiode", "bad", f"e{i}", {"i": i})
    alle = await st.ereignisse()
    assert len(alle) == 10
    assert alle[0]["text"] == "e14"
    assert alle[0]["daten"] == {"i": 14}
    assert {e["typ"] for e in await st.ereignisse("steuerung")} == {"steuerung"}

    monkeypatch.setattr(store_mod, "MAX_KI_LAEUFE", 5)
    for i in range(8):
        lauf = {"id": f"{i:012x}", "erstellt": f"2026-01-01T00:00:{i:02d}+00:00", "entitaet": "ai_task.x", "tipps": [{"a": 1}]}
        await st.ki_lauf_speichern(lauf, {"lage": i})
    laeufe = await st.ki_laeufe(20)
    assert [r["id"] for r in laeufe] == [f"{i:012x}" for i in (7, 6, 5, 4, 3)]
    assert laeufe[0]["tipps"] == [{"a": 1}]
    assert await st.ki_lauf(f"{0:012x}") is None
    st.close()


# ------------------------------------------------------------------ KI (rein)


def test_parse_answer_variants():
    obj = {"zusammenfassung": "Z", "tipps": []}
    assert ki.parse_antwort(obj) is obj
    assert ki.parse_antwort(json.dumps(obj)) == obj
    assert ki.parse_antwort("Gern:\n```json\n" + json.dumps(obj) + "\n```\nViel Erfolg") == obj
    assert ki.parse_antwort("Vorwort " + json.dumps(obj) + " Nachwort") == obj
    for bad in ("", None, "kein json", "{kaputt", "[1, 2]", 42):
        with pytest.raises(ValueError, match="KI"):
            ki.parse_antwort(bad)


def test_sanitize_answer():
    raeume = {"badezimmer": "Badezimmer", "kuche": "Küche"}
    tipps = [
        {
            "titel": "A\x07",
            "text": "x" * 900,
            "prioritaet": 7,
            "raum": "keller",
            "massnahme": {"typ": "sprengen"},
        },
        {"titel": "B", "text": "t", "prioritaet": 1, "raum": "Küche", "massnahme": {"typ": "steuerung", "temperatur": 21.3}},
        {"titel": "C", "text": "t", "raum": "badezimmer", "massnahme": {"typ": "steuerung", "temperatur": 40, "dauer": 5000}},
        {"titel": "D", "text": "t", "massnahme": {"typ": "heizplan", "dauer": 60.0}},
        "kein objekt",
        {"titel": "", "text": ""},
        *({"titel": f"T{i}", "text": "t"} for i in range(10)),
    ]
    out = ki.bereinige({"zusammenfassung": "Z‮", "tipps": tipps}, raeume, "abc")
    assert out["zusammenfassung"] == "Z"
    t = out["tipps"]
    assert len(t) == 8
    assert t[0]["titel"] == "A"
    assert len(t[0]["text"]) == 600
    assert t[0]["prioritaet"] == 2
    assert t[0]["raum"] is None
    assert t[0]["massnahme"] is None
    assert t[1]["raum"] == "kuche"
    assert t[1]["raum_name"] == "Küche"
    assert t[1]["massnahme"] == {"typ": "steuerung", "temperatur": 21.5}
    assert t[2]["massnahme"] == {"typ": "steuerung"}
    assert t[3]["massnahme"] == {"typ": "heizplan", "dauer": 60}
    assert t[0]["id"] == "ki_abc_1:global"
    assert t[1]["id"] == "ki_abc_2:kuche"
    with pytest.raises(ValueError, match="Schema"):
        ki.bereinige({"antwort": "x"}, raeume, "abc")


# ------------------------------------------------------------------ API


async def test_coach_api_tips(app_client, fake, studio):
    d = await (await app_client.get("/api/coach")).json()
    assert d["wissen_version"] == "test-1"
    assert d["saison"] == "heizperiode"  # Außentemperatur der Simulation ca. 8 °C
    assert isinstance(d["stand"], int)
    assert [e["raum"] for e in d["integration"]] == ["Wohnzimmer", "Badezimmer"]
    ids = [t["id"] for t in d["tipps"]]
    assert ids[0] == "bad_steuern:badezimmer"
    assert {f"feuchte_anzeigen:{s}" for s in ("wohnzimmer", "kuche", "schlafzimmer", "badezimmer")} <= set(ids)
    assert "heizperiode_global:global" in ids
    assert not any(i.startswith(("nie:", "sommer_global")) for i in ids)
    wz = next(t for t in d["tipps"] if t["id"] == "feuchte_anzeigen:wohnzimmer")
    assert wz["text"].startswith("Die Feuchte in Wohnzimmer liegt bei ")
    assert "." not in wz["text"][:-1]  # Dezimalkomma
    assert wz["begruendung"] == "Nassraum: nein."
    glob = next(t for t in d["tipps"] if t["id"] == "heizperiode_global:global")
    assert glob["text"].endswith("Raum –.")


async def test_coach_feedback(app_client, fake, studio):
    await app_client.get("/api/coach")
    resp = await app_client.post("/api/coach/rueckmeldung", json={"id": "feuchte_anzeigen:kuche", "aktion": "ausblenden"})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    ids = [t["id"] for t in (await (await app_client.get("/api/coach")).json())["tipps"]]
    assert "feuchte_anzeigen:kuche" not in ids
    await app_client.post("/api/coach/rueckmeldung", json={"id": "feuchte_anzeigen:wohnzimmer", "aktion": "hilfreich"})
    await app_client.post("/api/coach/rueckmeldung", json={"id": "bad_steuern:badezimmer", "aktion": "erledigt"})
    ids = [t["id"] for t in (await (await app_client.get("/api/coach")).json())["tipps"]]
    assert "feuchte_anzeigen:wohnzimmer" in ids
    assert "bad_steuern:badezimmer" not in ids
    for body in (
        {"id": "x", "aktion": "ausblenden"},
        {"id": "a:b", "aktion": "loeschen"},
        {"id": "a:b", "aktion": "ausblenden", "tage": 0},
        {"id": "a:b", "aktion": "ausblenden", "tage": 91},
        {"id": "a:b", "aktion": "ausblenden", "tage": "7"},
        {"id": "a:b", "aktion": "hilfreich", "tage": 7},
        {"id": "a:b<script>", "aktion": "hilfreich"},
    ):
        resp = await app_client.post("/api/coach/rueckmeldung", json=body)
        assert resp.status == 400, body


def _pfad_vorhanden(ctx, pfad):
    cur = ctx
    for teil in pfad.split("."):
        if not isinstance(cur, dict) or teil not in cur:
            return False
        cur = cur[teil]
    return True


async def test_lage_context_has_all_paths(studio):
    lage = await studio.coach.lage.erstellen()
    for pfad in GLOBAL_PFADE:
        assert _pfad_vorhanden(lage["global"], pfad), pfad
    assert lage["global"]["raum"] is None
    assert len(lage["raeume"]) == 4
    for ctx in lage["raeume"]:
        for pfad in (*RAUM_PFADE, *GLOBAL_PFADE):
            assert _pfad_vorhanden(ctx, pfad), pfad
    bad = next(c["raum"] for c in lage["raeume"] if c["raum"]["slug"] == "badezimmer")
    assert bad["nassraum"] is True
    assert bad["schlafraum"] is False
    assert bad["modus"] == "auto"
    assert bad["overlay_aktiv"] is False
    assert bad["woche"]["heizstunden"] > 0
    assert bad["woche"]["feuchte_max"] > 70
    assert bad["plan"]["max_temp"] == 25
    assert bad["plan"]["min_temp"] == 22
    assert bad["plan"]["leer"] is False
    assert bad["plan"]["nacht"] is False
    assert bad["plan"]["tage_ohne_block"] == 0
    assert bad["plan"]["stunden_woche"] == 5 * 6 + 2 * 4
    g = lage["global"]
    assert g["wetter"] == {"min_morgen": 3.0, "max_morgen": 11.0, "min_3tage": 3.0, "regen_morgen": True}
    assert g["anwesenheit"] == {"jemand_zuhause": True, "anzahl_zuhause": 1}
    assert g["integration"] == {"aktiv": True, "gesperrt": False}
    assert g["heizperiode"]["modus"] == "automatik"
    assert g["heizperiode"]["aktiv"] is True
    assert isinstance(g["aussen"]["tagesmittel_gestern"], float)
    assert 0 <= g["zeit"]["wochentag"] <= 6


def test_plan_kennzahlen_ignores_setback_blocks():
    def blk(von, bis, temp):
        return {"start": von * 60, "end": bis * 60, "temp": temp, "extra": {}}

    tag = [blk(0, 6, 17.0), blk(6, 22, 21.0), blk(22, 24, 16.5)]
    p = plan_kennzahlen({d: list(tag) for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")})
    assert p["nacht"] is False  # abgesenkter Nachtblock ist gewollt
    assert p["stunden_woche"] == 7 * 16
    assert (p["min_temp"], p["max_temp"]) == (16.5, 21.0)
    warm = plan_kennzahlen({"monday": [blk(0, 6, 18.0)], "tuesday": [blk(0, 5, None)]})
    assert warm["nacht"] is True
    assert warm["stunden_woche"] == 11
    assert plan_kennzahlen({"monday": [blk(0, 24, 17.5)]})["stunden_woche"] == 0


def test_presence_ignores_unknown_persons_without_helper():
    by_id = {
        "person.johanna": {"state": "home", "attributes": {"friendly_name": "Johanna"}},
        "person.dashboard": {"state": "unknown", "attributes": {"friendly_name": "Dashboard"}},
        "person.tablet": {"state": "unavailable", "attributes": {}},
        "person.lena_berger": {"state": "unknown", "attributes": {"friendly_name": "Lena Berger"}},
        "input_boolean.lena_ist_zuhause": {"state": "off"},
    }
    zusammen, personen = anwesenheit(by_id)
    assert {p["name"]: p["zuhause"] for p in personen} == {"Johanna": True, "Lena Berger": False}
    assert zusammen == {"jemand_zuhause": True, "anzahl_zuhause": 1}
    zusammen, personen = anwesenheit({"person.dashboard": {"state": "unknown", "attributes": {}}})
    assert personen == []
    assert zusammen == {"jemand_zuhause": None, "anzahl_zuhause": None}


async def test_weather_uses_return_response(studio, fake):
    await studio.coach.lage.erstellen()
    calls = [c for c in fake.service_calls if c[0] == "weather"]
    assert calls == [("weather", "get_forecasts", {"entity_id": "weather.dwd_zuhause", "type": "daily"})]
    # Wetter-Entität fehlt -> Werte None, kein Fehler
    del fake.extra["weather.dwd_zuhause"]
    studio.coach.lage._wetter = None
    lage = await studio.coach.lage.erstellen()
    assert lage["global"]["wetter"] == {"min_morgen": None, "max_morgen": None, "min_3tage": None, "regen_morgen": None}


VERBOTEN = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b[0-9a-f]{32}\b|device_id|access_token|Bearer")


async def test_lagebericht_with_presence(app_client, fake):
    fake.extra["person.dashboard"] = ("unknown", {"friendly_name": "Dashboard"})
    d = await (await app_client.get("/api/coach/lagebericht")).json()
    assert d["entitaet"] == "ai_task.claude_ai_task"
    assert d["anwesenheit"] is True
    lage = d["lage"]
    assert d["zeichen"] == len(json.dumps(lage, ensure_ascii=False, separators=(",", ":")))
    assert {p["name"]: p["zuhause"] for p in lage["personen"]} == {"Johanna": "ja", "Lena Berger": "nein"}
    assert lage["anwesenheit"]["anzahl_zuhause"] == 1
    assert [r["raum"] for r in lage["raeume"]] == ["badezimmer", "kuche", "schlafzimmer", "wohnzimmer"]
    bad = lage["raeume"][0]
    assert bad["heizplan"]["mo,di,mi,do,fr"] == ["05:00-07:00 25 °C", "18:00-22:00 22 °C"]
    assert "verlauf" not in json.dumps(lage)
    assert len(lage["wetter"]["tage"]) == 3
    assert lage["lokale_tipps"]
    assert lage["integration"]["aktiv"] is True
    text = json.dumps(d, ensure_ascii=False)
    assert TOKEN not in text
    assert not VERBOTEN.search(text)


async def test_lagebericht_without_presence(aiohttp_client, ha_client, fake, tmp_path):
    opts = Options.from_dict({"coach_anwesenheit": False})
    ks = KlimaStudio(opts, ha_client, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    client = await aiohttp_client(create_app(ks, networks=LOCAL))
    d = await (await client.get("/api/coach/lagebericht")).json()
    assert d["anwesenheit"] is False
    assert "personen" not in d["lage"]
    assert "anwesenheit" not in d["lage"]
    assert "Johanna" not in json.dumps(d, ensure_ascii=False)
    resp = await client.post("/api/coach/ki", json={})
    assert resp.status == 200
    instructions = next(c[2]["instructions"] for c in fake.service_calls if c[0] == "ai_task")
    assert "Johanna" not in instructions
    assert "Lena" not in instructions
    info = await (await client.get("/api/info")).json()
    assert info["coach"] == {"ki_entitaet": "ai_task.claude_ai_task", "anwesenheit": False}
    ks.store.close()


async def test_ki_run_success(app_client, fake):
    resp = await app_client.post("/api/coach/ki", json={})
    d = await resp.json()
    assert resp.status == 200, d
    assert set(d) == {"id", "erstellt", "entitaet", "dauer_s", "zusammenfassung", "tipps", "roh", "fehler"}
    assert d["fehler"] is None
    assert d["roh"] is None
    assert d["entitaet"] == "ai_task.claude_ai_task"
    assert d["zusammenfassung"].startswith("Die Räume")
    assert [t["raum"] for t in d["tipps"]] == ["badezimmer", "wohnzimmer"]
    assert d["tipps"][0]["raum_name"] == "Badezimmer"
    assert d["tipps"][1]["massnahme"] == {"typ": "steuerung", "temperatur": 20.5, "dauer": 60}
    domain, service, data = next(c for c in fake.service_calls if c[0] == "ai_task")
    assert (domain, service) == ("ai_task", "generate_data")
    assert set(data) == {"task_name", "entity_id", "instructions"}
    assert data["task_name"] == "klima_coach"
    assert "Lagebericht (JSON)" in data["instructions"]
    assert "Sie-Form" in data["instructions"]
    assert TOKEN not in data["instructions"]
    # Keine Maßnahme wurde ausgeführt
    assert [c for c in fake.service_calls if c[0] in ("pm_heizung", "climate")] == []
    liste = await (await app_client.get("/api/coach/ki")).json()
    assert liste == [
        {
            "id": d["id"],
            "erstellt": d["erstellt"],
            "entitaet": "ai_task.claude_ai_task",
            "zusammenfassung": d["zusammenfassung"],
            "anzahl_tipps": 2,
            "fehler": None,
        }
    ]
    einzel = await (await app_client.get(f"/api/coach/ki/{d['id']}")).json()
    assert einzel == d
    assert (await app_client.get("/api/coach/ki/000000000000")).status == 404
    assert (await app_client.get("/api/coach/ki/..%2Fetc")).status == 404


async def test_ki_run_fenced_and_dict(app_client, fake):
    fake.ki_antwort = "Hier ist die Auswertung:\n```json\n" + fake.ki_antwort + "\n```"
    d = await (await app_client.post("/api/coach/ki", json={})).json()
    assert d["fehler"] is None
    assert len(d["tipps"]) == 2
    fake.ki_antwort = {"zusammenfassung": "Strukturiert", "tipps": []}
    d = await (await app_client.post("/api/coach/ki", json={})).json()
    assert d["zusammenfassung"] == "Strukturiert"


async def test_ki_run_broken_json(app_client, fake):
    fake.ki_antwort = 'Leider {"zusammenfassung": "abgeschnitten'
    d = await (await app_client.post("/api/coach/ki", json={})).json()
    assert d["fehler"]
    assert d["roh"] == fake.ki_antwort
    assert d["tipps"] == []
    liste = await (await app_client.get("/api/coach/ki")).json()
    assert liste[0]["fehler"] == d["fehler"]


async def test_ki_run_parallel_conflict(app_client, fake):
    fake.ki_delay = 0.5
    r1, r2 = await asyncio.gather(app_client.post("/api/coach/ki", json={}), app_client.post("/api/coach/ki", json={}))
    assert sorted([r1.status, r2.status]) == [200, 409]
    konflikt = r1 if r1.status == 409 else r2
    assert (await konflikt.json())["code"] == "laeuft"


async def test_ki_missing_entity_and_ha_error(app_client, fake, studio):
    del fake.extra["ai_task.claude_ai_task"]
    resp = await app_client.post("/api/coach/ki", json={})
    body = await resp.json()
    assert resp.status == 409
    assert body["code"] == "ki_fehlt"
    assert "coach_ki_entitaet" in body["fehler"]
    fake.extra["ai_task.claude_ai_task"] = ("unknown", {})
    fake.ki_fehler = "Error talking to API: overloaded"
    resp = await app_client.post("/api/coach/ki", json={})
    assert resp.status == 502
    assert (await resp.json())["fehler"] == "Der KI-Dienst meldet: Error talking to API: overloaded"
    liste = await (await app_client.get("/api/coach/ki")).json()
    assert liste[0]["fehler"] == "Der KI-Dienst meldet: Error talking to API: overloaded"
    call = [c for c in fake.ws_commands if c["type"] == "call_service" and c["domain"] == "ai_task"][-1]
    assert call["return_response"] is True
    assert call["service_data"]["entity_id"] == "ai_task.claude_ai_task"


async def test_real_knowledge_evaluates_against_fake(ha_client, fake, tmp_path):
    ks = KlimaStudio(Options(), ha_client, data_dir=tmp_path)
    assert ks.coach.regelwerk.fehler == []
    fake.climate["wohnzimmer"].update(overlay_bis="dauerhaft")
    fake.climate["kuche"]["state"] = "off"
    d = await ks.coach.uebersicht()
    assert d["wissen_version"] == ks.coach.regelwerk.version
    for t in d["tipps"]:
        for feld in ("titel", "text", "begruendung"):
            assert "{" not in t[feld], t
            assert "!" not in t[feld]
    ks.store.close()


async def test_events_api(app_client, fake):
    await app_client.post("/api/steuerung/kuche", json={"aktion": "zurueck"})
    ev = await (await app_client.get("/api/ereignisse?typ=steuerung")).json()
    assert ev[0]["typ"] == "steuerung"
    assert ev[0]["raum"] == "kuche"
    assert ev[0]["daten"]["dienst"] == "pm_heizung.clear_overlay"
    assert await (await app_client.get("/api/ereignisse?typ=heizperiode")).json() == []
    assert len(await (await app_client.get("/api/ereignisse")).json()) == 1
    assert (await app_client.get("/api/ereignisse?typ=DROP%20TABLE")).status == 400


async def test_info_contains_coach(app_client):
    info = await (await app_client.get("/api/info")).json()
    assert info["coach"] == {"ki_entitaet": "ai_task.claude_ai_task", "anwesenheit": True}
    assert info["version"] == __version__
