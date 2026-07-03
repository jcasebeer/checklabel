"""Configuration for the label check service.

Everything here is overridable via environment variables so the same image
runs identically on a laptop, a container, or a VPS. Nothing sensitive is
stored on disk; the only secret is the API key, read from the environment.
"""
from __future__ import annotations

import os


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- Model -----------------------------------------------------------------
# Sonnet is the accuracy/latency sweet spot for careful label transcription.
MODEL = _get("LABEL_CHECK_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(_get("LABEL_CHECK_MAX_TOKENS", "1024"))
API_TIMEOUT_SECONDS = float(_get("LABEL_CHECK_API_TIMEOUT", "30"))
API_MAX_RETRIES = int(_get("LABEL_CHECK_API_RETRIES", "2"))

# --- Batch concurrency -----------------------------------------------------
# Bounded fan-out keeps a 200-300 label batch fast without tripping rate limits.
BATCH_CONCURRENCY = int(_get("LABEL_CHECK_BATCH_CONCURRENCY", "8"))

# --- Image handling --------------------------------------------------------
MAX_IMAGE_EDGE_PX = int(_get("LABEL_CHECK_MAX_EDGE", "1568"))
MAX_UPLOAD_BYTES = int(_get("LABEL_CHECK_MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))
# A label may span several panels (front/back/neck) checked in one model call.
MAX_IMAGES_PER_LABEL = int(_get("LABEL_CHECK_MAX_IMAGES_PER_LABEL", "8"))

# --- Matching tolerances ---------------------------------------------------
# Brand: exact after normalization passes; a high fuzzy ratio is flagged for
# human review ("warn") rather than silently passed or hard-failed. This is the
# STONE'S THROW vs Stone's Throw case from the brief.
BRAND_FUZZY_WARN_THRESHOLD = float(_get("LABEL_CHECK_BRAND_WARN", "0.90"))
# ABV: absolute percentage-point tolerance. Default 0.0 = must match exactly.
ABV_TOLERANCE = float(_get("LABEL_CHECK_ABV_TOLERANCE", "0.0"))

# --- Canonical federal warning ---------------------------------------------
# Statutory text mandated by 27 CFR 16.21 (U.S. government warning). This is
# the fixed reference every label is checked against; it is NOT per-application
# metadata, so it lives in the app rather than being supplied at request time.
GOVERNMENT_WARNING = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability to "
    "drive a car or operate machinery, and may cause health problems."
)
# The header portion that must appear in ALL CAPS and bold.
WARNING_HEADER = "GOVERNMENT WARNING:"
