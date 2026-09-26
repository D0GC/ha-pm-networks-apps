"""Oberflächenprüfung mit Playwright (Chromium) gegen tools/dev_server.py.

PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python tools/ui_check.py --out <ordner>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = "http://127.0.0.1:8099/"


def check_no_external(page: Page, hosts: set[str]) -> None:
    page.on("request", lambda r: hosts.add(r.url.split("/")[2]))


def desktop(p, out: Path, results: dict) -> None:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="de-DE", timezone_id="Europe/Berlin")
    page = ctx.new_page()
    hosts: set[str] = set()
    errors: list[str] = []
    check_no_external(page, hosts)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(BASE + "#/uebersicht")
    page.wait_for_selector(".grid-tiles .card")
    expect(page.locator(".grid-tiles .card")).to_have_count(4)
    page.screenshot(path=out / "desktop_1_uebersicht.png", full_page=True)

    # Heizplan: Block per Maus aufziehen, verschieben, Temperatur ändern, Diff prüfen
    page.goto(BASE + "#/heizplan/wohnzimmer")
    page.wait_for_selector(".daycol .block")
    before = page.locator('.daycol[data-day="wednesday"] .block').count()
    col = page.locator('.daycol[data-day="wednesday"]').bounding_box()
    hour = col["height"] / 24
    x = col["x"] + col["width"] / 2
    page.mouse.move(x, col["y"] + hour * 10 + 2)
    page.mouse.down()
    page.mouse.move(x, col["y"] + hour * 12, steps=6)
    page.mouse.move(x, col["y"] + hour * 13 + 2, steps=6)
    page.mouse.up()
    expect(page.locator('.daycol[data-day="wednesday"] .block')).to_have_count(before + 1)
    results["maus_block_angelegt"] = True
    page.click("#t-plus")
    page.click("#t-plus")
    expect(page.locator("#btn-save")).to_be_enabled()
    page.fill("#f-to", "13:30")
    page.press("#f-to", "Tab")
    expect(page.locator('.daycol[data-day="wednesday"] .block.sel')).to_contain_text("10:00–13:30")
    page.fill("#f-to", "25:00")
    page.press("#f-to", "Tab")
    expect(page.locator("#f-err")).to_contain_text("hh:mm")
    page.fill("#f-to", "13:30")
    page.press("#f-to", "Tab")
    results["zeitfeld_24h"] = True
    # Verschieben eines Blocks (Montag 16:00 -> +1 h)
    blk = page.locator('.daycol[data-day="monday"] .block').nth(1)
    bb = blk.bounding_box()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.mouse.down()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2 + hour, steps=8)
    page.mouse.up()
    expect(page.locator('.daycol[data-day="monday"] .block').nth(1)).to_contain_text("17:00–23:30")
    results["maus_block_verschoben"] = True
    # Unteren Rand ziehen (verlängern bis 24:00 wird durch Tagesende begrenzt)
    page.screenshot(path=out / "desktop_2_heizplan.png", full_page=True)
    # Tag kopieren auf Wochenende
    page.click('.dayhead[data-day="monday"]')
    page.click('#modal-body [data-set="we"]')
    page.click("#modal-actions .primary")
    expect(page.locator('.daycol[data-day="saturday"] .block')).to_have_count(2)
    results["tag_kopiert"] = True
    page.click("#btn-save")
    page.wait_for_selector("#modal:not([hidden]) .diff-day")
    diff_text = page.inner_text("#modal-body")
    results["diff_tage"] = [line for line in diff_text.splitlines() if line in ("Montag", "Mittwoch", "Samstag", "Sonntag")]
    page.screenshot(path=out / "desktop_3_diff.png")
    page.click("#modal-actions .primary")
    expect(page.locator("#toast")).to_contain_text("Heizplan gespeichert")
    results["gespeichert"] = page.inner_text("#toast")
    expect(page.locator("#btn-save")).to_be_disabled()

    # Leerer Plan: Rückfrage erscheint, Abbrechen schreibt nichts
    page.goto(BASE + "#/heizplan/schlafzimmer")
    page.wait_for_selector(".daycol .block")
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        page.click(f'.dayhead[data-day="{d}"]')
        page.click("#modal-actions .danger")
    page.click("#btn-save")
    expect(page.locator("#modal-title")).to_have_text("Heizplan vollständig leeren?")
    results["leer_rueckfrage"] = True
    page.click("#modal-actions .ghost")
    page.click("#btn-discard")
    page.goto(BASE + "#/auswertung/wohnzimmer")
    page.wait_for_selector("#c-temp svg")
    page.click('#rangechips [data-b="7d"]')
    page.wait_for_selector("#c-co2 svg path")
    box = page.locator("#c-temp svg").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.5)
    expect(page.locator("#c-temp .tip")).to_be_visible()
    page.screenshot(path=out / "desktop_4_auswertung.png", full_page=True)

    page.goto(BASE + "#/berichte")
    page.wait_for_selector(".rep-view .intro")
    page.click("#r-new")
    expect(page.locator("#toast")).to_contain_text("Bericht erstellt")
    results["bericht_erstellt"] = page.inner_text("#toast")
    results["berichte_im_archiv"] = page.locator(".rep-item").count()
    page.screenshot(path=out / "desktop_5_berichte.png", full_page=True)
    results["externe_hosts_desktop"] = sorted(h for h in hosts if not h.startswith("127.0.0.1"))
    results["js_fehler_desktop"] = errors
    browser.close()


def mobile(p, out: Path, results: dict) -> None:
    browser = p.chromium.launch()
    device = p.devices["iPhone 13"]
    ctx = browser.new_context(
        **{k: v for k, v in device.items() if k != "default_browser_type"}, locale="de-DE", timezone_id="Europe/Berlin"
    )
    page = ctx.new_page()
    hosts: set[str] = set()
    errors: list[str] = []
    check_no_external(page, hosts)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "#/uebersicht")
    page.wait_for_selector(".grid-tiles .card")
    page.screenshot(path=out / "mobil_1_uebersicht.png", full_page=True)
    page.goto(BASE + "#/heizplan/badezimmer")
    page.wait_for_selector(".daycol .block")
    # Touch: freie Fläche antippen -> "Neuer Block"
    col = page.locator('.daycol[data-day="saturday"]').bounding_box()
    page.touchscreen.tap(col["x"] + col["width"] / 2, col["y"] + col["height"] * 10 / 24)
    page.wait_for_selector("#b-add")
    page.click("#t-minus")
    page.click("#b-add")
    expect(page.locator('.daycol[data-day="saturday"] .block')).to_have_count(2)
    results["touch_block_angelegt"] = True
    page.screenshot(path=out / "mobil_2_heizplan.png", full_page=False)
    # Scrollbreite prüfen
    results["mobil_horizontal_scroll"] = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.click("#b-close")
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(BASE + "#/auswertung/badezimmer")
    page.wait_for_selector("#c-hum svg")
    page.screenshot(path=out / "mobil_3_auswertung.png", full_page=True)
    page.goto(BASE + "#/berichte")
    page.wait_for_selector(".rep-view .intro")
    page.screenshot(path=out / "mobil_4_berichte.png", full_page=True)
    results["rueckfrage_bei_ungespeichert"] = dialogs
    results["externe_hosts_mobil"] = sorted(h for h in hosts if not h.startswith("127.0.0.1"))
    results["js_fehler_mobil"] = errors
    browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {}
    with sync_playwright() as pw:
        desktop(pw, out, res)
        mobile(pw, out, res)
    print(json.dumps(res, ensure_ascii=False, indent=1))
