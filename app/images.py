"""Image preprocessing.

Labels arrive from phones and scanners: rotated, oversized, odd formats. We
normalize before sending to the model, which both controls cost (tokens scale
with pixels) and improves robustness on the "imperfect image" cases (odd angles
handled via EXIF orientation).
"""
from __future__ import annotations

import base64
import io

from PIL import Image, ImageOps

from . import config


class ImageError(ValueError):
    """Raised when an upload is not a usable image."""


def prepare_image(raw: bytes) -> tuple[str, str]:
    """Return (base64_data, media_type) ready for an image content block.

    Raises ImageError on anything that is not a decodable image.
    """
    if not raw:
        raise ImageError("The file was empty.")
    if len(raw) > config.MAX_UPLOAD_BYTES:
        mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ImageError(f"That image is larger than the {mb} MB limit.")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise ImageError("That file isn't a readable image (try JPEG or PNG).") from exc

    # Honor EXIF orientation so a phone photo taken sideways reads upright.
    img = ImageOps.exif_transpose(img)

    # Flatten transparency / palettes to RGB for consistent JPEG encoding.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Downscale so the longest edge fits our cap; never upscale.
    longest = max(img.size)
    if longest > config.MAX_IMAGE_EDGE_PX:
        scale = config.MAX_IMAGE_EDGE_PX / longest
        new_size = (round(img.width * scale), round(img.height * scale))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return data, "image/jpeg"
