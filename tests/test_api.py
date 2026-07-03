"""Endpoint tests with the model call stubbed out."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import config
from app.verifier import Check, Result
from tests.conftest import make_image_bytes

PASS_RESULT = Result(overall="pass", checks=[
    Check("Brand name", "pass", "Stone's Throw", "Stone's Throw", "Matches the application."),
    Check("Alcohol content", "pass", "13.5%", "13.5%", "Matches the application."),
    Check("Government warning", "pass", "Exact federal text", "GOVERNMENT WARNING: ...",
          "Present, exact, and correctly formatted."),
])


@pytest.fixture
def stub_verify(monkeypatch):
    """Replace the live model call; records the args of each invocation."""
    calls = []

    async def fake_verify(client, images, brand, abv):
        calls.append({"brand": brand, "abv": abv, "n_images": len(images)})
        return PASS_RESULT

    monkeypatch.setattr(main, "verify_label", fake_verify)
    return calls


def upload(name="label.png"):
    return ("files", (name, make_image_bytes(), "image/png"))


# --- Basics -----------------------------------------------------------------
def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["key_present"] is True


def test_index_has_no_external_requests(client):
    html = client.get("/").text
    assert "fonts.googleapis" not in html
    assert "fonts.gstatic" not in html
    assert "https://" not in html  # fully self-contained page


def test_index_links_to_batch_docs(client):
    assert 'href="/batch-api"' in client.get("/").text


def test_batch_api_docs_page(client):
    html = client.get("/batch-api").text
    assert "curl -X POST" in html
    assert 'href="/static/sample-batch.zip"' in html
    assert "mode=queued" in html
    # The zip itself is served.
    assert client.get("/static/sample-batch.zip").status_code == 200


def test_sample_batch_zip_round_trips_through_the_api(client, stub_verify):
    """The downloadable sample must always submit cleanly against /batch."""
    import io
    import zipfile
    from pathlib import Path

    zip_path = Path(main.BASE) / "static" / "sample-batch.zip"
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as z:
        manifest = z.read("manifest.json").decode()
        spec = json.loads(manifest)
        image_names = [n for n in z.namelist() if n.endswith(".jpg")]
        files = [("files", (n, z.read(n), "image/jpeg")) for n in image_names]

    # Every image is referenced by the manifest and vice versa.
    referenced = set()
    for key, entry in spec.items():
        referenced.update(entry.get("files", [key]))
    assert referenced == set(image_names)

    resp = client.post("/batch", data={"manifest": manifest}, files=files)
    body = resp.json()
    assert body["summary"]["errored"] == 0
    assert body["summary"]["total"] == len(spec)
    # The multi-panel entry produced one verification with both images.
    assert sorted(c["n_images"] for c in stub_verify) == [1, 1, 2]


# --- /check -------------------------------------------------------------------
def test_check_happy_path(client, stub_verify, png_bytes):
    resp = client.post("/check", data={"brand": "Stone's Throw", "abv": "13.5%"},
                       files={"label": ("l.png", png_bytes, "image/png")})
    assert resp.status_code == 200
    assert "Approved" in resp.text
    assert stub_verify == [{"brand": "Stone's Throw", "abv": 13.5, "n_images": 1}]


def test_check_multi_panel_label_is_one_call(client, stub_verify, png_bytes):
    # Front + back of one label -> a single verification with both images.
    resp = client.post("/check", data={"brand": "Stone's Throw"},
                       files=[("label", ("front.png", png_bytes, "image/png")),
                              ("label", ("back.png", png_bytes, "image/png"))])
    assert resp.status_code == 200
    assert "Approved" in resp.text
    assert stub_verify == [{"brand": "Stone's Throw", "abv": None, "n_images": 2}]


def test_check_too_many_panels_rejected(client, stub_verify, png_bytes, monkeypatch):
    monkeypatch.setattr(config, "MAX_IMAGES_PER_LABEL", 2)
    resp = client.post("/check", data={"brand": "X"},
                       files=[("label", (f"p{i}.png", png_bytes, "image/png"))
                              for i in range(3)])
    assert resp.status_code == 200
    assert "at most 2" in resp.text
    assert stub_verify == []


def test_check_blank_abv_passed_as_none(client, stub_verify, png_bytes):
    client.post("/check", data={"brand": "X", "abv": "  "},
                files={"label": ("l.png", png_bytes, "image/png")})
    assert stub_verify[0]["abv"] is None


def test_check_missing_key_is_visible_in_ui(monkeypatch, png_bytes):
    # A 5xx would be invisible to HTMX; the error must be a 200 fragment.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = TestClient(main.app).post(
        "/check", data={"brand": "X"},
        files={"label": ("l.png", png_bytes, "image/png")})
    assert resp.status_code == 200
    assert "ANTHROPIC_API_KEY" in resp.text


def test_check_bad_image_shows_error_fragment(client, stub_verify):
    resp = client.post("/check", data={"brand": "X"},
                       files={"label": ("l.txt", b"not an image", "text/plain")})
    assert resp.status_code == 200
    assert "readable image" in resp.text
    assert stub_verify == []


def test_check_oversized_upload_rejected_while_reading(client, stub_verify, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    resp = client.post("/check", data={"brand": "X"},
                       files={"label": ("l.png", b"x" * 4096, "image/png")})
    assert resp.status_code == 200
    assert "limit" in resp.text
    assert stub_verify == []


# --- /batch (sync) --------------------------------------------------------------
def test_batch_rejects_bad_manifest(client):
    resp = client.post("/batch", data={"manifest": "[1,2,3]"}, files=[upload()])
    assert resp.status_code == 400


def test_batch_rejects_bad_mode(client):
    resp = client.post("/batch",
                       data={"manifest": "{}", "mode": "nonsense"}, files=[upload()])
    assert resp.status_code == 400


def test_batch_sync_reconciles_manifest_and_uploads(client, stub_verify):
    manifest = json.dumps({
        "a.png": {"brand": "Stone's Throw", "abv": 13.5},
        "ghost.png": {"brand": "Never Uploaded"},
    })
    resp = client.post("/batch", data={"manifest": manifest},
                       files=[upload("a.png"), upload("orphan.png")])
    assert resp.status_code == 200
    body = resp.json()
    by_file = {r["file"]: r["result"] for r in body["results"]}

    assert by_file["a.png"]["overall"] == "pass"
    # File uploaded but absent from the manifest → reported, not fake-failed.
    assert by_file["orphan.png"]["overall"] == "error"
    assert "manifest" in by_file["orphan.png"]["error"]
    # Manifest row with no matching upload → reported, not silently dropped.
    assert by_file["ghost.png"]["overall"] == "error"
    assert "not uploaded" in by_file["ghost.png"]["error"]

    assert body["summary"] == {"total": 3, "passed": 1, "failed": 0,
                               "needs_review": 0, "errored": 2}
    assert stub_verify == [{"brand": "Stone's Throw", "abv": 13.5, "n_images": 1}]


def test_batch_sync_grouped_multi_panel_label(client, stub_verify):
    manifest = json.dumps({
        "wine-label": {"brand": "Stone's Throw", "abv": 13.5,
                       "files": ["front.png", "back.png"]},
    })
    resp = client.post("/batch", data={"manifest": manifest},
                       files=[upload("front.png"), upload("back.png")])
    body = resp.json()
    assert body["summary"] == {"total": 1, "passed": 1, "failed": 0,
                               "needs_review": 0, "errored": 0}
    assert body["results"][0]["file"] == "wine-label"
    # Both panels went into ONE verification call.
    assert stub_verify == [{"brand": "Stone's Throw", "abv": 13.5, "n_images": 2}]


def test_batch_grouped_entry_with_missing_file_errors(client, stub_verify):
    manifest = json.dumps({
        "wine-label": {"brand": "X", "files": ["front.png", "missing.png"]},
    })
    resp = client.post("/batch", data={"manifest": manifest}, files=[upload("front.png")])
    body = resp.json()
    by_file = {r["file"]: r["result"] for r in body["results"]}
    assert by_file["wine-label"]["overall"] == "error"
    assert "missing.png" in by_file["wine-label"]["error"]
    assert stub_verify == []


def test_batch_missing_key_is_json_503(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = TestClient(main.app).post("/batch", data={"manifest": "{}"}, files=[upload()])
    assert resp.status_code == 503


# --- /batch (queued via Message Batches API) ----------------------------------------
class FakeBatches:
    """Stand-in for client.messages.batches covering create/retrieve/results."""

    def __init__(self):
        self.created_requests = None
        self.status = "in_progress"
        self.result_items = []

    async def create(self, requests):
        self.created_requests = requests
        return SimpleNamespace(id="msgbatch_test", processing_status="in_progress")

    async def retrieve(self, batch_id):
        return SimpleNamespace(
            id=batch_id, processing_status=self.status,
            request_counts=SimpleNamespace(processing=0, succeeded=1, errored=0))

    async def results(self, batch_id):
        async def gen():
            for item in self.result_items:
                yield item
        return gen()


def tool_use_message(extracted: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=extracted)])


@pytest.fixture
def fake_batches(monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(main._client.messages, "batches", fake)
    main._QUEUED_BATCHES.clear()
    return fake


def test_batch_queued_roundtrip(client, fake_batches):
    manifest = json.dumps({"a.png": {"brand": "Stone's Throw", "abv": 13.5}})
    resp = client.post("/batch", data={"manifest": manifest, "mode": "queued"},
                       files=[upload("a.png")])
    assert resp.status_code == 202
    body = resp.json()
    assert body["batch_id"] == "msgbatch_test"
    assert body["queued"] == 1
    assert len(fake_batches.created_requests) == 1
    assert fake_batches.created_requests[0]["custom_id"] == "label-0"

    # Still processing → status payload, no results yet.
    polled = client.get("/batch/msgbatch_test").json()
    assert polled["status"] == "in_progress"

    # Batch ends → results are decided server-side with the stored manifest.
    fake_batches.status = "ended"
    fake_batches.result_items = [SimpleNamespace(
        custom_id="label-0",
        result=SimpleNamespace(type="succeeded", message=tool_use_message({
            "brand_name": "Stone's Throw",
            "alcohol_content_text": "13.5% ALC/VOL",
            "abv_percent": 13.5,
            "government_warning": {
                "present": True,
                "text_verbatim": config.GOVERNMENT_WARNING,
                "header_all_caps": True,
                "appears_bold": True,
            },
        })))]
    done = client.get("/batch/msgbatch_test").json()
    assert done["status"] == "ended"
    assert done["summary"]["passed"] == 1
    assert done["results"][0]["file"] == "a.png"
    assert done["results"][0]["result"]["overall"] == "pass"


def test_batch_queued_errored_request_reported(client, fake_batches):
    manifest = json.dumps({"a.png": {"brand": "X"}})
    client.post("/batch", data={"manifest": manifest, "mode": "queued"},
                files=[upload("a.png")])
    fake_batches.status = "ended"
    fake_batches.result_items = [SimpleNamespace(
        custom_id="label-0", result=SimpleNamespace(type="errored", message=None))]
    done = client.get("/batch/msgbatch_test").json()
    assert done["summary"]["errored"] == 1


def test_batch_queued_results_lost_after_restart(client, fake_batches):
    manifest = json.dumps({"a.png": {"brand": "X"}})
    client.post("/batch", data={"manifest": manifest, "mode": "queued"},
                files=[upload("a.png")])
    fake_batches.status = "ended"
    main._QUEUED_BATCHES.clear()  # simulate a server restart
    resp = client.get("/batch/msgbatch_test")
    assert resp.status_code == 409
    assert "restarted" in resp.json()["detail"]
