#!/usr/bin/env python3
"""Download real label images from the TTB Public COLA Registry for tests.

The registry (https://ttbonline.gov/colasonline/) has no API, but the public
search is plain server-rendered HTML:

  1. GET  publicSearchColasBasic.do                    -> session cookie
  2. POST publicSearchColasBasicProcess.do?action=search
         (searchCriteria.dateCompletedFrom/To, productOrFancifulName, ...)
                                                       -> rows of TTB IDs
  3. GET  viewColaDetails.do?action=publicFormDisplay&ttbid=<id>
                                                       -> printable form with
                                                          brand name, status,
                                                          and label <img> tags
  4. GET  publicViewAttachment.do?filename=<f>&filetype=l -> label JPEG

Only *approved* (and later expired/surrendered) COLAs are public — rejected
applications never appear in the registry. So these fixtures give us real
ground truth for the "should pass" direction, and the integration tests
synthesize failures by checking the same labels against wrong expected values.

Usage:
  python scripts/fetch_cola_samples.py                 # 4 recent COLAs
  python scripts/fetch_cola_samples.py --limit 8 --name "IPA"
  python scripts/fetch_cola_samples.py --from 01/01/2026 --to 06/01/2026

Writes images + manifest.json to tests/fixtures/cola/ (override with --out).
Then: pytest -m integration
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

BASE = "https://ttbonline.gov/colasonline"
UA = "label-check-poc/0.1 (test fixture collection; contact: repo owner)"

TTBID_RE = re.compile(r"viewColaDetails\.do\?action=publicDisplaySearchBasic&(?:amp;)?ttbid=(\d+)")
IMAGE_RE = re.compile(r"publicViewAttachment\.do\?filename=([^\"'&]+)&(?:amp;)?filetype=l")
TAG_RE = re.compile(r"<[^>]+>")


def text_lines(page: str) -> list[str]:
    lines = [html.unescape(l).strip() for l in TAG_RE.sub("\n", page).splitlines()]
    return [l for l in lines if l]


def field_after(lines: list[str], label: str, skip: tuple[str, ...] = ("(Required)", "(If any)")) -> str | None:
    """Value of a form field: the first content line after its numbered label."""
    for i, line in enumerate(lines):
        if line.upper().startswith(label.upper()):
            for candidate in lines[i + 1:i + 4]:
                if candidate in skip:
                    continue
                # The next numbered label means the field was blank.
                if re.match(r"^\d+[a-z]?\.", candidate):
                    return None
                return candidate
    return None


def make_client(insecure: bool) -> httpx.Client:
    # ttbonline.gov serves an incomplete certificate chain, so strict
    # verification usually fails. We try strict first and fall back loudly.
    return httpx.Client(base_url=BASE, verify=not insecure, timeout=30,
                        headers={"User-Agent": UA}, follow_redirects=True)


def fetch_all(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = make_client(insecure=False)
        client.get("/publicSearchColasBasic.do")
    except httpx.ConnectError:
        print("note: strict TLS verification failed (ttbonline.gov serves an "
              "incomplete chain); retrying without verification.", file=sys.stderr)
        client.close()
        client = make_client(insecure=True)
        client.get("/publicSearchColasBasic.do")

    try:
        resp = client.post(
            "/publicSearchColasBasicProcess.do", params={"action": "search"},
            data={
                "searchCriteria.dateCompletedFrom": args.date_from,
                "searchCriteria.dateCompletedTo": args.date_to,
                "searchCriteria.productOrFancifulName": args.name or "",
                "searchCriteria.productNameSearchType": "E",
                "searchCriteria.classTypeFrom": "",
                "searchCriteria.classTypeTo": "",
                "searchCriteria.originCode": "",
            })
        resp.raise_for_status()
        ttbids = list(dict.fromkeys(TTBID_RE.findall(resp.text)))
        if not ttbids:
            print("No results — widen the date range or drop --name.", file=sys.stderr)
            return 1

        # Results are 20 per page; paging state lives in the session. Collect a
        # buffer beyond --limit since some COLAs lack a brand or usable images.
        want = int(args.limit * 1.5) + 10
        while len(ttbids) < want:
            time.sleep(args.delay)
            page = client.get("/publicPageBasicCola.do",
                              params={"action": "page", "pgfcn": "nextset"})
            found = [t for t in TTBID_RE.findall(page.text) if t not in set(ttbids)]
            if not found:
                break  # ran out of pages
            ttbids.extend(found)
        print(f"Collected {len(ttbids)} candidate COLAs; fetching up to {args.limit}.")

        colas = []
        for ttbid in ttbids:
            if len(colas) >= args.limit:
                break
            time.sleep(args.delay)  # be polite to a government JSP app
            form = client.get("/viewColaDetails.do",
                              params={"action": "publicFormDisplay", "ttbid": ttbid})
            if form.status_code != 200:
                continue
            lines = text_lines(form.text)
            brand = field_after(lines, "6. BRAND NAME")
            image_files = list(dict.fromkeys(IMAGE_RE.findall(form.text)))
            if not brand or not image_files:
                continue

            saved = []
            for filename in image_files:
                time.sleep(args.delay)
                img = client.get("/publicViewAttachment.do",
                                 params={"filename": filename, "filetype": "l"})
                if img.status_code != 200 or not img.content[:4].startswith(b"\xff\xd8"):
                    continue  # not a JPEG; skip oddities
                local = f"{ttbid}-{len(saved)}.jpg"
                (out_dir / local).write_bytes(img.content)
                saved.append(local)
            if not saved:
                continue

            status = field_after(lines, "THE STATUS IS")
            colas.append({
                "ttbid": ttbid,
                "brand": brand,
                "fanciful_name": field_after(lines, "7. FANCIFUL NAME"),
                # Registry status of this COLA (APPROVED / EXPIRED / ...).
                # Note: only ever approved-family statuses — rejected
                # applications are not published in the public registry.
                "registry_status": (status or "").rstrip(".").strip() or None,
                "images": saved,
                "source": f"{BASE}/viewColaDetails.do?action=publicDisplaySearchBasic&ttbid={ttbid}",
            })
            print(f"  {ttbid}: {brand!r} ({len(saved)} image(s), {status})")
    finally:
        client.close()

    if not colas:
        print("No usable COLAs found (all lacked brand or images).", file=sys.stderr)
        return 1
    (out_dir / "manifest.json").write_text(json.dumps(colas, indent=2))
    print(f"Wrote {len(colas)} COLAs to {out_dir}/manifest.json")
    return 0


def main() -> int:
    today = date.today()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="date_from", default=(today - timedelta(days=180)).strftime("%m/%d/%Y"),
                   help="dateCompletedFrom, MM/DD/YYYY (default: 180 days ago)")
    p.add_argument("--to", dest="date_to", default=today.strftime("%m/%d/%Y"),
                   help="dateCompletedTo, MM/DD/YYYY (default: today)")
    p.add_argument("--name", default="", help="brand/fanciful name filter (optional)")
    p.add_argument("--limit", type=int, default=4, help="max COLAs to download (default 4)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    p.add_argument("--out", default="tests/fixtures/cola", help="output directory")
    return fetch_all(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
