"""Image preprocessing tests."""
import base64
import io

import pytest
from PIL import Image

from app import config
from app.images import ImageError, prepare_image
from tests.conftest import make_image_bytes


def decoded(image_b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.standard_b64decode(image_b64)))


def test_valid_png_becomes_jpeg(png_bytes):
    b64, media_type = prepare_image(png_bytes)
    assert media_type == "image/jpeg"
    img = decoded(b64)
    assert img.format == "JPEG"
    assert img.size == (400, 300)


def test_empty_file_rejected():
    with pytest.raises(ImageError, match="empty"):
        prepare_image(b"")


def test_non_image_rejected():
    with pytest.raises(ImageError, match="readable image"):
        prepare_image(b"this is not an image, it's a manifest" * 100)


def test_oversized_upload_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ImageError, match="limit"):
        prepare_image(b"x" * 101)


def test_large_image_downscaled_never_upscaled(monkeypatch):
    monkeypatch.setattr(config, "MAX_IMAGE_EDGE_PX", 500)
    b64, _ = prepare_image(make_image_bytes(2000, 1000))
    assert max(decoded(b64).size) == 500
    b64, _ = prepare_image(make_image_bytes(200, 100))
    assert decoded(b64).size == (200, 100)


def test_transparency_flattened():
    buf = io.BytesIO()
    Image.new("RGBA", (50, 50), (255, 0, 0, 128)).save(buf, format="PNG")
    b64, media_type = prepare_image(buf.getvalue())
    assert media_type == "image/jpeg"
    assert decoded(b64).mode == "RGB"


def test_exif_orientation_honored():
    # A 100x50 JPEG tagged "rotate 90" should come out 50x100.
    img = Image.new("RGB", (100, 50), (10, 20, 30))
    exif = img.getexif()
    exif[0x0112] = 6  # orientation: rotate 90 CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    b64, _ = prepare_image(buf.getvalue())
    assert decoded(b64).size == (50, 100)
