"""Wochenbericht: Register, Varianten, Empfehlungen, Archiv und Termin."""

from __future__ import annotations

import itertools
import random
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from klimastudio import report as rp

TZ = ZoneInfo("Europe/Berlin")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
DU_FORM = re.compile(r"\b(du|dein|deine|dir|dich)\b", re.IGNORECASE)


def analysis(raum, name, heiz=10.0, **kw):
    a = {"raum": raum, "name": name, "heizstunden": heiz}
    a.update(kw)
    return a


QUIET = [analysis("wohnzimmer", "Wohnzimmer", 12.0, feuchte={"mittel": 52, "max": 60, "stunden_70": 0, "stunden_80": 0})]
BUSY = [
    analysis(
        "wohnzimmer",
        "Wohnzimmer",
        35.0,
        feuchte={"mittel": 62, "max": 74, "stunden_70": 9.5, "stunden_80": 0},
        schimmel={"max": 83.2, "stunden_70": 20, "stunden_80": 2},
        co2={"max": 1620, "spitzen_1000": 6, "spitzen_1400": 3, "stunden_1000": 9},
        fenster={
            "anzahl": 14,
            "offen_stunden": 5.2,
            "laengste": {"start": 1_758_800_000_000, "ende": 1_758_805_700_000, "dauer_min": 95},
        },
        lueftung={"bewertung": "wirksam"},
    ),
    analysis(
        "kuche",
        "Küche",
        8.0,
        fenster={"anzahl": 7, "offen_stunden": 2.3, "laengste": {"start": 1_758_700_000_000, "ende": 1, "dauer_min": 20}},
    ),
    analysis(
        "badezimmer", "Badezimmer", 9.0, feuchte={"max": 88, "stunden_70": 7, "stunden_80": 3}, lueftung={"bewertung": "gering"}
    ),
]


def test_ten_variants_each_and_register():
    assert len(rp.EINLEITUNGEN) == 10
    assert len(set(rp.EINLEITUNGEN)) == 10
    assert len(rp.SCHLUSSSAETZE) == 10
    assert len(set(rp.SCHLUSSSAETZE)) == 10
    for text in (*rp.EINLEITUNGEN, *rp.SCHLUSSSAETZE):
        assert "!" not in text
        assert not EMOJI.search(text)
        assert not DU_FORM.search(text)
        assert text.endswith(".")


@pytest.mark.parametrize("data", [QUIET, BUSY, []])
def test_all_combinations_without_exclamation(data):
    end = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    seen_intro, seen_outro = set(), set()
    for seed in range(200):
        rep = rp.build_report(data, end - timedelta(days=7), end, TZ, rng=random.Random(seed))
        for field in ("text", "push_text", "einleitung", "schluss"):
            assert "!" not in rep[field]
            assert not EMOJI.search(rep[field])
            assert not DU_FORM.search(rep[field])
        assert 1 <= len(rep["empfehlungen"]) <= 3
        seen_intro.add(rep["einleitung"])
        seen_outro.add(rep["schluss"])
    assert seen_intro == set(rp.EINLEITUNGEN)
    assert seen_outro == set(rp.SCHLUSSSAETZE)


def test_busy_content():
    end = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    rep = rp.build_report(BUSY, end - timedelta(days=7), end, TZ, rng=random.Random(1))
    themen = {(f["raum"], f["thema"]) for f in rep["auffaellig"]}
    assert ("wohnzimmer", "schimmel") in themen
    assert ("wohnzimmer", "co2") in themen
    assert ("badezimmer", "feuchte") in themen
    assert rep["auffaellig"][0]["stufe"] == 3
    assert rep["fenster_laengste"][0]["raum"] == "wohnzimmer"
    assert len(rep["empfehlungen"]) == 3
    assert "Heizstunden:" in rep["text"]
    assert "Wohnzimmer: 35 h" in rep["text"]
    assert "Empfehlung:" in rep["push_text"]
    for rec in rep["empfehlungen"]:
        assert re.search(r"empfiehlt|wäre|würde|könnte|lohnen", rec), rec


def test_quiet_week_has_neutral_recommendation():
    end = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    rep = rp.build_report(QUIET, end - timedelta(days=7), end, TZ)
    assert rep["auffaellig"] == []
    assert rep["empfehlungen"] == ["Es empfiehlt sich, den bisherigen Heiz- und Lüftungsrhythmus beizubehalten."]
    assert "keine Räume" in rep["text"]


def test_long_window_recommendation_and_heating_outlier():
    rooms = [rp.room_summary(a) for a in BUSY]
    recs = rp.recommendations(rooms, [])
    assert any("1 Stunde 35 Minuten" in r for r in recs)
    assert rp._dauer(45) == "45 Minuten"
    assert rp._dauer(120) == "2 Stunden"
    assert any("Heizzeit im Raum Wohnzimmer" in r for r in recs)


def test_archive_keeps_last_twelve(tmp_path):
    arc = rp.ReportArchive(tmp_path / "berichte.json")
    for i in range(15):
        arc.add({"id": f"r{i}", "text": "x"})
    items = arc.load()
    assert len(items) == 12
    assert items[0]["id"] == "r14"
    assert arc.get("r2") is None
    arc.update({"id": "r14", "text": "neu"})
    assert arc.get("r14")["text"] == "neu"
    (tmp_path / "berichte.json").write_text("kaputt")
    assert arc.load() == []


def test_archive_contains_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "geheim-123")
    arc = rp.ReportArchive(tmp_path / "berichte.json")
    end = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    arc.add(rp.build_report(BUSY, end - timedelta(days=7), end, TZ))
    for f in tmp_path.iterdir():
        assert "geheim-123" not in f.read_text()


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 9, 25, 21, 0, tzinfo=TZ), datetime(2026, 9, 27, 18, 0, tzinfo=TZ)),
        (datetime(2026, 9, 27, 17, 59, tzinfo=TZ), datetime(2026, 9, 27, 18, 0, tzinfo=TZ)),
        (datetime(2026, 9, 27, 18, 0, tzinfo=TZ), datetime(2026, 10, 4, 18, 0, tzinfo=TZ)),
        # Zeitumstellung am 25.10.2026: 18:00 Wandzeit bleibt 18:00 (UTC-Versatz wechselt)
        (datetime(2026, 10, 20, 12, 0, tzinfo=TZ), datetime(2026, 10, 25, 18, 0, tzinfo=TZ)),
    ],
)
def test_next_run(now, expected):
    nxt = rp.next_run(now, 6, 18, 0)
    assert nxt == expected
    assert nxt.hour == 18


def test_next_run_dst_offset():
    nxt = rp.next_run(datetime(2026, 10, 20, 12, 0, tzinfo=TZ), 6, 18, 0)
    assert nxt.utcoffset() == timedelta(hours=1)


async def test_weekly_scheduler_fires(monkeypatch):
    calls = []

    async def job():
        calls.append(1)

    now = datetime.now(TZ) + timedelta(seconds=1)
    sched = rp.WeeklyScheduler(job, now.weekday(), now.hour, now.minute, TZ)
    monkeypatch.setattr(rp, "next_run", lambda *_: datetime.now(TZ) + timedelta(milliseconds=50))
    sched.start()
    import asyncio

    for _ in itertools.repeat(None, 40):
        if calls:
            break
        await asyncio.sleep(0.05)
    await sched.stop()
    assert calls == [1]
