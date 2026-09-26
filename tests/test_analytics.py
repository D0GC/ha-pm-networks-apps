"""Auswertungen mit synthetischen Historien."""

from __future__ import annotations

import pytest

from klimastudio import analytics as an

H = 3600


def test_parse_compressed_ws_rows_inherit_attributes():
    rows = [
        {"s": "auto", "a": {"hvac_action": "heating", "temperature": 21}, "lu": 1000.0},
        {"s": "auto", "lu": 1600.0},
        {"s": "unavailable", "a": {}, "lu": 2000.0},
    ]
    recs = an.parse_history_rows(rows)
    assert recs[1][2]["hvac_action"] == "heating"
    series = an.attr_series(recs, "hvac_action", numeric=False)
    assert series == [(1000.0, "heating"), (2000.0, None)]


def test_parse_rest_rows():
    rows = [
        {"entity_id": "sensor.x", "state": "50.5", "last_updated": "2026-09-25T10:00:00+00:00", "attributes": {}},
        {"entity_id": "sensor.x", "state": "unknown", "last_changed": "2026-09-25T11:00:00+00:00"},
    ]
    s = an.numeric_series(an.parse_history_rows(rows))
    assert s[0][1] == 50.5
    assert s[1][1] is None
    assert s[1][0] - s[0][0] == H


def test_heating_seconds_clips_to_range():
    series = [(0, "idle"), (1 * H, "heating"), (3 * H, "idle"), (5 * H, "heating")]
    assert an.heating_seconds(series, 0, 6 * H) == 3 * H
    assert an.heating_seconds(series, 2 * H, 5.5 * H) == 1.5 * H
    assert an.heating_seconds([], 0, H) == 0


def test_heating_ignores_unavailable():
    series = [(0, "heating"), (H, None), (2 * H, "heating")]
    assert an.heating_seconds(series, 0, 3 * H) == 2 * H


def test_window_intervals_and_merge():
    series = [(0, "off"), (100, "on"), (200, "on"), (400, "off"), (1000, "on")]
    series = an.dedupe(series)
    assert an.window_open_intervals(series, 0, 1200) == [(100, 400), (1000, 1200)]


def test_hours_above_threshold():
    series = [(0, 60.0), (H, 72.0), (3 * H, 81.0), (4 * H, 65.0)]
    assert an.hours_above(series, 70, 0, 5 * H) == pytest.approx(3)
    assert an.hours_above(series, 80, 0, 5 * H) == pytest.approx(1)


def test_co2_peaks():
    series = [(0, 600.0), (H, 1100.0), (2 * H, 1500.0), (3 * H, 900.0), (5 * H, 1200.0), (6 * H, 800.0)]
    res = an.co2_peaks(series, 0, 8 * H)
    assert res["max"] == 1500.0
    assert res["spitzen_1000"] == 2
    assert res["spitzen_1400"] == 1
    assert res["stunden_1000"] == pytest.approx(3)
    assert res["episoden"][0]["max"] == 1500.0


def test_ventilation_success():
    t0 = 10 * H
    hum = [(0, 68.0), (t0 + 300, 64.0), (t0 + 900, 59.0), (t0 + 2400, 55.0)]
    co2 = [(0, 1400.0), (t0 + 600, 900.0), (t0 + 1700, 600.0)]
    res = an.ventilation_success([(t0, t0 + 900)], hum, co2)
    ev = res["ereignisse"][0]
    assert ev["feuchte_start"] == 68.0
    assert ev["feuchte_abfall"] == 9.0  # 55 liegt außerhalb der 30 min
    assert ev["co2_abfall"] == 800.0
    assert res["bewertung"] == "wirksam"
    assert an.ventilation_success([], hum, co2)["bewertung"] == "keine Daten"


def test_ventilation_weak():
    res = an.ventilation_success([(0, 600)], [(0, 60.0), (300, 59.5)], None)
    assert res["bewertung"] == "gering"


def test_downsample_limits_points_and_keeps_edges():
    series = [(i * 60, float(i % 50)) for i in range(5000)]
    pts = an.downsample(series, 0, 5000 * 60, 200)
    assert len(pts) <= 201
    assert pts[-1][0] == 5000 * 60 * 1000
    small = an.downsample([(0, 1.0), (100, 2.0)], 0, 200, 300)
    assert small[0] == [0, 1.0]
    assert small[-1] == [200000, 2.0]


def test_stats_to_series_ms():
    rows = [{"start": 1_700_000_000_000.0, "mean": 50.0}, {"start": 1_700_003_600_000.0, "mean": None}]
    s = an.stats_to_series(rows)
    assert s[0] == (1_700_000_000.0, 50.0)
    assert s[1][1] is None


def test_value_at_and_merge():
    s = an.merge_series([[(0, 1), (10, 2)], [(10, 2), (20, 3)]])
    assert s == [(0, 1), (10, 2), (20, 3)]
    assert an.value_at(s, 15) == 2
    assert an.value_at(s, -1) is None
