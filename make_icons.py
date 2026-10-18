"""Render the PWA icons (icons/*.png) -- a white Hebrew resh on the page-header blue.

Needs Pillow and a Hebrew-capable bold font (David Bold on Windows; pass --font
for another).  The PNGs are committed, so this only needs re-running to restyle.
"""
import argparse
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BLUE = (31, 78, 121)      # matches the --head colour of the pages
WHITE = (255, 255, 255)


def render(size, font_path, letter="\u05e8", glyph=0.62):
    """glyph = letter height as a fraction of the canvas (maskable icons need
    the artwork inside the central 80% safe zone, so they pass a smaller one)."""
    img = Image.new("RGB", (size, size), BLUE)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(font_path, int(size * glyph * 1.35))
    l, t, r, b = d.textbbox((0, 0), letter, font=f)
    d.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t), letter, font=f, fill=WHITE)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", default=r"C:\Windows\Fonts\davidbd.ttf")
    ap.add_argument("--out", default=os.path.join(HERE, "icons"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name, size, glyph in (("icon-192.png", 192, 0.62), ("icon-512.png", 512, 0.62),
                              ("icon-maskable-512.png", 512, 0.46),
                              ("apple-touch-icon.png", 180, 0.62)):
        render(size, a.font, glyph=glyph).save(os.path.join(a.out, name))
        print("wrote", name)


if __name__ == "__main__":
    main()
