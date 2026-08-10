"""Generate sample files used by the test suite.

Run: python tests/make_fixtures.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"


def photo(width: int = 2400, height: int = 1600) -> Image.Image:
    """A noisy gradient with shapes - compresses like a real photograph."""
    random.seed(7)
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(0, width, 4):
            r = int(120 + 100 * math.sin(x / 190) + random.randint(-14, 14))
            g = int(120 + 100 * math.sin(y / 150) + random.randint(-14, 14))
            b = int(140 + 90 * math.cos((x + y) / 230) + random.randint(-14, 14))
            colour = (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
            for dx in range(4):
                if x + dx < width:
                    pixels[x + dx, y] = colour

    draw = ImageDraw.Draw(img)
    for i in range(14):
        x0 = random.randint(0, width - 300)
        y0 = random.randint(0, height - 300)
        draw.ellipse([x0, y0, x0 + 260, y0 + 260], fill=(random.randint(0, 255),) * 3)
    return img


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    base = photo()

    # A deliberately under-compressed JPEG, so there is room to improve.
    base.save(FIXTURES / "photo.jpg", quality=97, subsampling=0)

    # Large lossless PNG of photographic content.
    base.resize((1200, 800)).save(FIXTURES / "photo.png", compress_level=1)

    # PNG with a real alpha channel.
    alpha = Image.new("RGBA", (900, 900), (0, 0, 0, 0))
    d = ImageDraw.Draw(alpha)
    for i in range(10):
        d.ellipse([i * 30, i * 30, 880 - i * 30, 880 - i * 30],
                  fill=(40 + i * 20, 90, 220 - i * 15, 255 - i * 20))
    alpha.save(FIXTURES / "logo-alpha.png", compress_level=1)

    # Animated GIF.
    frames = []
    for i in range(12):
        frame = Image.new("RGB", (320, 240), (20, 20, 40))
        fd = ImageDraw.Draw(frame)
        offset = i * 20
        fd.ellipse([offset, 80, offset + 80, 160], fill=(240, 120, 40))
        frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE))
    frames[0].save(FIXTURES / "spin.gif", save_all=True, append_images=frames[1:],
                   duration=80, loop=0)

    # WebP and BMP and TIFF, to exercise format detection.
    base.resize((1000, 667)).save(FIXTURES / "photo.webp", quality=95)
    base.resize((600, 400)).save(FIXTURES / "photo.bmp")
    base.resize((800, 533)).save(FIXTURES / "photo.tiff")

    # Photo-heavy PDF: three pages, each a full-bleed high quality image.
    pages = [base.resize((1700, 2200)).convert("RGB") for _ in range(3)]
    pages[0].save(FIXTURES / "scanned.pdf", save_all=True, append_images=pages[1:],
                  resolution=200.0, quality=95)

    # Text-only PDF, where only the lossless engine can help.
    text_pages = []
    for page_no in range(3):
        page = Image.new("RGB", (1240, 1754), "white")
        pd = ImageDraw.Draw(page)
        for line in range(46):
            pd.text((90, 90 + line * 34), f"Page {page_no + 1} line {line + 1} " + "lorem ipsum dolor sit amet " * 2, fill="black")
        text_pages.append(page)
    text_pages[0].save(FIXTURES / "text.pdf", save_all=True, append_images=text_pages[1:],
                       resolution=150.0)

    # Not a supported file at all.
    (FIXTURES / "notes.txt").write_bytes(b"just some plain text, definitely not a pdf\n" * 40)

    for path in sorted(FIXTURES.iterdir()):
        print(f"{path.name:20s} {path.stat().st_size / 1024:9.1f} KB")


if __name__ == "__main__":
    main()
