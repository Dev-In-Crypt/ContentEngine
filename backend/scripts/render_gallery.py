"""Render the home page's gallery of finished slides.

Run once and commit the output; nothing calls this at runtime.

    py scripts/render_gallery.py

Why a script and not a fixture folder: every picture on the landing page has to
be something the product really produces, and the only way to guarantee that is
to produce it with the product. These five files come out of the same
`PillowBrandEngine.create_branded_card` that renders a slide inside a post, at
the same 1080x1350, with no model involved and no network call.

Two honest limits, both visible in the output and both said out loud on the page
itself:

* The words are written, not generated. A gallery of model output would be
  showing one roll of the dice as if it were the product, and re-rolling it for
  a nicer one is how a page ends up promising an average nobody gets.
* The backgrounds are drawn here, not photographed. In a real post the picture
  comes from the visitor's own library, their stock account or the image model —
  none of which we may ship. A plain ground is the one background that is
  nobody else's property and misrepresents nothing; what the engine contributes
  is the treatment on top of it, and that is what these show.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageChops, ImageDraw, ImageFilter  # noqa: E402

from services.brand_engine import BrandConfig, PillowBrandEngine  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "static" / "img" / "gallery"

SIZE = (1080, 1350)

#: Three niches, three brand colours, three headlines. Three rather than five
#: because five across the page shrank each slide to about 220 points, where
#: the headline the slide exists to carry stopped being readable at all.
#: Ordinary posts on purpose:
#: a gallery of exceptional ones is a promise the next visitor's first post has
#: to keep. Colours are from the engine's own suggested swatches.
CARDS = [
    ("gallery-coffee", "COFFEE", "Three habits for better coffee",
     "#ff751f", (30, 24, 21), (14, 11, 10)),
    ("gallery-saas", "SAAS", "Why your demo loses people at minute two",
     "#5e17eb", (24, 22, 32), (11, 10, 15)),
    ("gallery-travel", "TRAVEL", "Two days in Lisbon, planned by nobody",
     "#0076cb", (18, 24, 31), (8, 11, 14)),
]


def ground(top: tuple[int, int, int], bottom: tuple[int, int, int],
           accent: str) -> bytes:
    """The picture under the card, drawn rather than photographed.

    The first version was a plain vertical gradient, which is what the engine
    does with a flat background and looks like exactly that: a tall empty frame
    with a bar of text near the bottom. In a real post that space is a
    photograph, so an empty version of it undersells the product on the one
    page where it is being judged.

    So the ground is composed: a fall from top to bottom, two very faint fields
    of the brand colour, and a little grain. Faint on purpose — the first
    attempt weighted them at half strength and produced five saturated
    gradients, which is both unlike this product and on the owner's short list
    of things not to put on this page. What the eye should find here is the
    card, not the wallpaper behind it.

    Nothing in it is anybody's photograph and nothing is a stock image we do
    not have the right to ship, which is the whole reason it is drawn.
    """
    w, h = SIZE
    ramp = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        ramp.putpixel((0, y), tuple(
            round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    img = ramp.resize((w, h), Image.BILINEAR)

    # Three broad fields, blurred until no edge of them is visible. Placed by
    # hand rather than at random so every rerun of this script produces the
    # same five files — a gallery that changes on each run is a gallery nobody
    # can review.
    ar, ag, ab = (int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    fields = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(fields)
    for cx, cy, r, weight in ((0.26, 0.22, 0.44, 0.10),
                              (0.84, 0.58, 0.34, 0.06)):
        box = (w * (cx - r), h * (cy - r * w / h),
               w * (cx + r), h * (cy + r * w / h))
        d.ellipse(box, fill=(round(ar * weight), round(ag * weight),
                             round(ab * weight)))
    fields = fields.filter(ImageFilter.GaussianBlur(radius=190))
    img = ImageChops.add(img, fields)

    # Grain, so a large flat area does not band in JPEG.
    noise = Image.effect_noise((w, h), 7).convert("L").point(lambda v: v // 12)
    img = ImageChops.add(img, Image.merge("RGB", (noise, noise, noise)))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, niche, headline, colour, top, bottom in CARDS:
        engine = PillowBrandEngine(BrandConfig(
            niche_box_color=colour,
            # No logo: there is no brand here to put one on, and a made-up mark
            # on the home page is the fabricated-proof line this phase does not
            # cross.
            show_logo=False,
        ))
        jpeg = engine.create_branded_card(
            background_image=ground(top, bottom, colour),
            niche_text=niche,
            description_text=headline,
        )
        path = OUT / f"{name}.jpg"
        path.write_bytes(jpeg)
        print(f"{path.relative_to(OUT.parent.parent.parent)}  {len(jpeg) // 1024} KB")


if __name__ == "__main__":
    main()
