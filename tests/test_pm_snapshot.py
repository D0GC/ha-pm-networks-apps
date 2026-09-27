"""Verhaltensgleichheit im Modus pm_networks: JSON-Antworten gegen den Stand 1.1.1.

Der Snapshot ``daten/pm_snapshot_1_1_1.json`` wurde VOR dem Umbau auf das Adapter-Paket
mit dem Stand 1.1.1 erzeugt (``PMKS_SNAPSHOT_SCHREIBEN=1 pytest tests/test_pm_snapshot.py``).
Die Uhr ist eingefroren (Fake-HA und App), zeitabhängige Felder ohne festen Bezug
(Millisekunden-Zeitstempel ``stand`` des Coachs) werden normalisiert.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import LOCAL, WISSEN_TEST
from fake_ha import FakeHA
from klimastudio.config import Options
from klimastudio.server import KlimaStudio, create_app

SNAPSHOT = Path(__file__).parent / "daten" / "pm_snapshot_1_1_1.json"
FEST = datetime(2026, 1, 14, 19, 17, 0, tzinfo=UTC)  # Mittwoch, 20:17 Uhr Ortszeit, Heizperiode
ENDPUNKTE = ("/api/steuerung", "/api/heizperiode", "/api/coach", "/api/coach/lagebericht")
INFO_SCHLUESSEL = ("version", "ha_version", "zeitzone", "raeume", "bericht", "coach")


class _Eingefroren(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FEST.astimezone(tz) if tz else FEST.replace(tzinfo=None)


@pytest.fixture
def uhr(monkeypatch):
    """datetime.now() in App und Fake-HA auf FEST setzen (auch in später geladenen Modulen)."""
    for name, mod in list(sys.modules.items()):
        if (name.startswith("klimastudio") or name == "fake_ha") and getattr(mod, "datetime", None) is datetime:
            monkeypatch.setattr(mod, "datetime", _Eingefroren)
    return FEST


def _normalisiere(pfad: str, daten):
    if pfad == "/api/coach":
        daten = dict(daten)
        daten["stand"] = "<stand>"
    return daten


def _szenario_freigabe(fake: FakeHA) -> None:
    """Freigabe vorhanden und aus, Sperre durch die Freigabe, Räume in Overlay/Hand/Aus/Boost."""
    fake.extra["input_boolean.pm_heizperiode"] = ("off", {"friendly_name": "PM Heizperiode"})
    fake.extra["sensor.pm_heizung_abwesenheitsphase"] = (
        "gesperrt",
        {
            "gesperrt": True,
            "sperre_grund": "freigabe_aus",
            "sperre_seit": "2026-01-14T08:00:00+00:00",
            "sperre_wirkung": "aus",
            "aktiv": True,
            "beschreibung": "Heizperiode aus",
        },
    )
    fake.climate["kuche"].update(overlay_bis="dauerhaft", temperature=22.5, grund="overlay", anzeige="Zeitweise 22.5 °C")
    fake.climate["badezimmer"].update(state="heat", grund="manuell", temperature=23)
    fake.climate["schlafzimmer"].update(state="off", grund="aus")
    fake.climate["wohnzimmer"].update(boost_bis="2026-01-14T19:47:00+00:00", temperature=25, grund="boost")


async def _antworten(aiohttp_server, aiohttp_client, tmp_path, wissen: Path | None, szenario: str) -> dict:
    fake = FakeHA(now=FEST)
    if szenario == "freigabe":
        _szenario_freigabe(fake)
    server = await aiohttp_server(fake.app)
    import aiohttp

    from fake_ha import TOKEN
    from klimastudio.ha_client import HAClient

    async with aiohttp.ClientSession() as session:
        client = HAClient(
            session,
            api_url=str(server.make_url("/core/api")),
            ws_url=str(server.make_url("/core/websocket")).replace("http", "ws", 1),
            token=TOKEN,
        )
        opts = Options.from_dict({"bericht_notify": ["notify.mobile_app_bewohner_a"]})
        ks = KlimaStudio(opts, client, data_dir=tmp_path, wissen_pfad=wissen)
        try:
            app = await aiohttp_client(create_app(ks, networks=LOCAL))
            out: dict = {}
            for pfad in ENDPUNKTE:
                resp = await app.get(pfad)
                assert resp.status == 200, pfad
                out[pfad] = _normalisiere(pfad, await resp.json())
            info = await (await app.get("/api/info")).json()
            out["/api/info"] = {k: info[k] for k in INFO_SCHLUESSEL}
            # Schreibende Wege: Steuerung (Overlay), Heizperiode (Freigabe schalten), KI-Prompt
            resp = await app.post("/api/steuerung/kuche", json={"aktion": "overlay", "temperatur": 21.5, "dauer": 60})
            out["POST /api/steuerung/kuche"] = {"status": resp.status, "antwort": await resp.json()}
            resp = await app.post("/api/steuerung/schlafzimmer", json={"aktion": "boost", "dauer": 30})
            out["POST /api/steuerung/schlafzimmer"] = {"status": resp.status, "antwort": await resp.json()}
            resp = await app.post("/api/heizperiode", json={"modus": "heizperiode"})
            out["POST /api/heizperiode"] = {"status": resp.status, "antwort": await resp.json()}
            resp = await app.post("/api/coach/ki", json={})
            assert resp.status == 200
            out["dienste"] = [list(c) for c in fake.service_calls if c[0] != "ai_task"]
            out["ki_prompt"] = next(c[2]["instructions"] for c in fake.service_calls if c[0] == "ai_task")
            return out
        finally:
            await ks.heizperiode.stop()
            ks.store.close()
            await client.close()


@pytest.mark.parametrize("variante", ["wissen_test", "wissen_produktiv", "freigabe"])
async def test_pm_antworten_wie_1_1_1(uhr, aiohttp_server, aiohttp_client, tmp_path, variante):
    wissen = WISSEN_TEST if variante == "wissen_test" else None
    szenario = "freigabe" if variante == "freigabe" else "standard"
    ist = await _antworten(aiohttp_server, aiohttp_client, tmp_path, wissen, szenario)
    ist = json.loads(json.dumps(ist, ensure_ascii=False))
    if os.environ.get("PMKS_SNAPSHOT_SCHREIBEN") == "1":
        alle = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {}
        alle[variante] = ist
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(alle, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("Snapshot geschrieben")
    soll = json.loads(SNAPSHOT.read_text(encoding="utf-8"))[variante]
    for pfad in soll:
        assert ist[pfad] == soll[pfad], pfad
    assert set(ist) == set(soll)
