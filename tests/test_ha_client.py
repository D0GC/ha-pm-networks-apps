"""REST-/WebSocket-Client gegen den lokalen Fake-Server."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest

from klimastudio import schedule as sch
from klimastudio.ha_client import HAClient, HAError


async def test_rest_states_and_config(ha_client):
    states = await ha_client.get_states()
    assert any(s["entity_id"] == "climate.pm_wohnzimmer" for s in states)
    cfg = await ha_client.get_config()
    assert cfg["time_zone"] == "Europe/Berlin"


async def test_rest_error(ha_client, fake):
    fake.fail_notify.add("kaputt")
    with pytest.raises(HAError) as err:
        await ha_client.call_service("notify", "kaputt", {"message": "x"})
    assert err.value.code == "400"


async def test_call_service_with_response_uses_websocket(ha_client, fake):
    resp = await ha_client.call_service(
        "weather", "get_forecasts", {"entity_id": "weather.dwd_zuhause", "type": "daily"}, return_response=True
    )
    assert resp["weather.dwd_zuhause"]["forecast"]
    msg = fake.ws_commands[-1]
    assert {k: v for k, v in msg.items() if k != "id"} == {
        "type": "call_service",
        "domain": "weather",
        "service": "get_forecasts",
        "service_data": {"entity_id": "weather.dwd_zuhause", "type": "daily"},
        "return_response": True,
    }
    fake.ki_fehler = "Rate limit exceeded"
    with pytest.raises(HAError) as err:
        await ha_client.call_service("ai_task", "generate_data", {"entity_id": "ai_task.x"}, return_response=True)
    assert err.value.meldung == "Rate limit exceeded"
    assert err.value.code == "home_assistant_error"


async def test_call_service_timeout_is_passed_through(ha_client, fake):
    fake.ki_delay = 0.5
    with pytest.raises(HAError) as err:
        await ha_client.call_service("ai_task", "generate_data", {"entity_id": "ai_task.x"}, return_response=True, timeout=0.1)
    assert err.value.code == "timeout"
    fake.ki_delay = 0
    assert await ha_client.call_service("ai_task", "generate_data", {"entity_id": "ai_task.x"}, return_response=True, timeout=5)


def test_ki_fehlertext():
    from klimastudio.coach.ki import fehlertext

    assert fehlertext(HAError("call_service: kaputt", "x", "kaputt")) == "Der KI-Dienst meldet: kaputt"
    assert "180 Sekunden" in fehlertext(HAError("call_service: Zeitüberschreitung", "timeout"))
    assert "HTTP" not in fehlertext(HAError("POST services/ai_task/generate_data: HTTP 500 boom", "500"))


async def test_ws_auth_and_schedule_list(ha_client):
    items = await ha_client.schedule_list()
    assert {i["id"] for i in items} >= {"heizplan_wohnzimmer", "heizplan_badezimmer"}
    assert ha_client.ha_version == "2026.9.3"


async def test_ws_auth_invalid(fake_server):
    async with aiohttp.ClientSession() as session:
        client = HAClient(
            session,
            api_url=str(fake_server.make_url("/core/api")),
            ws_url=str(fake_server.make_url("/core/websocket")).replace("http", "ws", 1),
            token="falsch",
        )
        with pytest.raises(HAError, match="abgelehnt"):
            await client.schedule_list()
        with pytest.raises(HAError):
            await client.get_states()


async def test_schedule_update_message_format(ha_client, fake):
    item = next(i for i in await ha_client.schedule_list() if i["id"] == "heizplan_kuche")
    days = sch.ha_to_ui(item)
    days["monday"][0]["temp"] = 21.5
    result = await ha_client.schedule_update(sch.build_update_message(item, days))
    sent = fake.updates[-1]
    assert sent["type"] == "schedule/update"
    assert sent["schedule_id"] == "heizplan_kuche"
    assert sent["name"] == "Heizplan Küche"
    assert sent["icon"] == "mdi:calendar-clock"
    assert sent["monday"][0] == {"from": "06:00:00", "to": "08:30:00", "data": {"temperatur": 21.5}}
    assert result["icon"] == "mdi:calendar-clock"
    assert result["tuesday"] == item["tuesday"]


async def test_schedule_update_rejected(ha_client):
    item = next(i for i in await ha_client.schedule_list() if i["id"] == "heizplan_kuche")
    msg = sch.build_update_message(item, sch.ha_to_ui(item))
    msg["monday"] = [{"from": "08:00:00", "to": "07:00:00"}]
    with pytest.raises(HAError) as err:
        await ha_client.schedule_update(msg)
    assert err.value.code == "invalid_format"
    with pytest.raises(ValueError, match="erwartet"):
        await ha_client.schedule_update({"type": "schedule/delete"})


async def test_history_and_statistics(ha_client, fake):
    end = fake.sim.now
    start = end - timedelta(hours=24)
    hist = await ha_client.history_during_period(
        ["sensor.badezimmer_luftfeuchtigkeit"], start, end, minimal_response=True, no_attributes=True
    )
    rows = hist["sensor.badezimmer_luftfeuchtigkeit"]
    assert rows[0]["lu"] == pytest.approx(start.timestamp())
    assert "a" not in rows[1]
    sent = fake.ws_commands[-1]
    assert sent["type"] == "history/history_during_period"
    assert sent["include_start_time_state"] is True
    stats = await ha_client.statistics_during_period(["sensor.badezimmer_luftfeuchtigkeit"], end - timedelta(days=20), end)
    assert len(stats["sensor.badezimmer_luftfeuchtigkeit"]) > 400
    assert fake.ws_commands[-1]["period"] == "hour"


async def test_reconnect_after_disconnect(ha_client):
    await ha_client.schedule_list()
    await ha_client._ws.close()
    items = await ha_client.schedule_list()
    assert items


async def test_unknown_command_error(ha_client):
    with pytest.raises(HAError) as err:
        await ha_client.ws_command({"type": "gibt/es_nicht"})
    assert err.value.code == "invalid_format"


async def test_unreachable():
    async with aiohttp.ClientSession() as session:
        client = HAClient(
            session, api_url="http://127.0.0.1:9/core/api", ws_url="ws://127.0.0.1:9/core/websocket", token="x", timeout=2
        )
        with pytest.raises(HAError, match="nicht erreichbar"):
            await client.schedule_list()
        with pytest.raises(HAError):
            await client.get_states()


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abc")
    client = HAClient(session=None)  # type: ignore[arg-type]
    assert client._headers["Authorization"] == "Bearer abc"
    assert client.api_url == "http://supervisor/core/api"
    assert client.ws_url == "ws://supervisor/core/websocket"


async def test_reconnect_does_not_fail_new_requests(ha_client):
    """Ein alter Leser, der beim Schließen endet, darf Anfragen der neuen Verbindung nicht abbrechen."""
    import asyncio

    await ha_client.schedule_list()
    old_ws = ha_client._ws
    ha_client._ws = None  # neue Verbindung erzwingen, alte bleibt zunächst offen
    slow = asyncio.create_task(ha_client.ws_command({"type": "test/slow", "delay": 0.3}))
    await asyncio.sleep(0.1)
    assert ha_client._ws is not old_ws
    await old_ws.close()  # alter Leser endet jetzt und räumt nur seine eigenen Anfragen ab
    await asyncio.sleep(0.05)
    assert await slow == "langsam"
    assert old_ws not in ha_client._pending


async def test_own_disconnect_is_retried_once_on_new_connection(ha_client):
    """Bricht die eigene Verbindung ab, wird die Anfrage genau einmal über eine neue Verbindung wiederholt."""
    import asyncio

    await ha_client.schedule_list()
    first = ha_client._ws
    task = asyncio.create_task(ha_client.ws_command({"type": "test/slow", "delay": 0.3}))
    await asyncio.sleep(0.1)
    await first.close()
    assert await task == "langsam"
    assert ha_client._ws is not first
    assert first not in ha_client._pending
