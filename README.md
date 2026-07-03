# TTB Label Check

A proof-of-concept that checks a beverage **label image** against the data an
importer submitted in their **application**, and confirms the mandatory federal
health warning is present and correctly formatted.

It has two entry points that share one verification core:

- A **web page** (server-rendered, HTMX) for checking a single label — built to
  be usable by a non-technical reviewer with no training.
- A **`/batch` JSON endpoint** for programmatic bulk checks (the peak-season
  case of a few hundred labels at once).

## Approach

The design principle is **the model extracts, the code decides.**

1. A vision model (Claude Sonnet) transcribes what's printed on the label into a
   strict schema via tool use — brand, alcohol content, and the warning text
   verbatim, plus whether the warning header is capitalized and bold.
2. Plain Python then makes every pass/fail decision.

Keeping judgment in code — rather than asking the model "does this pass?" — makes
results **deterministic, auditable, and tunable** without re-prompting. It matters
most for the government warning, which must match the statutory text
**word-for-word** (the requirement as stated in the brief): the model
transcribes it verbatim, and the code compares every word, in order, against
the canonical string (defined in `app/config.py`, per 27 CFR 16.21).
Punctuation and spacing are treated as typography, not wording — line wraps,
tight kerning (`WARNING:(1)` on a real approved label), and a comma lost to a
blurry bottle photo must not reject a compliant label.

The three checks:

| Field | Rule |
|---|---|
| Brand name | Compared (after normalizing case/punctuation) against **every name printed on the label** — producer, brand, product/fanciful — because applicants register either one as the brand (`NEW BELGIUM` vs `FAT TIRE`, both on the label; a real pattern surfaced by the COLA eval). An exact match on any candidate passes; a whole-word containment (brand `TX` inside `TX Experimental Series ...`) or a very close match is flagged **needs review** rather than silently passed or failed. |
| Alcohol content | Numeric match within a configurable tolerance (default: exact). If the application lists no ABV there is nothing to compare, so the check is reported as **skipped** and does not block approval. |
| Government warning | Present **and** wording matches word-for-word **and** header is ALL CAPS **and** header is bold — defined concretely as larger text size *or* heavier stroke weight than the text immediately following, evaluated as independent dimensions (A/B tested against real scans, a real bottle photo, and a synthetic non-bold control: this phrasing recognizes genuinely bold headers even on blurry curved-glass photos where "appears bold" was a coin flip, while still failing the sharp non-bold control; too-blurry-to-tell routes to **needs review**). Header capitalization is judged by the code from the verbatim transcription (e.g. `Government Warning:` in title case fails even if the model mis-reports it). |

## Quick start (local)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Run with Docker

```bash
docker build -t label-check .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... label-check
```

The image is a single portable container — it runs the same on a laptop, a
managed platform, or a plain VPS. It honors `$PORT` if the host sets one.

## Production deployment (VPS + Cloudflare Tunnel)

The intended production shape is one locked-down VPS with **zero inbound
ports**: the app container publishes nothing, a `cloudflared` sidecar makes
outbound-only connections to Cloudflare's edge (which terminates TLS), and the
firewall denies all inbound traffic except SSH. Abuse protection is built into
the app: an optional API key for `/batch` (`LABEL_CHECK_API_KEY`) and a
per-visitor-network **spend cap** on estimated model cost (IPv6 grouped at the
provider /32, so address rotation doesn't reset it). See
[`deploy/README.md`](deploy/README.md) for the full setup; day-to-day releases
are one command:

```bash
DEPLOY_HOST=deploy@your-vps ./scripts/deploy.sh
```

## Multi-panel labels

A label is often several images — front, back, neck strip. The UI file picker
accepts multiple images, and **all panels are sent to the model in one
extraction call**, so the brand can come from the front and the government
warning from the back without either panel spuriously "missing" a field.
(About a third of real labels pulled from the TTB registry are multi-panel.)

## Batch endpoint

Human-readable docs live in the app at **`/batch-api`** (linked from the main
page), including a downloadable sample bundle — real TTB registry labels plus
a ready-to-submit `manifest.json` — and the exact `curl` to run it.

`POST /batch` as `multipart/form-data`:

- `files` — label images
- `manifest` — JSON keyed by filename (single-panel labels), or by a label id
  with a `files` list (multi-panel labels checked together in one call)
- `mode` — `sync` (default) or `queued`

```bash
curl -X POST http://localhost:8000/batch \
  -F 'manifest={
        "stones_throw_red.jpg": {"brand":"Stone'\''s Throw","abv":13.5},
        "harbor-light":         {"brand":"Harbor Light","abv":6.2,
                                 "files":["hl_front.jpg","hl_back.jpg"]}
      }' \
  -F 'files=@stones_throw_red.jpg' -F 'files=@hl_front.jpg' -F 'files=@hl_back.jpg'
```

Response:

```json
{
  "summary": {"total": 1, "passed": 1, "failed": 0, "needs_review": 0, "errored": 0},
  "results": [{"file": "stones_throw_red.jpg", "result": { "overall": "pass", "checks": [ ... ] }}]
}
```

Uploads and manifest rows are reconciled both ways: a file with no manifest
entry, and a manifest entry with no uploaded file, are each reported as inline
errors rather than silently dropped. A single bad file never aborts the batch.

### Two batch modes

| | `mode=sync` (default) | `mode=queued` |
|---|---|---|
| Mechanism | Live API calls, fanned out with bounded concurrency (`LABEL_CHECK_BATCH_CONCURRENCY`, default 8) | One [Message Batch](https://platform.claude.com/docs/en/build-with-claude/batch-processing) submitted to Anthropic |
| Latency | Minutes for a few hundred labels | Usually under an hour; up to 24h |
| Token cost | Standard | **50% of standard** |
| Response | Results in the response body | `202` with a `batch_id`; poll `GET /batch/{batch_id}` |

`queued` fits the peak-season case — an importer dumps 300 labels at 5 pm,
results are ready in the morning at half the cost. The poll response carries
the same `summary`/`results` shape once processing has ended. Expected values
from the manifest are held in server memory while the batch is in flight (label
images and results themselves live on Anthropic's side for 29 days); if the
server restarts mid-batch, the poll returns `409` and the batch should be
resubmitted.

## Configuration

All optional, via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required.** |
| `LABEL_CHECK_MODEL` | `claude-sonnet-4-6` | Vision model. |
| `LABEL_CHECK_BATCH_CONCURRENCY` | `8` | Parallel calls in `/batch`. |
| `LABEL_CHECK_ABV_TOLERANCE` | `0.0` | Allowed ABV difference in percentage points. |
| `LABEL_CHECK_BRAND_WARN` | `0.90` | Similarity above which a brand near-miss is flagged for review. |
| `LABEL_CHECK_MAX_IMAGES_PER_LABEL` | `8` | Max panels per label (checked together in one call). |
| `LABEL_CHECK_API_KEY` | *(unset)* | If set, `/batch` requires `Authorization: Bearer <key>` or `X-API-Key`. Unset = open. |
| `LABEL_CHECK_SPEND_CAP_PER_IP` | `2.0` | Max estimated model spend (USD) per visitor network per rolling 24h. IPv4 per address, IPv6 per provider /32. `0` disables. |

## Testing

```bash
pip install -r requirements-dev.txt
pytest                       # unit + endpoint tests; no network, no API key
```

The unit suite covers every decision rule (brand normalization and fuzzy
matching, ABV tolerance, the exact-wording and ALL-CAPS warning checks — 
including the title-case `Government Warning:` rejection case), image
preprocessing, and both batch modes with the model call stubbed out.

### Integration tests with real TTB labels

Real approved labels can be pulled from the [TTB Public COLA Registry](https://ttbonline.gov/colasonline/publicSearchColasBasic.do)
(no API — the script drives the public HTML search):

```bash
python scripts/fetch_cola_samples.py     # downloads labels + manifest to tests/fixtures/cola/
ANTHROPIC_API_KEY=sk-ant-... pytest -m integration
```

The registry publishes only approved/expired COLAs — rejected applications are
not public — so the fixtures provide ground truth for the *pass* direction
(each label must match its own COLA brand), and the *fail* direction is
synthesized by checking the same labels against a wrong expected brand. These
tests make real model calls and are excluded from a plain `pytest` run.

### Full evaluation run

For a larger measurement than the smoke-level integration tests:

```bash
python scripts/fetch_cola_samples.py --limit 110   # ~170 label images
python scripts/run_cola_eval.py
```

Because the model only *extracts* and the code *decides*, each label needs
exactly one model call — all its panels in a single request — regardless of how
many expectations are scored against it. The eval extracts every label once
through the **Message Batches API**
(50% token cost), caches extractions in `tests/fixtures/cola/extractions.json`
(re-scoring after a rule change is free: `--rescore`; the cache is keyed to the
extraction schema and re-extracts automatically when it changes), then scores
two cases per COLA: its own brand (must match) and a donor brand from a
different COLA (must not). Per-case pass/fail metadata lands in
`tests/fixtures/cola/eval_results.json`.

**Results (110 COLAs / 166 label panels, grouped per label, `claude-sonnet-4-6`):**

| | passed | note |
|---|---|---|
| Positive cases (own brand recognized) | **104/110** | |
| Negative cases (wrong brand rejected) | **110/110** | zero false approvals |
| Overall | **97.3%** | |
| Compliant warning recognized | **97/110** | up from 82 before the word-for-word + bold-definition fixes |

The first eval round scored 92.3% and directly drove three rule changes: the
whole-word **containment** rule (registry brand `TX` printed as `TX
Experimental Series ...`), multi-candidate brand matching (`name_candidates`
in the extraction schema), because applicants register either the producer
name *or* the product name as the brand (`NEW BELGIUM` vs `FAT TIRE`, both on
the label), and word-for-word warning wording (a TTB-approved label prints
`WARNING:(1)` with no space; a bottle photo loses a comma to blur). The five
residual failures are all in the safe direction — false
"does not match", never a false approval: a stylized `SABÉ` wordmark
transcribed as `SAKE` (×3–4 depending on sampling), and two COLAs whose
registered brand isn't legibly printed on the label at all (`KL` monogram for
KENTUCKY LEGEND; CASTANNOVE). A human reviewer gets those, which is the point
of the screening design.

## Scope & assumptions

- **Standalone POC.** No integration with the existing COLA system and no data
  persistence — nothing about a label is stored after the response is returned.
  (Exceptions, both in-memory only: `mode=queued` keeps a batch's expected
  values while it is in flight, and the spend cap keeps a rolling ledger of
  estimated cost per visitor network. A restart forgets both.)
- **Three checks on purpose.** Brand, ABV, and the government warning are the
  checks named in the discovery interviews. Other label requirements (class/type,
  net contents, bottler name and address, country of origin) are deliberately
  out of scope for the POC; the extraction schema extends naturally to them.
- **Latency.** A single check is one Sonnet vision call — typically a few
  seconds end-to-end, in line with the reviewers' ~5-second usability ceiling.
  The sync batch path bounds concurrency rather than latency.
- **"Bold" is a visual judgment.** Capitalization is verified in code from the
  transcribed text; boldness is not verifiable from text, so the model reports
  it as a separate signal and an unconfirmed bold is surfaced as *needs review*
  rather than a hard pass.
- **Human-in-the-loop by design.** Borderline cases are flagged, not auto-decided.
  This is a screening aid, not a final authority.
- **Deployment note.** A production deployment inside a restricted network must
  allowlist outbound access to `api.anthropic.com` — nothing else. htmx is
  vendored locally (`app/static/htmx.min.js`) and the UI uses native system
  font stacks, so the browser makes no external requests at all.
