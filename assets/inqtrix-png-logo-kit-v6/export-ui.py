"""Re-export the README banner and compact browser badge from the kit artwork.

Run from any directory with Python + Pillow. Helvetica Neue is required for
banner lettering (macOS default); no fonts or generated artwork are downloaded.
This is an explicit design export, never a frontend/container build step.
"""
from pathlib import Path
import hashlib
import json
import shutil

from PIL import Image, ImageDraw, ImageFont, ImageOps

KIT = Path(__file__).resolve().parent
ROOT = KIT.parents[1]
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"


def export_banner():
    name = "inqtrix-github-hero-extended-dark.png"
    original = Image.open(KIT / name).convert("RGBA")
    background = ImageOps.fit(
        Image.open(KIT / "sources/inqtrix-ambient-background-source.png").convert("RGBA"),
        (1600, 400), Image.Resampling.LANCZOS,
    )
    # Preserve every existing logo pixel, including its material and edge alpha.
    background.paste(original.crop((0, 0, 420, 400)), (0, 0))
    copy = json.loads((KIT / "copy.json").read_text())
    draw = ImageDraw.Draw(background)
    for value, size, bold, y, color in (
        ("Inqtrix", 108, True, 79, "#F4F1EA"),
        (copy["product_descriptor"], 33, False, 223, "#D9DFE2"),
        (copy["extended_banner"], 22, False, 301, "#D9DFE2"),
    ):
        font = ImageFont.truetype(FONT, size, index=1 if bold else 0)
        draw.text((434, y), value, font=font, fill=color, anchor="lt")
    background.save(KIT / name)
    background.resize((960, 240), Image.Resampling.LANCZOS).save(
        KIT / "previews/inqtrix-readme-hero.png"
    )


def export_icons():
    # A constant Ink carrier keeps the badge legible in both browser chrome themes.
    badge = Image.new("RGBA", (1024, 1024))
    ImageDraw.Draw(badge).rounded_rectangle(
        (8, 8, 1015, 1015), radius=190, fill="#132536", outline="#F4F1EA", width=24,
    )
    mark = Image.open(KIT / "inqtrix-mark-flat-dark-1024.png").convert("RGBA")
    badge.alpha_composite(mark.resize((960, 960), Image.Resampling.LANCZOS), (32, 32))
    for size in (16, 32, 48):
        badge.resize((size, size), Image.Resampling.LANCZOS).save(KIT / f"favicon-{size}.png")
    badge.save(KIT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    badge.resize((180, 180), Image.Resampling.LANCZOS).save(KIT / "apple-touch-icon-180.png")
    public = ROOT / "apps/research-desk/public"
    for source, target in (
        ("favicon.ico", "favicon.ico"),
        ("favicon-32.png", "favicon-32.png"),
        ("apple-touch-icon-180.png", "apple-touch-icon.png"),
    ):
        shutil.copyfile(KIT / source, public / target)


def update_manifest():
    path = KIT / "manifest.json"
    manifest = json.loads(path.read_text())
    rows = []
    for file in sorted(KIT.rglob("*")):
        relative = file.relative_to(KIT)
        # Hidden files such as Finder's .DS_Store are never part of the kit.
        if (not file.is_file() or file == path or "__pycache__" in relative.parts
                or any(part.startswith(".") for part in relative.parts)):
            continue
        row = {"path": str(relative), "bytes": file.stat().st_size,
               "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
        if file.suffix.lower() in (".png", ".ico"):
            with Image.open(file) as image:
                row.update(width=image.width, height=image.height, mode=image.mode,
                           has_alpha="A" in image.getbands())
                if "A" in image.getbands():
                    row["contains_transparency"] = image.getchannel("A").getextrema()[0] < 255
                if file.suffix == ".ico":
                    row["embedded_sizes"] = sorted([list(size) for size in image.ico.sizes()])
        if row["path"].startswith("previews/"):
            row["role"] = ("README banner preview" if file.name == "inqtrix-readme-hero.png"
                           else "visual concept; not running application")
        rows.append(row)
    manifest["files"] = rows
    path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    export_banner()
    export_icons()
    update_manifest()
