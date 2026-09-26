import numpy as np
from PIL import Image

# Twilight corner colors (from original)
TL = np.array([15, 12, 42], float)  # #0F0C2A midnight indigo
TR = np.array([68, 49, 113], float)  # #443171 royal violet
BL = np.array([38, 34, 82], float)  # #262252 deep navy
BR = np.array([120, 66, 149], float)  # #784295 twilight purple


def twilight_bg(W, H, glow=True):
    u = np.linspace(0, 1, W)[None, :, None]  # x 0..1
    v = np.linspace(0, 1, H)[:, None, None]  # y 0..1
    top = TL * (1 - u) + TR * u
    bot = BL * (1 - u) + BR * u
    img = top * (1 - v) + bot * v  # bilinear
    if glow:
        # soft radial highlight toward bottom (echoes original lighter purple glow)
        cx, cy = 0.62, 1.02
        gx = np.linspace(0, 1, W)[None, :]
        gy = np.linspace(0, 1, H)[:, None]
        d = np.sqrt(((gx - cx) * 1.15) ** 2 + (gy - cy) ** 2)
        g = np.clip(1 - d / 0.85, 0, 1) ** 1.6
        img = img + (g[:, :, None] * np.array([40, 24, 55]))
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


if __name__ == "__main__":
    twilight_bg(2560, 1440).save("brand/_grad_test.png")
    print("ok")
