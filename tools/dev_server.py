"""Lokale Vorführung: Fake-HA (Port 8123) und Klima Studio (Port 8099) in einem Prozess.

    python tools/dev_server.py [--data /tmp/pmks]

Nur für Entwicklung und Oberflächentests; erlaubt Zugriffe von 127.0.0.1.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pm_klima_studio" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_ha import TOKEN, FakeHA  # noqa: E402
from klimastudio.config import Options  # noqa: E402
from klimastudio.ha_client import HAClient  # noqa: E402
from klimastudio.server import KlimaStudio, create_app  # noqa: E402


async def main(data_dir: Path, ha_port: int, port: int, reports: int) -> None:
    fake = FakeHA()
    ha_runner = web.AppRunner(fake.app)
    await ha_runner.setup()
    await web.TCPSite(ha_runner, "127.0.0.1", ha_port).start()
    async with aiohttp.ClientSession() as session:
        client = HAClient(session, f"http://127.0.0.1:{ha_port}/core/api", f"ws://127.0.0.1:{ha_port}/core/websocket", TOKEN)
        opts = Options.from_dict({"bericht_notify": ["mobile_app_bewohner_a"]})
        ks = KlimaStudio(opts, client, data_dir=data_dir)
        await ks.start()
        for _ in range(reports):
            await ks.create_report(push=False, anlass="zeitplan")
        app = create_app(ks, networks=[ipaddress.ip_network("127.0.0.0/8")])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        logging.info("Klima Studio: http://127.0.0.1:%s/", port)
        await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="/tmp/pmks-dev")  # noqa: S108
    p.add_argument("--ha-port", type=int, default=8123)
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--berichte", type=int, default=2)
    a = p.parse_args()
    Path(a.data).mkdir(parents=True, exist_ok=True)
    asyncio.run(main(Path(a.data), a.ha_port, a.port, a.berichte))
