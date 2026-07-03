"""Core label verification.

Design: the model *extracts* what's printed on the label into a strict schema;
our code *decides* pass/fail. Keeping the judgment in code (especially the
exact government-warning check) makes results deterministic, auditable, and
tunable without re-prompting.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from . import config

# --- Extraction schema (forces structured JSON via tool use) ----------------
EXTRACT_TOOL = {
    "name": "report_label",
    "description": "Report exactly what is printed on the beverage label.",
    "input_schema": {
        "type": "object",
        "properties": {
            "brand_name": {
                "type": ["string", "null"],
                "description": "The most prominent brand name as printed, or null if not visible.",
            },
            "name_candidates": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("Every distinct brand-like name printed on the label — "
                                "producer/company name, brand name, product or fanciful "
                                "name — each exactly as printed."),
            },
            "alcohol_content_text": {
                "type": ["string", "null"],
                "description": "Alcohol statement exactly as printed, e.g. '13.5% ALC/VOL'.",
            },
            "abv_percent": {
                "type": ["number", "null"],
                "description": "The numeric ABV as a percentage, or null if none is shown.",
            },
            "government_warning": {
                "type": "object",
                "properties": {
                    "present": {"type": "boolean"},
                    "text_verbatim": {
                        "type": ["string", "null"],
                        "description": "The full warning transcribed exactly, preserving wording.",
                    },
                    "header_all_caps": {
                        "type": "boolean",
                        "description": "True if the 'GOVERNMENT WARNING:' header is in all capitals.",
                    },
                    "appears_bold": {
                        "type": ["boolean", "null"],
                        # A/B tested against a blurry bottle photo, sharp registry
                        # scans, and a synthetic non-bold control. This wording won
                        # every cell (5/5 true on a real bottle whose header IS
                        # bold, true on scans, false on the non-bold synthetic):
                        # evaluating size and stroke weight as independent
                        # dimensions lets the model perceive heavier strokes even
                        # when the size is identical, where "appears bold" was a
                        # coin flip and single-axis definitions failed real bottles.
                        "description": (
                            "Compare the text size and stroke weight of the words "
                            "GOVERNMENT WARNING to the text which follows it. If the "
                            "GOVERNMENT WARNING text size is larger, or the stroke "
                            "weight is heavier, return true. If the GOVERNMENT "
                            "WARNING text size is the same or smaller and the stroke "
                            "weight is the same or lighter, return false. If the "
                            "text is too blurry, curved, or glary to tell a "
                            "difference, return null."
                        ),
                    },
                },
                "required": ["present", "text_verbatim", "header_all_caps", "appears_bold"],
            },
        },
        "required": [
            "brand_name",
            "name_candidates",
            "alcohol_content_text",
            "abv_percent",
            "government_warning",
        ],
    },
}

_PROMPT = (
    "Transcribe this alcohol beverage label. Report only what is actually "
    "printed; do not guess or normalize. List every brand-like name you can "
    "see in name_candidates (producer, brand, product/fanciful names). "
    "Preserve exact wording and casing in text_verbatim. Use the report_label tool."
)


@dataclass
class Check:
    field: str
    status: str  # "pass" | "fail" | "warn" | "skipped"
    expected: Optional[str] = None
    found: Optional[str] = None
    detail: str = ""


@dataclass
class Result:
    overall: str  # "pass" | "fail" | "error"
    checks: list[Check] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    # Token usage of the extraction call, for spend accounting. Not exposed
    # in API responses.
    usage: Optional[dict] = None


# --- Normalization helpers --------------------------------------------------
def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _words(s: str) -> list[str]:
    """Tokenize for the word-for-word comparison the brief requires.

    The requirement is "exact... word-for-word" — every word, in order.
    Punctuation and spacing are NOT part of it: they're typography, and on
    phone photos they're also where transcription noise lives (a real bottle
    photo lost the comma in 'Surgeon General, women'; a real label prints
    'WARNING:(1)' with no space). Casing is checked separately."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()


def _norm_brand(s: str) -> str:
    s = re.sub(r"[^\w\s]", "", (s or ""))
    return re.sub(r"\s+", " ", s).strip().upper()


def _parse_abv(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(m.group(1)) if m else None


# Locates the warning header in the transcription regardless of casing, so the
# casing rule is decided here rather than trusted from the model's boolean.
_HEADER_RE = re.compile(
    r"\s*(" + r"\s+".join(re.escape(w) for w in config.WARNING_HEADER.rstrip(":").split()) + r"\s*:)",
    re.IGNORECASE,
)


def _header_all_caps(text: str) -> Optional[bool]:
    """Whether the transcribed header is ALL CAPS; None if it can't be located."""
    m = _HEADER_RE.match(text or "")
    if not m:
        return None
    return m.group(1) == m.group(1).upper()


# --- Individual field checks ------------------------------------------------
def check_brand(expected: str, found: Optional[str]) -> Check:
    label = "Brand name"
    if not found:
        return Check(label, "fail", expected, None, "No brand name found on the label.")
    ne, nf = _norm_brand(expected), _norm_brand(found)
    if ne == nf:
        return Check(label, "pass", expected, found, "Matches the application.")
    # Labels often print the registered brand inside a larger lockup — e.g.
    # application brand "TX" on a label reading "TX Experimental Series ...".
    # Whole-word containment (either direction) is likely the same brand, but
    # it isn't exact, so it goes to a human rather than passing silently.
    if ne and nf and (re.search(rf"\b{re.escape(ne)}\b", nf)
                      or re.search(rf"\b{re.escape(nf)}\b", ne)):
        return Check(label, "warn", expected, found,
                     "The application's brand appears within the label text — please confirm by eye.")
    ratio = difflib.SequenceMatcher(None, ne, nf).ratio()
    if ratio >= config.BRAND_FUZZY_WARN_THRESHOLD:
        return Check(label, "warn", expected, found,
                     "Very close to the application — please confirm by eye.")
    return Check(label, "fail", expected, found, "Does not match the application.")


_STATUS_RANK = {"pass": 2, "warn": 1, "fail": 0}


def best_brand_check(expected: str, extracted: dict) -> Check:
    """Brand check against every name printed on the label.

    A TTB "brand name" is whatever the applicant registered — sometimes the
    producer name, sometimes the product name (real registry examples: brand
    NEW BELGIUM on a label whose big text is FAT TIRE, and brand LEMON GINGER
    SPRITZY whose producer line is Tree House Brewing Company). The model
    reports every name-like string; the best match against any of them
    decides, with ties going to the primary brand_name.
    """
    candidates: list[Optional[str]] = [extracted.get("brand_name")]
    candidates += [c for c in (extracted.get("name_candidates") or []) if c]
    seen: set[str] = set()
    checks = []
    for cand in candidates:
        if cand is None or cand in seen:
            continue
        seen.add(cand)
        checks.append(check_brand(expected, cand))
    if not checks:
        return check_brand(expected, None)
    return max(checks, key=lambda c: _STATUS_RANK[c.status])


def check_abv(expected: Optional[float], found: Optional[float]) -> Check:
    label = "Alcohol content"
    exp_s = None if expected is None else f"{expected:g}%"
    fnd_s = None if found is None else f"{found:g}%"
    if expected is None:
        # The application doesn't list one, so there is nothing to compare —
        # informational, and deliberately not a block on approval.
        return Check(label, "skipped", None, fnd_s,
                     "The application doesn't list an alcohol content, so this was not checked.")
    if found is None:
        return Check(label, "fail", exp_s, None, "No alcohol content found on the label.")
    if abs(expected - found) <= config.ABV_TOLERANCE:
        return Check(label, "pass", exp_s, fnd_s, "Matches the application.")
    return Check(label, "fail", exp_s, fnd_s, "Does not match the application.")


def check_warning(gw: dict[str, Any]) -> Check:
    label = "Government warning"
    if not gw or not gw.get("present"):
        return Check(label, "fail", "Required", "Missing", "The federal warning is not on the label.")

    found = gw.get("text_verbatim") or ""
    issues: list[str] = []

    # Word-for-word wording match (casing handled separately).
    if _words(found) != _words(config.GOVERNMENT_WARNING):
        issues.append("wording does not match the required text word-for-word")
    # Casing is decided from the verbatim transcription; the model's boolean is
    # only a fallback for when the header can't be located in the text.
    caps = _header_all_caps(found)
    if caps is None:
        caps = bool(gw.get("header_all_caps"))
    if not caps:
        issues.append(f"'{config.WARNING_HEADER}' is not in all capitals")
    bold = gw.get("appears_bold")
    if bold is False:
        issues.append("the header does not appear bold")

    status = "pass"
    detail = "Present, exact, and correctly formatted."
    if issues:
        status = "fail"
        detail = "Problem: " + "; ".join(issues) + "."
    elif bold is None:
        status = "warn"
        detail = "Wording and capitals are correct, but bold could not be confirmed — check by eye."

    return Check(label, status, "Exact federal text", _norm_text(found)[:120], detail)


# --- Model call -------------------------------------------------------------
# A label is often several panels (front/back, neck, keg collar). All panels go
# into ONE extraction call so the model reports one coherent result — brand
# from the front, warning from the back — instead of per-panel "missing" noise.
Images = list[tuple[str, str]]  # [(base64_data, media_type), ...]


def extract_request_params(images: Images) -> dict:
    """Messages API params for one extraction — shared by the live call and the
    Message Batches path so both send byte-identical requests."""
    content: list[dict] = [
        {"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": image_b64}}
        for image_b64, media_type in images
    ]
    prompt = _PROMPT
    if len(images) > 1:
        prompt = (f"These {len(images)} images are panels of ONE product's label "
                  f"(e.g. front and back). ") + _PROMPT
    content.append({"type": "text", "text": prompt})
    return {
        "model": config.MODEL,
        "max_tokens": config.MAX_TOKENS,
        "tools": [EXTRACT_TOOL],
        "tool_choice": {"type": "tool", "name": "report_label"},
        "messages": [{"role": "user", "content": content}],
    }


def extraction_from_message(msg: Any) -> dict:
    """Pull the report_label tool input out of an API response message."""
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Model did not return structured label data.")


async def _extract(client: anthropic.AsyncAnthropic, images: Images) -> tuple[dict, Optional[dict]]:
    # Retries for 429/5xx/connection errors are handled by the SDK client
    # (max_retries on the shared client); 4xx errors fail fast.
    msg = await client.messages.create(**extract_request_params(images))
    usage = None
    u = getattr(msg, "usage", None)
    if u is not None:
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0}
    return extraction_from_message(msg), usage


def decide(extracted: dict, expected_brand: str, expected_abv: Optional[float]) -> Result:
    abv_found = extracted.get("abv_percent")
    if abv_found is None:  # explicit None check: an ABV of 0 is a real reading
        abv_found = extracted.get("alcohol_content_text")
    checks = [
        best_brand_check(expected_brand, extracted),
        check_abv(expected_abv, _parse_abv(abv_found)),
        check_warning(extracted.get("government_warning") or {}),
    ]
    if any(c.status == "fail" for c in checks):
        overall = "fail"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "pass"
    return Result(overall=overall, checks=checks, extracted=extracted)


async def verify_label(
    client: anthropic.AsyncAnthropic,
    images: Images,
    expected_brand: str,
    expected_abv: Optional[float],
) -> Result:
    """Extract fields from one label (all its panels) and decide pass/fail."""
    try:
        extracted, usage = await _extract(client, images)
    except Exception as exc:  # noqa: BLE001 - surface a clean message, don't crash the batch
        return Result(overall="error", error=str(exc))
    result = decide(extracted, expected_brand, expected_abv)
    result.usage = usage
    return result
