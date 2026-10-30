"""
OptiMind's mark, drawn in code: a white pulse line on a rounded square in
OptiMind's blue. The tray icon, the app icon and the installer's images
(packaging/make_assets.py) all come from here, so they always match the
dashboard's logo.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

BLUE = (47, 96, 199)  # #2f60c7, the dashboard's filled blue (white on it reads at 5.8:1)
# The pulse line on a 64-unit square (the same points as the dashboard's SVG logo)
LINE = [(12, 34), (24, 34), (30, 20), (37, 46), (43, 30), (52, 30)]
_SUPERSAMPLE = 8


def draw_mark(size: int) -> Image.Image:
    """The mark as a size x size RGBA image, drawn large and scaled down for smooth edges.
    Small sizes get a relatively thicker line, so it stays readable at 16 px."""
    big = size * _SUPERSAMPLE
    unit = big / 64
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(16 * unit), fill=255)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (big, big), BLUE + (255,)), (0, 0), mask)

    width_px = max(1.9, size * 0.0875)  # line width in final pixels (5.6 of 64, as in the dashboard)
    width = round(width_px * _SUPERSAMPLE)
    points = [(x * unit, y * unit) for x, y in LINE]
    draw = ImageDraw.Draw(image)
    draw.line(points, fill=(255, 255, 255, 255), width=width, joint="curve")
    r = width / 2
    for x, y in (points[0], points[-1]):  # round line ends
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255))
    return image.resize((size, size), Image.LANCZOS)
