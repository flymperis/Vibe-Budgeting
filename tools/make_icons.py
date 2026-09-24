"""Generate PWA icon PNGs from static/favicon.svg using Pillow.

Pillow can't rasterize SVG directly, so this script re-implements the tiny
subset of the SVG path grammar the favicon actually uses (M/L/H/V/C/Z,
absolute coordinates only) and redraws the same shapes with Pillow at a high
supersampled resolution, then downsamples for smooth (anti-aliased) edges.

Run with the system Python (Pillow lives there, not necessarily in .venv):
    python tools/make_icons.py

Outputs (all under static/icons/):
    apple-touch-icon.png   180x180, opaque (iOS paints transparent -> black)
    icon-192.png           192x192
    icon-512.png           512x512
    icon-maskable-512.png  512x512, mark confined to the central 80% safe zone
"""

import os
import re

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAVICON_SVG = os.path.join(ROOT, "static", "favicon.svg")
OUT_DIR = os.path.join(ROOT, "static", "icons")

# Colors sampled straight from static/favicon.svg (which mirrors the in-app
# logo: warm charcoal background, mint "VB" mark, green stonks line).
BG_GRADIENT_START = (0x26, 0x26, 0x26)  # #262626 (--bg, dark theme)
BG_GRADIENT_END = (0x2B, 0x2A, 0x2A)  # #2b2a2a (--surface, dark theme)
GLYPH_MINT = (0x6D, 0xD0, 0xAF)  # #6dd0af (--accent, dark theme)
STONK_GREEN = (0x4A, 0xDE, 0x80)  # #4ade80 (dark-theme .app-logo-stonks stroke)

SUPERSAMPLE = 8
VIEWBOX = 32  # favicon.svg viewBox is "0 0 32 32"


def _flatten_path(d, bezier_steps=48):
    """Flatten an SVG path 'd' string (M/L/H/V/C/Z, absolute coords only,
    the only commands static/favicon.svg uses) into a list of subpaths, each
    a list of (x, y) point tuples."""
    tokens = re.findall(r"([MLHVCZ])([^MLHVCZ]*)", d)
    subpaths = []
    current = []
    cx = cy = 0.0

    def nums(s):
        return [float(n) for n in re.findall(r"-?\d+\.?\d*", s)]

    for cmd, arg_str in tokens:
        args = nums(arg_str)
        if cmd == "M":
            if current:
                subpaths.append(current)
            x, y = args[0], args[1]
            current = [(x, y)]
            cx, cy = x, y
        elif cmd == "L":
            x, y = args[0], args[1]
            current.append((x, y))
            cx, cy = x, y
        elif cmd == "H":
            x = args[0]
            current.append((x, cy))
            cx = x
        elif cmd == "V":
            y = args[0]
            current.append((cx, y))
            cy = y
        elif cmd == "C":
            x1, y1, x2, y2, x, y = args
            for i in range(1, bezier_steps + 1):
                t = i / bezier_steps
                mt = 1 - t
                px = (
                    mt**3 * cx
                    + 3 * mt**2 * t * x1
                    + 3 * mt * t**2 * x2
                    + t**3 * x
                )
                py = (
                    mt**3 * cy
                    + 3 * mt**2 * t * y1
                    + 3 * mt * t**2 * y2
                    + t**3 * y
                )
                current.append((px, py))
            cx, cy = x, y
        elif cmd == "Z":
            if current:
                subpaths.append(current)
            current = []
    if current:
        subpaths.append(current)
    return subpaths


# The two glyph paths sit inside <g transform="translate(3.6 8.2) scale(0.56)">.
_V_PATH = "M 5.5 8 L 10 22 L 12 22 L 16.5 8 H 13.9 L 11 18.4 L 8.1 8 Z"
_B_PATH = (
    "M 17 22 L 17 8 L 20.15 8 C 25.6 8 28 10.2 28 12.85 C 28 14.95 25.3 16.05 21.2 16.05 "
    "L 20.15 16.05 L 20.15 16.65 C 26.2 16.85 28 18.95 28 20.85 C 28 22.55 24.85 22 20.15 22 L 17 22 Z "
    "M 21.25 10.6 C 23.85 10.15 25.9 11.35 25.9 13.05 C 25.9 14.55 23.95 15.35 21.45 14.95 "
    "C 19.35 14.6 19.15 11.35 21.25 10.6 Z "
    "M 21.35 17.75 C 24.25 17.25 26.15 18.45 26.15 20.35 C 26.15 21.95 23.65 21.75 21.35 21.35 "
    "C 19.25 20.95 19.15 18.45 21.35 17.75 Z"
)
_STONK_POINTS = [
    (17.5, 23.5),
    (20.5, 19.5),
    (22.5, 21.5),
    (25.5, 14.5),
    (27.5, 16.5),
    (29.5, 10),
    (31, 7.5),
]

_GROUP_TRANSLATE = (3.6, 8.2)
_GROUP_SCALE = 0.56


def _apply_group_transform(points):
    tx, ty = _GROUP_TRANSLATE
    return [(tx + x * _GROUP_SCALE, ty + y * _GROUP_SCALE) for x, y in points]


def _diagonal_gradient(px_size, start, end):
    """Linear gradient from `start` (top-left) to `end` (bottom-right),
    matching the SVG's <linearGradient x1="0" y1="0" x2="1" y2="1">."""
    ramp = np.linspace(0, 1, px_size * 2)
    grid_x, grid_y = np.meshgrid(np.arange(px_size), np.arange(px_size))
    t = (grid_x.astype(np.float64) + grid_y.astype(np.float64)) / (2 * (px_size - 1))
    t = np.clip(t, 0, 1)
    start_arr = np.array(start, dtype=np.float64)
    end_arr = np.array(end, dtype=np.float64)
    rgb = start_arr[None, None, :] * (1 - t[:, :, None]) + end_arr[None, None, :] * t[:, :, None]
    return rgb.astype(np.uint8)


def _rounded_rect_mask(px_size, radius):
    mask = Image.new("L", (px_size, px_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, px_size - 1, px_size - 1], radius=radius, fill=255)
    return mask


def _scale_points(points, factor):
    return [(x * factor, y * factor) for x, y in points]


def _render(size, maskable=False, opaque=True):
    """Render the mark at `size`x`size` px (after downsampling from a
    supersampled canvas). If `maskable`, the mark is shrunk into the central
    80% safe zone and the background fully covers the canvas (no rounding),
    per the maskable icon spec. If `opaque`, corners are filled with the
    gradient instead of left transparent (needed for apple-touch-icon)."""
    px = size * SUPERSAMPLE
    scale = px / VIEWBOX

    gradient = _diagonal_gradient(px, BG_GRADIENT_START, BG_GRADIENT_END)
    bg_rgb = Image.fromarray(gradient)

    if maskable or opaque:
        # Full-bleed square background: safe for both "no transparency"
        # (apple-touch-icon) and "OS may crop to any shape" (maskable).
        canvas = bg_rgb.convert("RGBA")
    else:
        radius = int(round(8 / VIEWBOX * px))
        mask = _rounded_rect_mask(px, radius)
        canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        canvas.paste(bg_rgb, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    # Maskable icons must keep all meaningful content inside the central 80%
    # "safe zone" since the OS may crop a mask (circle, squircle, ...) over
    # the full square.
    mark_scale = 0.8 if maskable else 1.0
    mark_offset = (px * (1 - mark_scale) / 2, px * (1 - mark_scale) / 2)

    def to_canvas(points):
        pts = _scale_points(points, scale * mark_scale)
        ox, oy = mark_offset
        return [(x + ox, y + oy) for x, y in pts]

    # "V" glyph.
    v_points = to_canvas(_apply_group_transform(_flatten_path(_V_PATH)[0]))
    draw.polygon(v_points, fill=GLYPH_MINT)

    # "B" glyph: outer bowl filled mint, two inner counters cut back to the
    # background color (mirrors the SVG's fill-rule="evenodd").
    b_subpaths = [_apply_group_transform(sp) for sp in _flatten_path(_B_PATH)]
    outer, hole1, hole2 = b_subpaths
    draw.polygon(to_canvas(outer), fill=GLYPH_MINT)
    # Sample the background color under each hole's centroid so the cutout
    # blends with the gradient instead of using a flat average.
    for hole in (hole1, hole2):
        pts = to_canvas(hole)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        color = bg_rgb.getpixel((min(int(cx), px - 1), min(int(cy), px - 1)))
        draw.polygon(pts, fill=(*color, 255))

    # Stonk arrow (skip on maskable/small icons where it would be an
    # illegible smear; keep it on the regular square icons for brand fidelity).
    if not maskable:
        stonk_points = to_canvas(_STONK_POINTS)
        width = max(1, int(round(2.15 * scale)))
        draw.line(stonk_points, fill=STONK_GREEN, width=width, joint="curve")
        _draw_arrowhead(draw, stonk_points[-2], stonk_points[-1], width * 2.5, STONK_GREEN)

    resized = canvas.resize((size, size), Image.LANCZOS)
    if opaque and resized.mode == "RGBA":
        flat = Image.new("RGB", resized.size, BG_GRADIENT_END)
        flat.paste(resized, (0, 0), resized)
        return flat
    return resized


def _draw_arrowhead(draw, tail, tip, length, color):
    import math

    dx, dy = tip[0] - tail[0], tip[1] - tail[1]
    dist = math.hypot(dx, dy) or 1
    ux, uy = dx / dist, dy / dist
    # Perpendicular
    px, py = -uy, ux
    base = (tip[0] - ux * length, tip[1] - uy * length)
    left = (base[0] + px * length * 0.5, base[1] + py * length * 0.5)
    right = (base[0] - px * length * 0.5, base[1] - py * length * 0.5)
    draw.polygon([tip, left, right], fill=color)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    apple = _render(180, maskable=False, opaque=True)
    apple.save(os.path.join(OUT_DIR, "apple-touch-icon.png"))

    icon192 = _render(192, maskable=False, opaque=False)
    icon192.save(os.path.join(OUT_DIR, "icon-192.png"))

    icon512 = _render(512, maskable=False, opaque=False)
    icon512.save(os.path.join(OUT_DIR, "icon-512.png"))

    maskable512 = _render(512, maskable=True, opaque=True)
    maskable512.save(os.path.join(OUT_DIR, "icon-maskable-512.png"))

    print(f"Wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    assert os.path.isfile(FAVICON_SVG), "static/favicon.svg not found; run from repo root"
    main()
