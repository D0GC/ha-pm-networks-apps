"""HTTP-API, Ingress-Filter und Ende-zu-Ende-Abläufe gegen den Fake-Server."""

from __future__ import annotations

import ipaddress

import pytest

from klimastudio import __version__
from klimastudio import schedule as sch
from klimastudio.server import allowed_networks, create_app, ingress_base

# ------------------------------------------------------------------ Ingress


async def test_ingress_filter_blocks_foreign_ip(aiohttp_client, studio):
    client = await aiohttp_client(create_app(studio, networks=[ipaddress.ip_network("172.30.32.2/32")]))
    for path in ("/", "/api/info", "/static/app.js", "/api/berichte"):
        resp = await client.get(path)
        assert resp.status == 403, path
    resp = await client.post("/api/berichte", json={})
    assert resp.status == 403


async def test_ingress_filter_allows_listed_ip(app_client):
    resp = await app_client.get("/api/health")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


def test_allowed_networks_default(monkeypatch):
    monkeypatch.delenv("PMKS_ALLOWED_IPS", raising=False)
    nets = allowed_networks()
    assert nets == [ipaddress.ip_network("172.30.32.2/32")]
    assert ipaddress.ip_address("172.30.32.1") not in nets[0]
    monkeypatch.setenv("PMKS_ALLOWED_IPS", "172.30.32.2, 10.0.0.0/8")
    assert len(allowed_networks()) == 2


class _Req:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("/api/hassio_ingress/AbC-123_x", "/api/hassio_ingress/AbC-123_x/"),
        ("", "/"),
        ('/api/hassio_ingress/x"><script>', "/"),
        ("/etwas/anderes", "/"),
    ],
)
def test_ingress_base(header, expected):
    assert ingress_base(_Req({"X-Ingress-Path": header} if header else {})) == expected


async def test_index_uses_ingress_path_and_security_headers(app_client):
    resp = await app_client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/tok123"})
    html = await resp.text()
    assert resp.status == 200
    assert '<base href="/api/hassio_ingress/tok123/">' in html
    assert "__VERSION__" not in html
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "http://" not in html.replace('xmlns="http://www.w3.org', "")
    assert "https://" not in html


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("get", "/api/gibt_es_nicht", {}, 404),
        ("get", "/api/uebersicht?bereich=1y", {}, 400),
        ("post", "/api/heizperiode", {"json": {"modus": "winter"}}, 400),
        ("post", "/api/steuerung/unbekannt", {"json": {}}, 404),
        ("post", "/api/heizperiode/einrichten", {"json": {}}, 409),
    ],
)
async def test_security_headers_on_errors(app_client, fake, method, path, kwargs, status):
    fake.extra["input_boolean.pm_heizperiode"] = ("on", {})
    resp = await getattr(app_client, method)(path, **kwargs)
    assert resp.status == status
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert resp.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/api/coach/ki", "/api/heizperiode/einrichten"])
async def test_post_without_body_requires_json(app_client, fake, path):
    for kwargs in ({}, {"data": "x=1", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}):
        resp = await app_client.post(path, **kwargs)
        assert resp.status == 415, kwargs
    assert [c for c in fake.service_calls if c[0] in ("ai_task", "input_boolean")] == []
    assert [c for c in fake.ws_commands if c["type"] in ("input_boolean/create", "call_service")] == []


async def test_steuerung_survives_heizperiode_error(app_client, studio, monkeypatch):
    import sqlite3

    from klimastudio.ha_client import HAError

    for err in (sqlite3.OperationalError("database is locked"), HAError("recorder weg")):

        async def kaputt(*a, _err=err, **kw):
            raise _err

        monkeypatch.setattr(studio.heizperiode, "status", kaputt)
        resp = await app_client.get("/api/steuerung")
        d = await resp.json()
        assert resp.status == 200
        assert d["heizperiode"] is None
        assert d["raeume"]


async def test_static_assets_have_no_external_hosts(app_client):
    for path in ("/static/app.js", "/static/charts.js", "/static/app.css"):
        resp = await app_client.get(path)
        body = await resp.text()
        assert resp.status == 200
        assert "https://" not in body, path
        assert "http://" not in body.replace("http://www.w3.org/2000/svg", ""), path


async def test_index_references_versioned_assets(app_client):
    html = await (await app_client.get("/")).text()
    for name in ("app.css", "charts.js", "app.js", "steuerung.js", "coach.js"):
        assert f'"static/{__version__}/{name}"' in html, name
    assert "?v=" not in html


@pytest.mark.parametrize("version", [__version__, "1.0.1"])
@pytest.mark.parametrize("name", ["app.js", "steuerung.js", "coach.js", "charts.js", "app.css"])
async def test_versioned_assets_no_cache(app_client, version, name):
    resp = await app_client.get(f"/static/{version}/{name}")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    # Versionsfremde Pfade liefern die aktuelle Datei
    assert await resp.read() == await (await app_client.get(f"/static/{name}")).read()


async def test_versioned_fonts_and_legacy_paths(app_client):
    css = await (await app_client.get(f"/static/{__version__}/app.css")).text()
    assert 'url("fonts/' in css  # relativ zum versionierten Pfad
    resp = await app_client.get(f"/static/{__version__}/fonts/Montserrat-Regular.woff2")
    assert resp.status == 200
    legacy = await app_client.get("/static/app.js")
    assert legacy.status == 200
    assert legacy.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        f"/static/{__version__}/..%2f..%2fserver.py",
        f"/static/{__version__}/..%2f__init__.py",
        f"/static/{__version__}/%2e%2e/%2e%2e/server.py",
        f"/static/{__version__}/..%2f..%2f..%2f..%2f..%2f..%2fetc%2fpasswd",
        f"/static/{__version__}/%2fetc%2fpasswd",
        f"/static/{__version__}/img",
        f"/static/{__version__}/gibt_es_nicht.js",
    ],
)
async def test_versioned_path_no_traversal(app_client, path):
    from yarl import URL

    resp = await app_client.get(URL(path, encoded=True))
    assert resp.status in (403, 404), path
    body = await resp.text()
    assert "__version__" not in body
    assert "root:" not in body


# ------------------------------------------------------------------ API


async def test_info_and_current(app_client):
    info = await (await app_client.get("/api/info")).json()
    assert info["zeitzone"] == "Europe/Berlin"
    assert [r["raum"] for r in info["raeume"]] == ["badezimmer", "kuche", "schlafzimmer", "wohnzimmer"]
    assert info["bericht"]["notify"] == ["notify.mobile_app_bewohner_a"]
    cur = await (await app_client.get("/api/aktuell")).json()
    assert cur["raeume"]["wohnzimmer"]["modus"] == "auto"
    assert cur["empfehlungen"][0]["text"].startswith("Wohnzimmer")
    assert cur["aussen"]["temperatur"] is not None


@pytest.mark.parametrize("bereich", ["24h", "7d", "30d"])
async def test_analysis_ranges(app_client, bereich):
    resp = await app_client.get(f"/api/auswertung/wohnzimmer?bereich={bereich}")
    a = await resp.json()
    assert resp.status == 200, a
    assert a["heizstunden"] > 0
    assert len(a["soll_ist"]["ist"]) <= 302
    assert a["feuchte"]["verlauf"]
    assert a["co2"]["max"] > 1000
    assert a["fenster"]["anzahl"] >= 1
    assert a["lueftung"]["anzahl"] == a["fenster"]["anzahl"]
    if bereich == "30d":
        assert a["feuchte"]["quelle"] == "statistik"
        assert a["hinweise"]
    else:
        assert a["feuchte"]["quelle"] == "historie"


async def test_analysis_bad_range_and_room(app_client):
    assert (await app_client.get("/api/auswertung/wohnzimmer?bereich=1y")).status == 400
    assert (await app_client.get("/api/auswertung/keller")).status == 404


async def test_overview(app_client):
    ov = await (await app_client.get("/api/uebersicht?bereich=7d")).json()
    assert len(ov["raeume"]) == 4
    assert all("verlauf" not in (r.get("feuchte") or {}) for r in ov["raeume"])


# ------------------------------------------------------------------ Heizplan


async def test_schedule_get(app_client):
    d = await (await app_client.get("/api/heizplan/badezimmer")).json()
    assert d["id"] == "heizplan_badezimmer"
    assert d["tage"]["monday"][0] == {"start": 300, "end": 420, "temp": 25.0, "extra": {}}
    assert d["revision"]


async def test_schedule_preview_save_roundtrip(app_client, fake):
    d = await (await app_client.get("/api/heizplan/wohnzimmer")).json()
    tage = d["tage"]
    tage["monday"][0]["temp"] = 22.5
    tage["sunday"] = []
    prev = await (
        await app_client.post("/api/heizplan/wohnzimmer/vorschau", json={"tage": tage, "revision": d["revision"]})
    ).json()
    assert prev["konflikt"] is False
    assert [x["day"] for x in prev["diff"]] == ["monday", "sunday"]
    assert fake.updates == []  # Vorschau schreibt nichts
    resp = await app_client.post("/api/heizplan/wohnzimmer", json={"tage": tage, "revision": d["revision"]})
    res = await resp.json()
    assert resp.status == 200, res
    assert res["tage"]["monday"][0]["temp"] == 22.5
    stored = fake.schedules["heizplan_wohnzimmer"]
    assert stored["name"] == "Heizplan Wohnzimmer"
    assert stored["icon"] == "mdi:calendar-clock"
    assert stored["sunday"] == []
    assert stored["monday"][0]["data"] == {"temperatur": 22.5}
    assert stored["tuesday"][0]["data"] == {"temperatur": 21}
    # Vor dem Schreiben wurde erneut gelesen
    types = [c["type"] for c in fake.ws_commands]
    assert types[-2:] == ["schedule/list", "schedule/update"]


async def test_schedule_conflict_and_force(app_client, fake):
    d = await (await app_client.get("/api/heizplan/kuche")).json()
    fake.schedules["heizplan_kuche"]["monday"].insert(0, {"from": "05:00:00", "to": "06:00:00", "data": {"temperatur": 19}})
    prev = await (
        await app_client.post("/api/heizplan/kuche/vorschau", json={"tage": d["tage"], "revision": d["revision"]})
    ).json()
    assert prev["konflikt"] is True
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": d["tage"], "revision": d["revision"]})
    body = await resp.json()
    assert resp.status == 409
    assert body["konflikt"] is True
    assert body["diff"][0]["changes"][0]["op"] == "remove"
    assert fake.updates == []
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": d["tage"], "revision": body["revision_aktuell"]})
    assert resp.status == 200
    assert fake.schedules["heizplan_kuche"]["monday"][0]["from"] == "06:00:00"


async def test_schedule_validation_error(app_client, fake):
    d = await (await app_client.get("/api/heizplan/kuche")).json()
    tage = d["tage"]
    tage["friday"] = [{"start": 0, "end": 120, "temp": 20}, {"start": 60, "end": 180, "temp": 30}]
    del tage["sunday"]
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": tage, "revision": d["revision"]})
    body = await resp.json()
    assert resp.status == 422
    assert any("überlappen" in e for e in body["details"])
    assert any("25 °C" in e for e in body["details"])
    assert any("Es fehlen Tage: Sonntag" in e for e in body["details"])
    assert fake.updates == []


async def test_schedule_requires_json(app_client):
    resp = await app_client.post("/api/heizplan/kuche", data="tage=1")
    assert resp.status == 415


async def test_schedule_yaml_helper_not_editable(app_client, fake):
    del fake.schedules["heizplan_schlafzimmer"]
    resp = await app_client.get("/api/heizplan/schlafzimmer")
    assert resp.status == 409


async def test_copy_day_via_ui_format(app_client, fake):
    d = await (await app_client.get("/api/heizplan/badezimmer")).json()
    tage = sch.copy_day(d["tage"], "monday", sch.WEEKEND)
    resp = await app_client.post("/api/heizplan/badezimmer", json={"tage": tage, "revision": d["revision"]})
    assert resp.status == 200
    assert fake.schedules["heizplan_badezimmer"]["saturday"] == fake.schedules["heizplan_badezimmer"]["monday"]


# ------------------------------------------------------------------ Berichte


async def test_report_manual_without_push(app_client, fake, tmp_path):
    resp = await app_client.post("/api/berichte", json={})
    rep = await resp.json()
    assert resp.status == 200
    assert rep["push"] == []
    assert fake.service_calls == []
    assert "!" not in rep["text"]
    assert len(rep["raeume"]) == 4
    lst = await (await app_client.get("/api/berichte")).json()
    assert lst[0]["id"] == rep["id"]
    full = await (await app_client.get(f"/api/berichte/{rep['id']}")).json()
    assert full["text"] == rep["text"]
    assert (tmp_path / "berichte.json").exists()
    assert (await app_client.get("/api/berichte/unbekannt")).status == 404


async def test_report_manual_with_push(app_client, fake):
    rep = await (await app_client.post("/api/berichte", json={"push": True})).json()
    assert fake.service_calls[0][:2] == ("notify", "mobile_app_bewohner_a")
    assert fake.service_calls[0][2]["message"] == rep["push_text"]
    assert rep["push"] == [{"dienst": "notify.mobile_app_bewohner_a", "ok": True}]


async def test_scheduled_report_records_push_failure(studio, fake):
    fake.fail_notify.add("mobile_app_bewohner_a")
    rep = await studio.create_report(push=True, anlass="zeitplan")
    assert rep["push"][0]["ok"] is False
    assert studio.archive.load()[0]["anlass"] == "zeitplan"


async def test_scheduled_report_without_notify_targets(studio, fake):
    studio.opts.bericht_notify = []
    rep = await studio.create_report(push=True)
    assert rep["push"] == []
    assert fake.service_calls == []


async def test_start_reads_timezone_and_schedules(studio):
    await studio.start()
    assert str(studio.tz) == "Europe/Berlin"
    assert studio.scheduler is not None
    import asyncio

    await asyncio.sleep(0)
    assert studio.scheduler.next.weekday() == 6
    assert (studio.scheduler.next.hour, studio.scheduler.next.minute) == (18, 0)


async def test_empty_plan_requires_flag(app_client, fake):
    d = await (await app_client.get("/api/heizplan/kuche")).json()
    leer = {day: [] for day in sch.DAYS}
    for path in ("/api/heizplan/kuche/vorschau", "/api/heizplan/kuche"):
        resp = await app_client.post(path, json={"tage": leer, "revision": d["revision"]})
        body = await resp.json()
        assert resp.status == 422
        assert body["code"] == "leer"
    # Nur ausdrücklich True zählt
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": leer, "revision": d["revision"], "leer_bestaetigt": "ja"})
    assert resp.status == 422
    assert fake.updates == []
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": leer, "revision": d["revision"], "leer_bestaetigt": True})
    assert resp.status == 200
    stored = fake.schedules["heizplan_kuche"]
    assert all(stored[day] == [] for day in sch.DAYS)
    assert stored["name"] == "Heizplan Küche"


async def test_seconds_survive_save(app_client, fake):
    fake.schedules["heizplan_kuche"]["monday"][0]["from"] = "06:00:30"
    d = await (await app_client.get("/api/heizplan/kuche")).json()
    tage = d["tage"]
    tage["tuesday"][0]["temp"] = 20.0
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": tage, "revision": d["revision"]})
    assert resp.status == 200
    assert fake.updates[-1]["monday"][0]["from"] == "06:00:30"


async def test_registry_failure_aborts_without_guessing(app_client, fake):
    fake.fail_registry = True
    resp = await app_client.get("/api/heizplan/kuche")
    assert resp.status == 502
    assert "nicht lesbar" in (await resp.json())["fehler"]
    resp = await app_client.post("/api/heizplan/kuche", json={"tage": {d: [] for d in sch.DAYS}, "leer_bestaetigt": True})
    assert resp.status == 502
    assert fake.updates == []
    assert not [c for c in fake.ws_commands if c["type"] == "schedule/list"]


async def test_registry_non_schedule_platform(app_client, fake, studio):
    orig = fake.handle

    def handle(msg):
        if msg["type"] == "config/entity_registry/get":
            return {"entity_id": msg["entity_id"], "platform": "template", "unique_id": "x"}
        return orig(msg)

    fake.handle = handle
    resp = await app_client.get("/api/heizplan/kuche")
    assert resp.status == 409
    assert studio._registry_ids == {}


async def test_registry_cache_bounded_and_cleared(studio):
    from klimastudio import server as srv

    for i in range(srv.REGISTRY_CACHE_MAX + 5):
        await studio._schedule_storage_id(f"schedule.heizplan_x{i}")
    assert len(studio._registry_ids) == srv.REGISTRY_CACHE_MAX
    await studio.rooms(refresh=True)
    assert studio._registry_ids == {}


async def test_timezone_retry_in_background(studio, fake, monkeypatch):
    from zoneinfo import ZoneInfo

    from klimastudio import server as srv

    monkeypatch.setattr(srv, "TZ_RETRY", (0.05,))
    fake.fail_config = 2
    studio.tz = ZoneInfo("UTC")
    await studio.start()
    assert studio._tz_loaded is False
    import asyncio

    for _ in range(60):
        if studio._tz_loaded:
            break
        await asyncio.sleep(0.05)
    assert studio._tz_loaded is True
    assert str(studio.tz) == "Europe/Berlin"
    await asyncio.sleep(0.05)
    assert str(studio.scheduler.tz) == "Europe/Berlin"
    assert studio.scheduler.next.utcoffset() is not None
    assert str(studio.scheduler.next.tzinfo) == "Europe/Berlin"
    await studio.stop()


def test_history_cache_default_size():
    from klimastudio.data import HistoryCache

    assert HistoryCache().max_entries == 800
