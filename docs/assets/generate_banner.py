#!/usr/bin/env python3
"""Bespoke README banners, one composition per project."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 2400, 640
CJK_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
CJK_R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_DIR = Path("/usr/share/matplotlib/mpl-data/fonts/ttf")
WHITE = (248, 250, 252)


def f_cjk(size, bold=True):
    return ImageFont.truetype(CJK_B if bold else CJK_R, size, index=2)


def f_sans(size):
    return ImageFont.truetype(str(FONT_DIR / "DejaVuSans-Bold.ttf"), size)


def f_mono(size, bold=False):
    return ImageFont.truetype(str(FONT_DIR / ("DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf")), size)


def use_cjk(text):
    return any(ord(ch) > 0x2E80 for ch in text)


CONFIGS = {
    "company": dict(
        kicker="COMPANY AI NETWORK  //  PRIVATE POC",
        lines=[("Enterprise", 96), ("Agent Network", 96)],
        subtitle="mTLS identity  ·  least privilege  ·  durable tasks  ·  auditable delivery",
        metrics=[("1036", "TESTS"), ("2", "CI GATES"), ("25.3", "MS P95"), ("7", "DAY CA TTL")],
        accent=(45, 212, 191), text_y=150, subtitle_y=420, metrics_y=492,
    ),
}


def base():
    t = np.linspace(0, 1, H)[:, None, None]
    img = np.repeat(np.array((5, 9, 17))[None, None, :] * (1 - t)
                    + np.array((11, 22, 38))[None, None, :] * t, W, axis=1)
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    img[((xs % 48) < 1) | ((ys % 48) < 1)] += 9.0
    return img


def vignette(canvas):
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    vx = (xs - W / 2) / (W / 2)
    vy = (ys - H / 2) / (H / 2)
    vig = 1.0 - 0.20 * np.clip(vx ** 2 + vy ** 2, 0, 1) ** 1.5
    arr = np.asarray(canvas.convert("RGB"), dtype=float) * vig[..., None]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def halo(img, cx, cy, accent, radius=600, strength=0.20):
    ys, xs = np.mgrid[0:H, 0:W].astype(float)
    d = np.hypot(xs - cx, ys - cy) / radius
    img += (np.clip(1 - d, 0, 1) ** 2 * strength)[..., None] * np.array(accent) * 0.6


def draw_text(canvas, cfg):
    d = ImageDraw.Draw(canvas)
    accent = cfg["accent"]
    left, y = 140, cfg["text_y"]
    d.rectangle((96, cfg["text_y"] - 6, 104, cfg["metrics_y"] + 46), fill=accent + (190,))
    d.text((left, y - 14), cfg["kicker"], font=f_mono(26), fill=accent + (255,))
    y += 34
    for i, (text, size) in enumerate(cfg["lines"]):
        f = f_cjk(size) if use_cjk(text) else f_sans(size)
        d.text((left - 4, y), text, font=f, fill=(WHITE if i == 0 else accent) + (255,))
        y += int(size * 1.14) + 4
    d.text((left, cfg["subtitle_y"]), cfg["subtitle"], font=f_cjk(27, bold=False), fill=(148, 163, 184, 255))
    x = left
    for value, label in cfg["metrics"]:
        d.text((x, cfg["metrics_y"]), value, font=f_mono(30, True), fill=WHITE + (255,))
        wv = d.textlength(value, font=f_mono(30, True))
        d.text((x + wv + 10, cfg["metrics_y"] + 10), label, font=f_mono(19), fill=(100, 116, 139, 255))
        x += wv + 10 + d.textlength(label, font=f_mono(19)) + 42


# ---------------------------------------------------------------- motifs

def motif_topology(ov, accent):
    d = ImageDraw.Draw(ov)
    cx, cy = 1840, 318
    hub = 60
    for r, a in ((150, 26), (215, 18), (280, 12)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent + (a,), width=1)
    angles = [-165, -138, -110, -82, -54, -26, 6, 38]
    pts = []
    for a in angles:
        px = cx + 215 * np.cos(np.radians(a))
        py = cy + 215 * np.sin(np.radians(a))
        pts.append((px, py))
        d.line([(cx, cy), (px, py)], fill=accent + (46,), width=1)

    dn = (cx + 300, cy + 150)
    for i in range(0, 40, 2):
        x0 = cx + (dn[0] - cx) * i / 40
        y0 = cy + (dn[1] - cy) * i / 40
        x1 = cx + (dn[0] - cx) * (i + 1) / 40
        y1 = cy + (dn[1] - cy) * (i + 1) / 40
        d.line([(x0, y0), (x1, y1)], fill=(248, 113, 113, 150), width=2)
    mx, my = cx + (dn[0] - cx) * 0.56, cy + (dn[1] - cy) * 0.56
    d.line([(mx - 7, my - 7), (mx + 7, my + 7)], fill=(248, 113, 113, 220), width=3)
    d.line([(mx - 7, my + 7), (mx + 7, my - 7)], fill=(248, 113, 113, 220), width=3)

    d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=accent + (26,), outline=accent + (210,), width=2)
    d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=WHITE + (240,))
    for a in angles[::2]:
        r0, r1 = hub + 6, hub + 16
        d.line([(cx + r0 * np.cos(np.radians(a)), cy + r0 * np.sin(np.radians(a))),
                (cx + r1 * np.cos(np.radians(a)), cy + r1 * np.sin(np.radians(a)))],
               fill=accent + (160,), width=2)
    d.text((cx, cy + 76), "ROUTER", font=f_mono(15, True), fill=accent + (170,), anchor="ma")
    for i, (px, py) in enumerate(pts):
        rr = 7 if i % 3 == 0 else 4.5
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=(WHITE if i % 3 == 0 else accent) + (230,))
    d.rectangle([dn[0] - 8, dn[1] - 8, dn[0] + 8, dn[1] + 8], outline=(248, 113, 113, 220), width=2)


def render(out):
    cfg = CONFIGS["company"]
    accent = cfg["accent"]
    img = base()
    halo(img, 1900, 320, accent, 620, 0.20)

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    motif_topology(ov, accent)

    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    canvas = Image.alpha_composite(canvas, ov.filter(ImageFilter.GaussianBlur(7)))
    canvas = Image.alpha_composite(canvas, ov.filter(ImageFilter.GaussianBlur(0.4)))
    canvas = vignette(canvas)
    draw_text(canvas, cfg)
    canvas.convert("RGB").save(out, optimize=True)
    print(f"wrote {out} ({Path(out).stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "banner.png")
