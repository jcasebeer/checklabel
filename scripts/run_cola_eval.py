#!/usr/bin/env python3
"""Evaluate the verifier against real TTB COLA labels, cheaply.

Because the design is "the model extracts, the code decides", each label needs
exactly ONE model call no matter how many expectations we score against it.
All panels of a COLA's label (front/back/neck) go into that single call, the
same request shape the app itself sends. This script:

  1. extracts every COLA's label once, through the Anthropic Message Batches
     API (50% of standard token cost), caching results in extractions.json;
  2. scores two cases per COLA in plain Python, no further model calls:
       positive — expected brand = the COLA's own brand   -> should match
       negative — expected brand = another COLA's brand   -> should not match
     (the public registry has no rejected applications, so negatives are
     synthesized; donors sharing words with the target brand are excluded
     because flagging same-family brands for review is correct behavior);
  3. writes per-case pass/fail metadata to eval_results.json and prints a
     summary.

Usage:
  set -a; source ~/api_key.env; set +a
  python scripts/run_cola_eval.py            # extract (batch API) + score
  python scripts/run_cola_eval.py --rescore  # score only, from cached extractions

Re-runs only extract COLAs missing from the cache, so iterating on decision
rules is free; the cache is keyed to the extraction schema and is discarded
automatically when the schema changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest

from app import config
from app.images import prepare_image
from app.verifier import (
    EXTRACT_TOOL,
    _norm_brand,
    best_brand_check,
    check_warning,
    extract_request_params,
    extraction_from_message,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cola"
CACHE = FIXTURES / "extractions.json"
RESULTS = FIXTURES / "eval_results.json"

# Bump when the request shape changes in a way the schema hash can't see.
CACHE_FLAVOR = "grouped-panels-v2"

# Keep each submitted batch well under the API's 256 MB request ceiling.
MAX_BATCH_BYTES = 150 * 1024 * 1024
POLL_SECONDS = 20
POLL_TIMEOUT = 2 * 60 * 60


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def schema_hash() -> str:
    blob = CACHE_FLAVOR + json.dumps(EXTRACT_TOOL, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_cache() -> dict:
    """Extraction cache (one entry per COLA), invalidated on schema change."""
    cache = load_json(CACHE, {})
    if cache.get("__meta__", {}).get("schema") != schema_hash():
        if cache:
            print("Extraction schema changed — discarding stale cache.")
        cache = {"__meta__": {"schema": schema_hash()}}
    return cache


def cache_entries(cache: dict) -> dict:
    return {k: v for k, v in cache.items() if k != "__meta__"}


def extract_missing(colas: list[dict], cache: dict) -> dict:
    """Extract every COLA not yet cached — all panels in one request."""
    todo = [c for c in colas if c["ttbid"] not in cache]
    if not todo:
        print("All COLAs already extracted (cache hit).")
        return cache
    print(f"Extracting {len(todo)} COLAs "
          f"({sum(len(c['images']) for c in todo)} panels) via the Message "
          f"Batches API (model {config.MODEL}, 50% batch pricing)...")

    client = anthropic.Anthropic(max_retries=3)

    chunks: list[list[BatchRequest]] = [[]]
    chunk_bytes = 0
    for cola in todo:
        ttbid = cola["ttbid"]  # digits only — valid as a custom_id
        try:
            images = [prepare_image((FIXTURES / n).read_bytes()) for n in cola["images"]]
        except Exception as exc:  # noqa: BLE001 - a bad fixture shouldn't kill the eval
            cache[ttbid] = {"__error__": f"unreadable fixture: {exc}"}
            continue
        payload = sum(len(b64) for b64, _ in images)
        if chunk_bytes + payload > MAX_BATCH_BYTES and chunks[-1]:
            chunks.append([])
            chunk_bytes = 0
        chunks[-1].append(BatchRequest(
            custom_id=ttbid,
            params=MessageCreateParamsNonStreaming(**extract_request_params(images))))
        chunk_bytes += payload

    batch_ids = []
    for chunk in chunks:
        if not chunk:
            continue
        mb = client.messages.batches.create(requests=chunk)
        batch_ids.append(mb.id)
        print(f"  submitted {mb.id} ({len(chunk)} requests)")

    deadline = time.monotonic() + POLL_TIMEOUT
    pending = set(batch_ids)
    while pending:
        if time.monotonic() > deadline:
            sys.exit(f"Timed out waiting for batches: {sorted(pending)}")
        time.sleep(POLL_SECONDS)
        for bid in sorted(pending):
            mb = client.messages.batches.retrieve(bid)
            c = mb.request_counts
            print(f"  {bid}: {mb.processing_status} "
                  f"(ok={c.succeeded} err={c.errored} in-flight={c.processing})")
            if mb.processing_status == "ended":
                pending.discard(bid)

    for bid in batch_ids:
        for item in client.messages.batches.results(bid):
            if item.result.type == "succeeded":
                try:
                    cache[item.custom_id] = extraction_from_message(item.result.message)
                except Exception as exc:  # noqa: BLE001
                    cache[item.custom_id] = {"__error__": str(exc)}
            else:
                cache[item.custom_id] = {"__error__": f"batch result: {item.result.type}"}
    CACHE.write_text(json.dumps(cache, indent=1))
    print(f"Extraction cache now covers {len(cache_entries(cache))} COLAs -> {CACHE}")
    return cache


def donor_brand(colas: list[dict], i: int) -> str | None:
    """A brand from a different COLA, unrelated to this one's.

    Word-overlapping donors are excluded: the registry holds multiple COLAs
    per brand family (e.g. 'EL AMO' and 'EL AMO SINGLE ESTATE REPOSADO'), and
    flagging those for review is correct behavior, not a false match.
    """
    own = _norm_brand(colas[i]["brand"])
    own_words = set(own.split())
    for step in range(1, len(colas)):
        cand = colas[(i + step) % len(colas)]["brand"]
        nc = _norm_brand(cand)
        if nc != own and not (set(nc.split()) & own_words):
            return cand
    return None


def score(colas: list[dict], cache: dict) -> dict:
    cases = []
    warning_ok_colas = 0
    colas_scored = 0

    for i, cola in enumerate(colas):
        extracted = cache.get(cola["ttbid"])
        if not extracted or "__error__" in extracted:
            cases.append({"case_id": f"{cola['ttbid']}-positive", "ttbid": cola["ttbid"],
                          "type": "positive", "expected_brand": cola["brand"],
                          "expected_outcome": "match", "actual_outcome": "error",
                          "case_passed": False,
                          "error": (extracted or {}).get("__error__", "no extraction")})
            continue
        colas_scored += 1

        def case(kind: str, expected_brand: str, expected_outcome: str) -> dict:
            status = best_brand_check(expected_brand, extracted).status
            matched = status in ("pass", "warn")
            actual = "match" if matched else "no_match"
            return {
                "case_id": f"{cola['ttbid']}-{kind}", "ttbid": cola["ttbid"],
                "type": kind, "images": cola["images"],
                "expected_brand": expected_brand,
                "expected_outcome": expected_outcome, "actual_outcome": actual,
                "case_passed": actual == expected_outcome,
                "brand_check": status,
                "extracted_brand": extracted.get("brand_name"),
                "name_candidates": extracted.get("name_candidates"),
            }

        # Positive: the label must match its own COLA's brand.
        cases.append(case("positive", cola["brand"], "match"))

        # Negative: a wrong brand must not match. The registry has no rejected
        # applications, so the failure case is synthesized with a donor brand.
        donor = donor_brand(colas, i)
        if donor:
            cases.append(case("negative", donor, "no_match"))

        # Info only: does the label carry a compliant government warning?
        gw = check_warning(extracted.get("government_warning") or {}).status
        if gw in ("pass", "warn"):
            warning_ok_colas += 1

    def rate(kind: str) -> tuple[int, int]:
        sub = [c for c in cases if c["type"] == kind and c["actual_outcome"] != "error"]
        return sum(c["case_passed"] for c in sub), len(sub)

    entries = cache_entries(cache)
    pos_ok, pos_n = rate("positive")
    neg_ok, neg_n = rate("negative")
    metrics = {
        "colas": len(colas), "colas_scored": colas_scored,
        "labels_extracted": sum(1 for v in entries.values() if "__error__" not in v),
        "extraction_errors": sum(1 for v in entries.values() if "__error__" in v),
        "positive_cases": {"passed": pos_ok, "total": pos_n},
        "negative_cases": {"passed": neg_ok, "total": neg_n},
        "accuracy": round((pos_ok + neg_ok) / max(pos_n + neg_n, 1), 4),
        "colas_with_compliant_warning": warning_ok_colas,
    }
    return {"generated": datetime.now(timezone.utc).isoformat(),
            "model": config.MODEL, "metrics": metrics, "cases": cases}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rescore", action="store_true",
                   help="skip extraction; score cached extractions only")
    args = p.parse_args()

    colas = load_json(FIXTURES / "manifest.json", None)
    if not colas:
        sys.exit("No fixtures — run scripts/fetch_cola_samples.py first.")
    cache = load_cache()

    if not args.rescore:
        cache = extract_missing(colas, cache)
    elif not cache_entries(cache):
        sys.exit("--rescore given but no usable extraction cache exists "
                 "(missing, or invalidated by a schema change).")

    report = score(colas, cache)
    RESULTS.write_text(json.dumps(report, indent=1))

    m = report["metrics"]
    print(f"\n== Eval on {m['colas_scored']}/{m['colas']} COLAs "
          f"({m['labels_extracted']} extracted, {m['extraction_errors']} errors) ==")
    print(f"  positive (label matches own COLA brand): {m['positive_cases']['passed']}/{m['positive_cases']['total']}")
    print(f"  negative (wrong brand rejected):          {m['negative_cases']['passed']}/{m['negative_cases']['total']}")
    print(f"  overall accuracy: {m['accuracy']:.1%}")
    print(f"  COLAs with a compliant government warning: "
          f"{m['colas_with_compliant_warning']}/{m['colas_scored']}")
    print(f"Per-case pass/fail metadata -> {RESULTS}")

    failures = [c for c in report["cases"] if not c["case_passed"]]
    if failures:
        print(f"\n{len(failures)} failed case(s):")
        for c in failures[:15]:
            print(f"  {c['case_id']}: expected {c['expected_outcome']}, got {c['actual_outcome']} "
                  f"(expected_brand={c['expected_brand']!r}, "
                  f"candidates={c.get('name_candidates')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
