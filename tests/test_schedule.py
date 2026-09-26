"""Zeitplan-Transformation UI <-> HA, Validierung, Diff und Tagesaktionen."""

from __future__ import annotations

import pytest

from klimastudio import schedule as sch

# Exakt die Form, die schedule/list in der Live-Instanz liefert (gelesen am 25.09.2026).
LIVE_ITEM = {
    "id": "heizplan_badezimmer",
    "name": "Heizplan Badezimmer",
    "icon": "mdi:calendar-clock",
    "monday": [
        {"from": "05:00:00", "to": "07:00:00", "data": {"temperatur": 25}},
        {"from": "18:00:00", "to": "22:00:00", "data": {"temperatur": 22}},
    ],
    "tuesday": [{"from": "05:00:00", "to": "07:00:00", "data": {"temperatur": 25}}],
    "wednesday": [],
    "thursday": [],
    "friday": [],
    "saturday": [{"from": "18:00:00", "to": "24:00:00", "data": {"temperatur": 21.5, "modus": "komfort"}}],
    "sunday": [{"from": "18:00:00", "to": "22:00:00"}],
}


def test_parse_and_format_time():
    assert sch.parse_time("06:30:00") == 390
    assert sch.parse_time("06:30") == 390
    assert sch.parse_time("24:00:00") == 1440
    assert sch.format_time(1440) == "24:00:00"
    assert sch.format_time(390) == "06:30:00"
    for bad in ("25:00", "12:60", "x", "24:01:00", "1"):
        with pytest.raises(ValueError, match="Uhrzeit"):
            sch.parse_time(bad)


def test_ha_to_ui_keeps_extra_data_and_missing_temperature():
    ui = sch.ha_to_ui(LIVE_ITEM)
    assert ui["monday"][0] == {"start": 300, "end": 420, "temp": 25.0, "extra": {}}
    assert ui["saturday"][0] == {"start": 1080, "end": 1440, "temp": 21.5, "extra": {"modus": "komfort"}}
    assert ui["sunday"][0]["temp"] is None
    assert ui["wednesday"] == []


def test_roundtrip_is_lossless():
    back = sch.ui_to_ha_days(sch.ha_to_ui(LIVE_ITEM))
    for day in sch.DAYS:
        assert back[day] == LIVE_ITEM[day]
    # Ganzzahlige Temperaturen bleiben int (keine Scheinänderung 21 -> 21.0)
    assert isinstance(back["monday"][0]["data"]["temperatur"], int)
    assert back["saturday"][0]["data"]["temperatur"] == 21.5


def test_update_message_contains_name_icon_and_all_days():
    msg = sch.build_update_message(LIVE_ITEM, sch.ha_to_ui(LIVE_ITEM))
    assert msg["type"] == "schedule/update"
    assert msg["schedule_id"] == "heizplan_badezimmer"
    assert msg["name"] == "Heizplan Badezimmer"
    assert msg["icon"] == "mdi:calendar-clock"
    assert set(sch.DAYS) <= set(msg)
    assert "id" not in msg


def test_update_message_without_icon():
    item = {k: v for k, v in LIVE_ITEM.items() if k != "icon"}
    assert "icon" not in sch.build_update_message(item, sch.ha_to_ui(item))


def full(**days):
    return {d: days.get(d, []) for d in sch.DAYS}


def test_validate_ok_normalizes_and_sorts():
    days = full(monday=[{"start": 600, "end": 700, "temp": "21.5"}, {"start": 0, "end": 60, "temp": 18}])
    out = sch.validate_days(days)
    assert [b["start"] for b in out["monday"]] == [0, 600]
    assert out["monday"][1]["temp"] == 21.5
    assert out["sunday"] == []


def test_validate_requires_all_seven_days():
    with pytest.raises(sch.ScheduleError) as err:
        sch.validate_days({"monday": [{"start": 0, "end": 60, "temp": 20}]})
    assert any("Es fehlen Tage: Dienstag" in e for e in err.value.errors)


def test_validate_empty_plan_needs_confirmation():
    with pytest.raises(sch.EmptyScheduleError):
        sch.validate_days(full())
    out = sch.validate_days(full(), allow_empty=True)
    assert all(v == [] for v in out.values())


def test_seconds_are_preserved():
    item = dict(LIVE_ITEM, monday=[{"from": "06:00:30", "to": "08:15:45", "data": {"temperatur": 21}}])
    ui = sch.ha_to_ui(item)
    assert ui["monday"][0]["start_s"] == 30
    assert ui["monday"][0]["end_s"] == 45
    assert sch.ui_to_ha_days(ui)["monday"] == item["monday"]
    msg = sch.build_update_message(item, sch.validate_days(ui))
    assert msg["monday"][0]["from"] == "06:00:30"
    assert sch.parse_time_s("24:00:00") == (1440, 0)
    assert sch.format_time(390, 5) == "06:30:05"
    # Revision unterscheidet Sekunden
    assert sch.revision(item) != sch.revision(
        dict(item, monday=[{"from": "06:00:00", "to": "08:15:45", "data": {"temperatur": 21}}])
    )


def test_seconds_in_overlap_and_diff():
    ok = full(friday=[{"start": 0, "end": 60, "end_s": 30, "temp": 20}, {"start": 60, "start_s": 30, "end": 120, "temp": 20}])
    sch.validate_days(ok)
    bad = full(friday=[{"start": 0, "end": 60, "end_s": 30, "temp": 20}, {"start": 60, "start_s": 10, "end": 120, "temp": 20}])
    with pytest.raises(sch.ScheduleError, match="überlappen"):
        sch.validate_days(bad)
    with pytest.raises(sch.ScheduleError, match="Sekunden"):
        sch.validate_days(full(friday=[{"start": 0, "end": 1440, "end_s": 5, "temp": 20}]))
    with pytest.raises(sch.ScheduleError, match="Sekunden"):
        sch.validate_days(full(friday=[{"start": 0, "start_s": 60, "end": 10, "temp": 20}]))
    old = full(monday=[{"start": 360, "start_s": 30, "end": 420, "temp": 20.0, "extra": {}}])
    new = full(monday=[{"start": 360, "end": 420, "temp": 20.0, "extra": {}}])
    d = sch.diff_days(old, new)
    assert d[0]["changes"][0]["op"] == "change"
    assert "06:00:30" in d[0]["changes"][0]["text"]


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        ({"start": 600, "end": 600, "temp": 20}, "nicht vor"),
        ({"start": 700, "end": 600, "temp": 20}, "nicht vor"),
        ({"start": -10, "end": 600, "temp": 20}, "außerhalb"),
        ({"start": 0, "end": 1441, "temp": 20}, "außerhalb"),
        ({"start": 0, "end": 60, "temp": 4.5}, "außerhalb 5–25"),
        ({"start": 0, "end": 60, "temp": 25.5}, "außerhalb 5–25"),
        ({"start": 0, "end": 60, "temp": 20.3}, "0,5"),
        ({"start": 0, "end": 60, "temp": "warm"}, "ungültig"),
        ({"start": "a", "end": 60, "temp": 20}, "fehlen"),
        ({"start": 0, "end": 60, "temp": 20, "extra": {"x": [1]}}, "Zusatzdaten"),
    ],
)
def test_validate_rejects(block, fragment):
    with pytest.raises(sch.ScheduleError) as err:
        sch.validate_days({"monday": [block]})
    assert any(fragment in e for e in err.value.errors), err.value.errors


def test_validate_overlap_and_touching():
    with pytest.raises(sch.ScheduleError, match="überlappen"):
        sch.validate_days({"friday": [{"start": 0, "end": 120, "temp": 20}, {"start": 60, "end": 180, "temp": 20}]})
    # Aneinanderstoßende Blöcke sind erlaubt (HA: previous_to > from ist verboten)
    sch.validate_days(full(friday=[{"start": 0, "end": 120, "temp": 20}, {"start": 120, "end": 1440, "temp": 20}]))


def test_validate_unknown_day_and_type():
    with pytest.raises(sch.ScheduleError, match="Unbekannte Tage"):
        sch.validate_days({"montag": []})
    with pytest.raises(sch.ScheduleError):
        sch.validate_days([])


def test_diff_detects_add_remove_change():
    old = sch.ha_to_ui(LIVE_ITEM)
    new = sch.copy_day(old, "monday", [])
    new["monday"][1] = dict(new["monday"][1], temp=21.0)  # geändert
    new["tuesday"] = []  # entfernt
    new["wednesday"] = [{"start": 360, "end": 420, "temp": 20.0, "extra": {}}]  # neu
    diff = {d["day"]: d for d in sch.diff_days(old, new)}
    assert set(diff) == {"monday", "tuesday", "wednesday"}
    assert diff["monday"]["changes"][0]["op"] == "change"
    assert "22 °C" in diff["monday"]["changes"][0]["text"]
    assert "21 °C" in diff["monday"]["changes"][0]["text"]
    assert diff["tuesday"]["changes"][0]["op"] == "remove"
    assert diff["wednesday"]["changes"][0]["op"] == "add"
    assert diff["wednesday"]["label"] == "Mittwoch"


def test_diff_empty_when_equal():
    ui = sch.ha_to_ui(LIVE_ITEM)
    assert sch.diff_days(ui, sch.ha_to_ui(LIVE_ITEM)) == []


def test_copy_day_to_weekdays_is_deep():
    ui = sch.ha_to_ui(LIVE_ITEM)
    out = sch.copy_day(ui, "monday", sch.WEEKDAYS)
    for d in sch.WEEKDAYS:
        assert out[d] == ui["monday"]
    out["friday"][0]["temp"] = 5
    assert ui["monday"][0]["temp"] == 25.0
    assert out["saturday"] == ui["saturday"]
    with pytest.raises(ValueError, match="Unbekannter Tag"):
        sch.copy_day(ui, "monday", ["feiertag"])


def test_revision_changes_only_on_content():
    rev = sch.revision(LIVE_ITEM)
    same = dict(LIVE_ITEM, monday=[dict(r) for r in LIVE_ITEM["monday"]])
    assert sch.revision(same) == rev
    changed = dict(LIVE_ITEM, monday=[])
    assert sch.revision(changed) != rev
    renamed = dict(LIVE_ITEM, name="Anders")
    assert sch.revision(renamed) != rev
