"""Gemeinsame Fixtures: Fake-HA-Server, HA-Client und App-Client."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pm_klima_studio" / "app"))
sys.path.insert(0, str(Path(__file__).parent))

from fake_ha import TOKEN, FakeHA  # noqa: E402
from klimastudio.config import Options  # noqa: E402
from klimastudio.ha_client import HAClient  # noqa: E402
from klimastudio.server import KlimaStudio, create_app  # noqa: E402

WISSEN_TEST = Path(__file__).parent / "wissen_test.json"

LOCAL = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


@pytest.fixture
def fake() -> FakeHA:
    return FakeHA()


@pytest.fixture
async def fake_server(aiohttp_server, fake):
    return await aiohttp_server(fake.app)


@pytest.fixture
async def ha_client(fake_server):
    async with aiohttp.ClientSession() as session:
        client = HAClient(
            session,
            api_url=str(fake_server.make_url("/core/api")),
            ws_url=str(fake_server.make_url("/core/websocket")).replace("http", "ws", 1),
            token=TOKEN,
        )
        yield client
        await client.close()


@pytest.fixture
def options() -> Options:
    return Options.from_dict({"bericht_notify": ["notify.mobile_app_bewohner_a"]})


@pytest.fixture
async def studio(ha_client, options, tmp_path):
    ks = KlimaStudio(options, ha_client, data_dir=tmp_path, wissen_pfad=WISSEN_TEST)
    yield ks
    if ks.scheduler:
        await ks.scheduler.stop()
    await ks.heizperiode.stop()
    ks.store.close()


@pytest.fixture
async def app_client(aiohttp_client, studio):
    return await aiohttp_client(create_app(studio, networks=LOCAL))
