"""Generate the application icon (app.ico + PNGs).

Design goals, in priority order:
  1. Legible at 16x16 — that is the size users actually see most (taskbar,
     tray, title bar). Everything else follows from that: one bold shape, one
     accent colour, no fine detail, no text.
  2. Reads as "sync" instantly — a circular arrow is the universal idiom.
  3. Sits beside DaVinci Resolve without clashing: Resolve's own blue (#00A0FF)
     on its own dark grey (#1F1F24), not a generic SaaS gradient.

Run:  python make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "static" / "icon"
BG = (31, 31, 36, 255)          # Resolve panel grey
BG_EDGE = (12, 12, 14, 255)     # near-black rim, matching Resolve's recessed look
BLUE = (0, 160, 255, 255)       # Resolve accent
BLUE_DIM = (0, 116, 190, 255)


def _rounded_rect(d: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _arrow_head(d: ImageDraw.ImageDraw, cx, cy, tangent_deg, half_width, length, fill):
    """Triangle whose base sits across the stroke and whose tip follows the tangent.

    Built from the tangent direction rather than a fixed angle so the head always
    continues the arc smoothly instead of jutting out of it.
    """
    t = math.radians(tangent_deg)
    nx, ny = -math.sin(t), math.cos(t)          # normal to the tangent
    tip = (cx + length * math.cos(t), cy + length * math.sin(t))
    a = (cx + half_width * nx, cy + half_width * ny)
    b = (cx - half_width * nx, cy - half_width * ny)
    d.polygon([tip, a, b], fill=fill)


def render(size: int) -> Image.Image:
    # Supersample so the arc and arrowhead stay clean when downscaled.
    s = size * 8
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = s * 0.04
    _rounded_rect(d, (pad, pad, s - pad, s - pad), radius=s * 0.20,
                  fill=BG, outline=BG_EDGE, width=max(1, int(s * 0.02)))

    # Sync arc: an open circle with one clean gap, ending in a tangential head.
    m = s * 0.25                       # margin to the arc's bounding box
    box = (m, m, s - m, s - m)
    stroke = max(2, int(s * 0.10))
    r = (s - 2 * m) / 2
    cx = cy = s / 2

    # Sweep clockwise from just past the head round to the gap. Leaving ONE gap
    # (rather than two) keeps the silhouette unambiguous at 16px.
    start_deg, end_deg = 20, 300
    d.arc(box, start=start_deg, end=end_deg, fill=BLUE, width=stroke)

    # Head continues the arc at its start, pointing along the tangent.
    a = math.radians(start_deg)
    hx, hy = cx + r * math.cos(a), cy + r * math.sin(a)
    _arrow_head(d, hx, hy, tangent_deg=start_deg + 90,
                half_width=stroke * 0.95, length=stroke * 1.5, fill=BLUE)

    # A solid centre gives the glyph weight and stops it reading as a plain "C".
    dot = s * 0.10
    d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=BLUE_DIM)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render(n) for n in sizes]

    ico = OUT.parent.parent / "app.ico"
    images[-1].save(ico, format="ICO",
                    sizes=[(n, n) for n in sizes])
    for n, im in zip(sizes, images):
        im.save(OUT / f"icon-{n}.png")
    images[-1].save(OUT / "icon.png")
    print(f"wrote {ico}  ({ico.stat().st_size} bytes)")
    print(f"wrote {len(sizes)} PNGs to {OUT}")


if __name__ == "__main__":
    main()
