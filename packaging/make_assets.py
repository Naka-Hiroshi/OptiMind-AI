"""
Draws OptiMind's icon and the installer's images from the logo in
app/desktop/icon.py, so they always match it:

    python packaging/make_assets.py

  - app/static/optimind.ico: the app's icon (Start menu, taskbar, window, exe),
    every size drawn for that size;
  - packaging/assets/wizard-*.png: the installer's side panel (welcome and
    finish pages), black like the dashboard, one per display scaling;
  - packaging/assets/corner-*.png: the logo in the installer's top corner.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.desktop.icon import LINE, draw_mark  # noqa: E402

ASSETS = ROOT / "packaging" / "assets"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
# Inno Setup's image areas at 100% … 250% display scaling
WIZARD_SIZES = ((202, 386), (269, 515), (336, 643), (403, 772), (430, 824), (498, 953), (534, 1022))
CORNER_SIZES = (58, 77, 97, 116, 124, 143, 159)
FONT = Path(r"C:\Windows\Fonts\seguisb.ttf")  # Segoe UI Semibold


def make_ico() -> Path:
    frames = [draw_mark(size) for size in ICO_SIZES]
    path = ROOT / "app" / "static" / "optimind.ico"
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    return path


def wizard_panel(width: int, height: int) -> Image.Image:
    """OptiMind's black panel: a faint blue pulse line across it, the logo with a soft blue glow, and the name."""
    top, bottom = (23, 23, 27), (0, 0, 0)  # the dashboard's raised black down to its black
    gradient = Image.linear_gradient("L").resize((width, height))
    panel = Image.composite(Image.new("RGB", (width, height), bottom), Image.new("RGB", (width, height), top), gradient)
    panel = panel.convert("RGBA")

    # A large, faint pulse line running across the panel, in the dashboard's blue
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    unit = width / 40
    y0 = height * 0.66
    points = [(x * unit - unit * 8, y0 + (y - 34) * unit * 0.9) for x, y in LINE]
    points = [(-unit, y0)] + points + [(width + unit, points[-1][1])]
    ImageDraw.Draw(glow).line(points, fill=(61, 123, 255, 80), width=max(2, round(width / 70)), joint="curve")
    panel.alpha_composite(glow.filter(ImageFilter.GaussianBlur(width / 400)))

    # The logo, with a soft glow of its blue behind it
    tile = round(width * 0.42)
    mark = draw_mark(tile)
    alpha = mark.getchannel("A")
    halo = Image.new("RGBA", (tile + tile // 2, tile + tile // 2), (0, 0, 0, 0))
    halo.paste(Image.new("RGBA", (tile, tile), (61, 123, 255, 110)), (tile // 4, tile // 4), alpha)
    halo = halo.filter(ImageFilter.GaussianBlur(tile / 9))
    x, y = (width - tile) // 2, round(height * 0.2)
    panel.alpha_composite(halo, (x - tile // 4, y - tile // 4))
    panel.alpha_composite(mark, (x, y))

    # The name under it
    if FONT.exists():
        font = ImageFont.truetype(str(FONT), round(width * 0.105))
        text = "OptiMind AI"
        d = ImageDraw.Draw(panel)
        w = d.textlength(text, font=font)
        d.text(((width - w) / 2, y + tile + width * 0.09), text, font=font, fill=(255, 255, 255, 255))
    return panel.convert("RGB")


def corner_logo(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = round(size * 0.86)
    offset = (size - inner) // 2
    image.alpha_composite(draw_mark(inner), (offset, offset))
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("icon:", make_ico())
    for w, h in WIZARD_SIZES:
        wizard_panel(w, h).save(ASSETS / f"wizard-{w}x{h}.png", optimize=True)
    for s in CORNER_SIZES:
        corner_logo(s).save(ASSETS / f"corner-{s}.png", optimize=True)
    print(f"installer images: {len(WIZARD_SIZES)} side panels, {len(CORNER_SIZES)} corner logos in {ASSETS}")


if __name__ == "__main__":
    main()
