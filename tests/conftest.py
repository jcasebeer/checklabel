import io

import pytest
from PIL import Image


def make_image_bytes(width: int = 400, height: int = 300, fmt: str = "PNG",
                     color=(200, 180, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return make_image_bytes()


@pytest.fixture
def client(monkeypatch):
    """TestClient with an API key present (model calls must be stubbed per-test)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
