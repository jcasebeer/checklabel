"""FastAPI application: SSR UI + programmatic batch endpoints.

All entry points call the same verification core. A "label" may span several
image panels (front/back/neck); all panels of one label go into a single
extraction call. The single-label route returns an HTML fragment for HTMX;
the batch routes return JSON. Batch checks run in one of two modes:

  - sync (default): fan out live API calls with bounded concurrency and
    return results in the response. Right for interactive use.
  - queued: submit one Anthropic Message Batch (50% token cost, results
    usually within an hour) and poll GET /batch/{id} for the outcome.
    Right for large non-urgent runs.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .images import ImageError, prepare_image
from .spend import SpendLedger, bucket_for_ip, estimate_usd, usage_usd
from .verifier import (
    Images,
    Result,
    decide,
    extract_request_params,
    extraction_from_message,
    verify_label,
)

BASE = Path(__file__).parent
app = FastAPI(title="TTB Label Check")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

# One shared async client for the process. The SDK retries 429/5xx and
# connection errors with backoff; 4xx errors fail fast.
_client = anthropic.AsyncAnthropic(
    timeout=config.API_TIMEOUT_SECONDS,
    max_retries=config.API_MAX_RETRIES,
)

_MISSING_KEY_MSG = "Server is missing ANTHROPIC_API_KEY. Set it and restart."

# Expected values for queued batches, keyed by batch id then custom_id.
# In-memory on purpose: nothing about a label is persisted to disk, and the
# extraction results themselves stay retrievable from Anthropic for 29 days.
# A restart only loses the expected-value mapping for in-flight batches.
_QUEUED_BATCHES: dict[str, dict[str, dict]] = {}

# Estimated model spend per client network over a rolling 24h (see app/spend.py).
_ledger = SpendLedger()

_CAP_MSG = ("This deployment limits model spend per visitor network and yours "
            "is used up for now. Try again later.")


def _key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _require_key() -> None:
    if not _key_present():
        raise HTTPException(status_code=503, detail=_MISSING_KEY_MSG)


def _require_api_key(request: Request) -> None:
    """Enforce the optional app-level API key on the batch endpoints."""
    expected = config.API_KEY
    if not expected:
        return  # no key configured: endpoint is open
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else \
        request.headers.get("x-api-key", "")
    if supplied != expected:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key. Send 'Authorization: Bearer <key>' "
                   "or 'X-API-Key: <key>'.")


def _spend_bucket(request: Request) -> str:
    # Cloudflare sets CF-Connecting-IP at the edge; behind the tunnel that's
    # the only hop, and the container is not directly reachable. Fall back to
    # the (proxy-header-resolved) socket address for local/un-fronted runs.
    ip = request.headers.get("cf-connecting-ip") or \
        (request.client.host if request.client else "unknown")
    return bucket_for_ip(ip)


async def _read_limited(upload: UploadFile) -> bytes:
    """Read an upload without buffering more than the size limit in memory."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > config.MAX_UPLOAD_BYTES:
            mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
            raise ImageError(f"That image is larger than the {mb} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _prepare_uploads(uploads: list[UploadFile]) -> Images:
    """Prepare every panel of one label; raises ImageError naming the file."""
    if len(uploads) > config.MAX_IMAGES_PER_LABEL:
        raise ImageError(f"A label can have at most {config.MAX_IMAGES_PER_LABEL} "
                         f"images; got {len(uploads)}.")
    images: Images = []
    for upload in uploads:
        try:
            raw = await _read_limited(upload)
            images.append(prepare_image(raw))
        except ImageError as exc:
            raise ImageError(f"{upload.filename}: {exc}") from exc
    return images


def _parse_expected_abv(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except ValueError:
        return None


def _result_dict(r: Result) -> dict:
    return {
        "overall": r.overall,
        "error": r.error,
        "checks": [
            {"field": c.field, "status": c.status, "expected": c.expected,
             "found": c.found, "detail": c.detail}
            for c in r.checks
        ],
        "extracted": r.extracted,
    }


def _summary(results: list[dict]) -> dict:
    return {"total": len(results),
            "passed": sum(1 for x in results if x["result"]["overall"] == "pass"),
            "failed": sum(1 for x in results if x["result"]["overall"] == "fail"),
            "needs_review": sum(1 for x in results if x["result"]["overall"] == "warn"),
            "errored": sum(1 for x in results if x["result"]["overall"] == "error")}


def _parse_manifest(manifest: str) -> dict:
    try:
        spec = json.loads(manifest)
        assert isinstance(spec, dict)
        return spec
    except (json.JSONDecodeError, AssertionError):
        raise HTTPException(status_code=400, detail="manifest must be a JSON object keyed by filename or label id.")


def _error_entry(label: str, message: str) -> dict:
    return {"file": label, "result": _result_dict(Result(overall="error", error=message))}


def _build_groups(spec: dict, files: list[UploadFile]) -> tuple[list[dict], list[dict]]:
    """Reconcile the manifest with the uploads into label groups.

    Manifest entries come in two shapes:
      "front.jpg":  {"brand": ..., "abv": ...}                      # one file
      "label-1":    {"brand": ..., "abv": ..., "files": [...]}      # multi-panel

    Returns (groups, errors). Every upload must be referenced by exactly one
    entry and every referenced file must be uploaded; violations become
    inline error results rather than silent drops.
    """
    by_name: dict[str, UploadFile] = {}
    for f in files:
        by_name.setdefault(f.filename, f)

    groups: list[dict] = []
    errors: list[dict] = []
    claimed: set[str] = set()

    for key, entry in spec.items():
        entry = entry if isinstance(entry, dict) else {}
        filenames = entry.get("files") if isinstance(entry.get("files"), list) else [key]
        missing = [n for n in filenames if n not in by_name]
        claimed.update(n for n in filenames if n in by_name)
        if missing:
            errors.append(_error_entry(
                key, "Listed in the manifest but not uploaded: " + ", ".join(missing) + "."))
            continue
        if len(filenames) > config.MAX_IMAGES_PER_LABEL:
            errors.append(_error_entry(
                key, f"A label can have at most {config.MAX_IMAGES_PER_LABEL} images."))
            continue
        groups.append({
            "label": key,
            "uploads": [by_name[n] for n in filenames],
            "brand": str(entry.get("brand", "")).strip(),
            "abv": _parse_expected_abv(entry.get("abv")),
        })

    for f in files:
        if f.filename not in claimed:
            errors.append(_error_entry(
                f.filename, "No manifest entry for this file, so there is nothing to check it against."))
    return groups, errors


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/batch-api", response_class=HTMLResponse)
async def batch_api_docs(request: Request):
    """Human-readable docs for the batch endpoint, with a sample bundle."""
    return templates.TemplateResponse(
        request, "batch_api.html", {"base_url": str(request.base_url)})


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "model": config.MODEL, "key_present": _key_present()}


@app.post("/check", response_class=HTMLResponse)
async def check(
    request: Request,
    label: list[UploadFile] = File(...),
    brand: str = Form(...),
    abv: str = Form(""),
):
    """Single label check (one or more panels). Returns an HTML fragment."""

    def error_fragment(message: str):
        # Errors render as fragments too — HTMX doesn't swap non-2xx
        # responses, so an HTTP error here would be invisible to the user.
        return templates.TemplateResponse(
            request, "partials/result.html",
            {"result": Result(overall="error", error=message)},
        )

    if not _key_present():
        return error_fragment(_MISSING_KEY_MSG)
    bucket = _spend_bucket(request)
    if _ledger.would_exceed(bucket, estimate_usd(len(label))):
        return error_fragment(_CAP_MSG)
    try:
        images = await _prepare_uploads(label)
    except ImageError as exc:
        return error_fragment(str(exc))

    expected_abv = _parse_expected_abv(abv) if abv.strip() else None
    result = await verify_label(_client, images, brand.strip(), expected_abv)
    _ledger.charge(bucket, usage_usd(result.usage))
    return templates.TemplateResponse(
        request, "partials/result.html", {"result": result}
    )


@app.post("/batch")
async def batch(
    request: Request,
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    mode: str = Form("sync"),
):
    """Programmatic batch endpoint.

    Send multipart/form-data with:
      - files:    label images
      - manifest: JSON keyed by filename (single-panel labels) or by a label id
                  with a "files" list (multi-panel labels), e.g.
                  {"a.jpg": {"brand": "Stone's Throw", "abv": 13.5},
                   "label-2": {"brand": "Harbor Light", "files": ["front.jpg", "back.jpg"]}}
      - mode:     "sync" (default) or "queued" (Anthropic Message Batches API)

    sync returns {"summary": ..., "results": [...]} directly. queued returns
    {"batch_id": ...}; poll GET /batch/{batch_id} for the same result shape.
    Per-label failures are reported inline and never abort the whole batch.
    """
    _require_key()
    _require_api_key(request)
    if mode not in ("sync", "queued"):
        raise HTTPException(status_code=400, detail="mode must be 'sync' or 'queued'.")
    spec = _parse_manifest(manifest)
    groups, errors = _build_groups(spec, files)

    # Pre-flight spend check on the whole submission; sync charges actual
    # usage afterwards, queued charges the estimate at submit time.
    bucket = _spend_bucket(request)
    n_images = sum(len(g["uploads"]) for g in groups)
    if _ledger.would_exceed(bucket, estimate_usd(n_images, batch_pricing=(mode == "queued"))):
        raise HTTPException(status_code=429, detail=_CAP_MSG)

    if mode == "queued":
        return await _batch_submit(groups, errors, bucket)

    sem = asyncio.Semaphore(config.BATCH_CONCURRENCY)

    async def one(group: dict) -> dict:
        async with sem:
            try:
                images = await _prepare_uploads(group["uploads"])
            except ImageError as exc:
                return _error_entry(group["label"], str(exc))
            r = await verify_label(_client, images, group["brand"], group["abv"])
            _ledger.charge(bucket, usage_usd(r.usage))
            return {"file": group["label"], "result": _result_dict(r)}

    results = list(await asyncio.gather(*(one(g) for g in groups))) + errors
    return JSONResponse({"summary": _summary(results), "results": results})


async def _batch_submit(groups: list[dict], errors: list[dict], bucket: str):
    """Queue one Message Batch for the label groups and remember expected values."""
    requests: list[BatchRequest] = []
    entries: dict[str, dict] = {}
    prep_failures: list[dict] = list(errors)
    queued_images = 0

    for i, group in enumerate(groups):
        try:
            images = await _prepare_uploads(group["uploads"])
        except ImageError as exc:
            prep_failures.append(_error_entry(group["label"], str(exc)))
            continue
        queued_images += len(images)
        custom_id = f"label-{i}"  # label ids can contain chars custom_id forbids
        entries[custom_id] = {"file": group["label"], "brand": group["brand"],
                              "abv": group["abv"]}
        requests.append(BatchRequest(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(**extract_request_params(images)),
        ))

    if not requests:
        return JSONResponse({"summary": _summary(prep_failures), "results": prep_failures})

    mb = await _client.messages.batches.create(requests=requests)
    # Spend is committed the moment the batch is accepted; charge the estimate
    # now (actual usage only becomes known when results are fetched).
    _ledger.charge(bucket, estimate_usd(queued_images, batch_pricing=True))
    _QUEUED_BATCHES[mb.id] = {"entries": entries, "prep_failures": prep_failures}
    return JSONResponse({
        "batch_id": mb.id,
        "status": mb.processing_status,
        "queued": len(requests),
        "failed_before_queue": len(prep_failures),
        "results_url": f"/batch/{mb.id}",
    }, status_code=202)


@app.get("/batch/{batch_id}")
async def batch_results(request: Request, batch_id: str):
    """Poll a queued batch. Returns processing status until it has ended, then
    the same {"summary": ..., "results": [...]} shape as the sync mode."""
    _require_key()
    _require_api_key(request)
    try:
        mb = await _client.messages.batches.retrieve(batch_id)
    except anthropic.NotFoundError:
        raise HTTPException(status_code=404, detail="No such batch.")

    if mb.processing_status != "ended":
        return {"batch_id": batch_id, "status": mb.processing_status,
                "counts": {"processing": mb.request_counts.processing,
                           "succeeded": mb.request_counts.succeeded,
                           "errored": mb.request_counts.errored}}

    stored = _QUEUED_BATCHES.get(batch_id)
    if stored is None:
        raise HTTPException(
            status_code=409,
            detail="The server restarted since this batch was submitted, so the "
                   "expected values from its manifest are gone. Resubmit the batch.",
        )

    entries = stored["entries"]
    results: list[dict] = list(stored["prep_failures"])
    async for item in await _client.messages.batches.results(batch_id):
        entry = entries.get(item.custom_id, {"file": item.custom_id, "brand": "", "abv": None})
        if item.result.type == "succeeded":
            try:
                extracted = extraction_from_message(item.result.message)
                r = decide(extracted, entry["brand"], entry["abv"])
            except Exception as exc:  # noqa: BLE001 - report per-label, keep the batch going
                r = Result(overall="error", error=str(exc))
        else:
            r = Result(overall="error", error=f"Model request {item.result.type}.")
        results.append({"file": entry["file"], "result": _result_dict(r)})

    return JSONResponse({"batch_id": batch_id, "status": "ended",
                         "summary": _summary(results), "results": results})
