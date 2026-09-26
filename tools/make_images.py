"""Erzeugt icon.png (128x128) und logo.png (250x100) im PM-Networks-Stil "Twilight Edition"."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "pm_klima_studio"
FONTS = APP / "app" / "klimastudio" / "static" / "fonts"
sys.path.insert(0, str(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools"))
from twilight_bg import twilight_bg  # noqa: E402

LAVENDER = (197, 192, 211, 255)
CLOUD = (236, 235, 242, 255)
AUTO = (184, 133, 214, 255)
HEAT = (232, 135, 90, 255)
HEIZT = (242, 179, 92, 255)


def thermometer(draw: ImageDraw.ImageDraw, x: float, y: float, s: float) -> None:
    """Thermometer mit Skalenstrichen (angelehnt an mdi:thermometer-lines), Höhe ~ s."""
    w = s * 0.22
    bulb = s * 0.2
    top, bottom = y, y + s * 0.72
    stroke = max(2, int(s * 0.045))
    draw.rounded_rectangle([x - w / 2, top, x + w / 2, bottom], radius=w / 2, outline=CLOUD, width=stroke)
    draw.ellipse([x - bulb, bottom - bulb * 0.4, x + bulb, bottom + bulb * 1.6], outline=CLOUD, width=stroke)
    inner = w * 0.28
    draw.rounded_rectangle([x - inner, top + s * 0.3, x + inner, bottom + bulb * 0.4], radius=inner, fill=HEAT)
    draw.ellipse([x - bulb * 0.6, bottom + bulb * 0.0, x + bulb * 0.6, bottom + bulb * 1.2], fill=HEAT)
    for i, col in enumerate((HEIZT, AUTO, LAVENDER)):
        ly = top + s * (0.08 + i * 0.16)
        draw.rounded_rectangle([x + w * 0.9, ly, x + w * 0.9 + s * (0.34 - i * 0.07), ly + stroke * 1.2], radius=stroke, fill=col)


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return m


def make_icon(path: Path) -> None:
    n = 512
    bg = twilight_bg(n, n).convert("RGBA")
    d = ImageDraw.Draw(bg)
    thermometer(d, n * 0.42, n * 0.14, n * 0.7)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(bg, (0, 0), rounded_mask((n, n), int(n * 0.22)))
    out.resize((128, 128), Image.LANCZOS).save(path, optimize=True)


def make_logo(path: Path) -> None:
    w, h = 1000, 400
    bg = twilight_bg(w, h).convert("RGBA")
    d = ImageDraw.Draw(bg)
    thermometer(d, 150, 70, 270)
    kicker = ImageFont.truetype(str(FONTS / "Montserrat-SemiBold.woff2"), 38)
    title = ImageFont.truetype(str(FONTS / "Montserrat-Bold.woff2"), 90)
    d.text((300, 120), "P M   N E T W O R K S", font=kicker, fill=AUTO)
    d.text((296, 178), "Klima Studio", font=title, fill=CLOUD)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(bg, (0, 0), rounded_mask((w, h), 60))
    out.resize((250, 100), Image.LANCZOS).save(path, optimize=True)


if __name__ == "__main__":
    make_icon(APP / "icon.png")
    make_logo(APP / "logo.png")
    print("icon.png und logo.png erstellt")
