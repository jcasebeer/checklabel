"""Integration tests against real labels from the TTB Public COLA Registry.

Opt-in (real model calls, costs money):

    python scripts/fetch_cola_samples.py   # download fixtures first
    pytest -m integration

The registry publishes only approved COLAs — rejected applications are not
public — so real labels give us ground truth for the "should pass" direction,
and we synthesize the "should fail" direction by checking the same labels
against a deliberately wrong expected brand.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "cola"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                       reason="needs ANTHROPIC_API_KEY"),
    pytest.mark.skipif(not (FIXTURES / "manifest.json").exists(),
                       reason="no fixtures — run scripts/fetch_cola_samples.py"),
]

MAX_COLAS = 3  # keep API spend bounded


def load_colas():
    if not (FIXTURES / "manifest.json").exists():
        return []
    return json.loads((FIXTURES / "manifest.json").read_text())[:MAX_COLAS]


def brand_check(result):
    return next(c for c in result.checks if c.field == "Brand name")


async def verify_cola(cola, expected_brand):
    """Run one grouped verification over all panels of a COLA's label."""
    import anthropic

    from app.images import prepare_image
    from app.verifier import verify_label

    client = anthropic.AsyncAnthropic(timeout=60, max_retries=2)
    images = [prepare_image((FIXTURES / name).read_bytes()) for name in cola["images"]]
    return await verify_label(client, images, expected_brand, None)


@pytest.mark.parametrize("cola", load_colas(), ids=lambda c: c["ttbid"])
def test_approved_label_brand_is_recognized(cola):
    """The brand on a TTB-approved label must match its own COLA data.

    All panels of the label are checked together in one call, so the brand
    can come from any panel (front vs back).
    """
    result = asyncio.run(verify_cola(cola, cola["brand"]))
    assert result.overall != "error", f"verification errored: {result.error}"
    status = brand_check(result).status
    assert status in ("pass", "warn"), (
        f"brand {cola['brand']!r} not recognized on TTB ID {cola['ttbid']} "
        f"(status={status}, extracted={result.extracted.get('name_candidates')})"
    )


def test_wrong_brand_is_not_approved():
    """Ground-truth negative: a wrong expected brand must never pass."""
    result = asyncio.run(verify_cola(load_colas()[0], "Zzyzx Imaginary Spirits Co."))
    assert result.overall != "error", f"verification errored: {result.error}"
    assert brand_check(result).status == "fail"
