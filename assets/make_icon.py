"""Generate Addled app + tray icons (no external assets needed)."""
import pathlib
from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path(__file__).parent.parent / "electron" / "icons"
OUT.mkdir(parents=True, exist_ok=True)


def rounded_square(size: int, radius: int, top, bottom):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=top)
    # bottom gradient overlay
    for y in range(size // 2, size):
        t = (y - size // 2) / (size // 2)
        r = int(bottom[0] + (top[0] - bottom[0]) * (1 - t))
        g = int(bottom[1] + (top[1] - bottom[1]) * (1 - t))
        b = int(bottom[2] + (top[2] - bottom[2]) * (1 - t))
        d.line([(0, y), (size, y)], fill=(r, g, b, 255))
    return img


def try_font(size: int):
    for name in ("segoeuiblack.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_app_icon():
    size = 512
    img = rounded_square(size, 112, (24, 28, 48, 255), (13, 17, 33, 255))
    d = ImageDraw.Draw(img, "RGBA")

    # accent ring
    d.ellipse([56, 56, 456, 456], outline=(255, 171, 64, 255), width=18)

    # spark / companion dot
    d.ellipse([176, 120, 336, 280], fill=(255, 171, 64, 255))
    d.ellipse([236, 256, 276, 296], fill=(255, 214, 140, 255))

    # letter A
    font = try_font(150)
    d.text((256, 330), "A", font=font, fill=(255, 214, 140, 255),
           anchor="mm", stroke_width=4, stroke_fill=(24, 28, 48, 255))

    img.save(OUT / "icon.png")
    print("wrote", OUT / "icon.png")


def make_tray_icon():
    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse([2, 2, 29, 29], fill=(255, 171, 64, 255))
    d.ellipse([10, 6, 22, 18], fill=(255, 214, 140, 255))
    font = try_font(14)
    d.text((16, 20), "A", font=font, fill=(24, 28, 48, 255), anchor="mm")
    img.save(OUT / "tray-icon.png")
    print("wrote", OUT / "tray-icon.png")


if __name__ == "__main__":
    make_app_icon()
    make_tray_icon()
