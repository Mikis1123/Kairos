"""Renders assets/kairos-ios-icon.svg into the iOS asset catalog (1024 px, opaque RGB).

Needs resvg-py and Pillow:  pip install resvg-py pillow
Run from the repo root:     python ios/tools/render_icon.py
The PNG is committed, so CI never needs this script.
"""
import io
import pathlib

import resvg_py
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "kairos-ios-icon.svg"
DST = ROOT / "ios" / "KairosApp" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon-1024.png"

png = bytes(resvg_py.svg_to_bytes(svg_string=SRC.read_text(encoding="utf-8"), width=1024, height=1024))
img = Image.open(io.BytesIO(png)).convert("RGBA")
flat = Image.new("RGB", img.size, (20, 20, 24))      # App Store rejects alpha in the icon
flat.paste(img, mask=img.split()[3])
flat.save(DST, optimize=True)
print(DST, flat.size)
