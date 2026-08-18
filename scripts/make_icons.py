#!/usr/bin/env python
"""Generate the PWA app icons.

    pip install pillow && python scripts/make_icons.py

Pillow is NOT in requirements.txt -- this runs by hand when the artwork or the
palette changes, and the PNGs it produces are committed. Adding an imaging
library to every production container to redraw a file that changes once a
year is not a trade worth making.

The icon is drawn from the same `primary` token as the rest of the UI (see
tailwind.config.js), so the home-screen tile and the app agree on what colour
TTStats is. Swap SOURCE_HSL if that token moves.

Outputs, all into pingpong/static/pingpong/icons/app/:

  icon-192.png          the manifest's small icon, `purpose: any`
  icon-512.png          the manifest's large icon, `purpose: any`
  icon-maskable-512.png same art inside the 80% safe zone, `purpose: maskable`
  apple-touch-icon.png  180px, opaque, square -- iOS rounds it itself and
                        renders alpha as black, so this one gets no
                        transparency and no rounding

Replacing these with real artwork needs no code change: keep the filenames.
"""

import colorsys
import math
from pathlib import Path

from PIL import Image, ImageDraw

# tailwind.config.js: colors.primary.DEFAULT
SOURCE_HSL = (222.2, 47.4, 11.2)
BALL_RGB = (249, 168, 37)      # warm amber, reads as a ping-pong ball
BLADE_RGB = (239, 68, 68)      # red-500, the classic rubber
HANDLE_RGB = (226, 232, 240)   # slate-200

OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / 'ttstats' / 'pingpong' / 'static' / 'pingpong' / 'icons' / 'app'
)

# Supersampling factor. Pillow has no antialiasing on draw primitives, so
# everything is drawn 4x and downsampled -- without this the paddle's edges
# are visibly jagged at 192px.
SS = 4


def hsl_to_rgb(h, s, light):
    r, g, b = colorsys.hls_to_rgb(h / 360.0, light / 100.0, s / 100.0)
    return (round(r * 255), round(g * 255), round(b * 255))


BG_RGB = hsl_to_rgb(*SOURCE_HSL)


def draw_paddle(size, scale=1.0):
    """The paddle-and-ball mark on a transparent square of `size` px.

    `scale` shrinks the art within the square without shrinking the square,
    which is how the maskable variant keeps clear of the safe-zone crop.
    """
    canvas = size * SS
    layer = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))

    art = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(art)

    c = canvas / 2
    u = canvas * 0.01 * scale  # one design unit

    # Handle first, so the blade overlaps its top end.
    handle_w = 8 * u
    d.rounded_rectangle(
        [c - handle_w / 2, c + 2 * u, c + handle_w / 2, c + 40 * u],
        radius=handle_w / 2,
        fill=HANDLE_RGB,
    )

    # Blade: a circle with a thin light rim, so it stays legible against the
    # dark background at 192px where a flat red disc goes muddy.
    blade_r = 21 * u
    d.ellipse(
        [c - blade_r, c - blade_r - 8 * u, c + blade_r, c + blade_r - 8 * u],
        fill=BLADE_RGB,
        outline=HANDLE_RGB,
        width=max(1, round(2 * u)),
    )

    # Rotate the whole paddle so it reads as in-motion rather than as a lollipop.
    art = art.rotate(-28, resample=Image.BICUBIC, center=(c, c))
    layer.alpha_composite(art)

    # Ball last and unrotated: it sits off the blade's top-right, on the arc
    # the rotation swept it toward.
    d2 = ImageDraw.Draw(layer)
    ball_r = 9 * u
    angle = math.radians(-58)
    bx = c + math.cos(angle) * 33 * u
    by = c + math.sin(angle) * 33 * u
    d2.ellipse([bx - ball_r, by - ball_r, bx + ball_r, by + ball_r], fill=BALL_RGB)

    return layer.resize((size, size), Image.LANCZOS)


def rounded_tile(size, radius_ratio, scale=1.0, opaque=False):
    """Background tile with the paddle composited on top."""
    canvas = size * SS
    base = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    if opaque or radius_ratio == 0:
        d.rectangle([0, 0, canvas, canvas], fill=BG_RGB)
    else:
        d.rounded_rectangle(
            [0, 0, canvas - 1, canvas - 1],
            radius=canvas * radius_ratio,
            fill=BG_RGB,
        )
    base = base.resize((size, size), Image.LANCZOS)
    base.alpha_composite(draw_paddle(size, scale=scale))
    return base


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # `purpose: any` -- the launcher shows these as drawn, so they carry their
    # own corner rounding.
    rounded_tile(192, 0.22).save(OUT_DIR / 'icon-192.png')
    rounded_tile(512, 0.22).save(OUT_DIR / 'icon-512.png')

    # `purpose: maskable` -- Android crops to an arbitrary shape and only the
    # centre 80% is guaranteed to survive. Full-bleed background, art at 72%.
    rounded_tile(512, 0, scale=0.72).save(OUT_DIR / 'icon-maskable-512.png')

    # iOS renders any alpha as black and applies its own mask, so: square,
    # opaque, no rounding of our own.
    apple = rounded_tile(180, 0, opaque=True).convert('RGB')
    apple.save(OUT_DIR / 'apple-touch-icon.png')

    for path in sorted(OUT_DIR.iterdir()):
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent.parent.parent)}")


if __name__ == '__main__':
    main()
