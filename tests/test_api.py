"""The HTTP service, end to end on the sample dossier (model replies replayed from the reference labels)."""

import json

import pytest
from fastapi.testclient import TestClient

from credit_review import api
from credit_review.storage import LocalStore
from tests.conftest import DOSSIER
from tests.test_engine import DECISION, EXPECTED

KEY = "test-key"


@pytest.fixture
def client(monkeypatch, tmp_path, golden_llm, policy):
    monkeypatch.setenv("CREDIT_API_KEY", KEY)
    store = LocalStore(tmp_path / "dossiers")
    api.app.dependency_overrides = {api.get_store: lambda: store, api.get_llm: lambda: golden_llm}
    with TestClient(api.app, headers={"X-API-Key": KEY}) as c:
        c.store = store          # type: ignore[attr-defined]
        yield c
    api.app.dependency_overrides = {}


@pytest.fixture
def upload(golden, docs):
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in sorted(DOSSIER.glob("*.pdf"))]
    form = {"types": json.dumps(types), "decision_date": DECISION, "judgment": "false"}
    return files, form


def test_a_dossier_is_assessed_in_the_background(client, upload):
    files, form = upload
    r = client.post("/dossiers", files=files, data=form)
    assert r.status_code == 202, r.text
    dossier_id = r.json()["id"]

    status = client.get(f"/dossiers/{dossier_id}").json()   # TestClient runs background tasks before returning
    assert status["state"] == "done", status
    assert status["summary"]["model_calls"] == 10 and status["summary"]["cost"] is None   # replay has no price

    assessment = client.get(f"/dossiers/{dossier_id}/assessment").json()
    assert {a["id"]: a["outcome"] for a in assessment["articles"]} == EXPECTED


def test_the_same_dossier_is_not_assessed_twice_unless_asked(client, upload):
    files, form = upload
    first = client.post("/dossiers", files=files, data=form).json()["id"]
    again = client.post("/dossiers", files=files, data=form)
    assert again.status_code == 200 and again.json()["id"] == first and again.json()["state"] == "done"

    rerun = client.post("/dossiers?rerun=true", files=files, data=form)
    assert rerun.status_code == 202 and rerun.json()["id"] == first
    summary = client.get(f"/dossiers/{first}").json()["summary"]
    assert summary["cached_calls"] == summary["model_calls"]      # every reply came from the cache


def test_other_dates_make_another_dossier(client, upload):
    files, form = upload
    a = client.post("/dossiers", files=files, data=form).json()["id"]
    b = client.post("/dossiers", files=files, data={**form, "decision_date": "2026-10-20"}).json()["id"]
    assert a != b


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_the_api_key_is_required(client, headers):
    client.headers.pop("X-API-Key")
    r = client.get("/dossiers/" + "0" * 24, headers=headers)
    assert r.status_code == 401


def test_no_configured_key_means_closed(client, monkeypatch):
    monkeypatch.delenv("CREDIT_API_KEY")
    assert client.get("/dossiers/" + "0" * 24).status_code == 503


def test_health_needs_no_key(client):
    client.headers.pop("X-API-Key")
    assert client.get("/healthz").json() == {"status": "ok"}


def test_uploads_are_checked(client):
    pdf = b"%PDF-1.7 tiny"
    assert client.post("/dossiers", files=[("files", ("a.txt", b"hello", "text/plain"))]).status_code == 415
    too_many = [("files", (f"d{i}.pdf", pdf, "application/pdf")) for i in range(api.MAX_FILES + 1)]
    assert client.post("/dossiers", files=too_many).status_code == 413
    twice = [("files", ("a.pdf", pdf, "application/pdf")), ("files", ("sub/a.pdf", pdf, "application/pdf"))]
    assert client.post("/dossiers", files=twice).status_code == 422
    one = [("files", ("a.pdf", pdf, "application/pdf"))]
    assert client.post("/dossiers", files=one, data={"decision_date": "12/10/2026"}).status_code == 422
    assert client.post("/dossiers", files=one, data={"types": "[1]"}).status_code == 422


def test_unknown_or_malformed_ids_are_not_found(client):
    assert client.get("/dossiers/" + "a" * 24).status_code == 404
    assert client.get("/dossiers/../../etc").status_code == 404


def test_a_broken_document_fails_the_assessment_not_the_service(client):
    r = client.post("/dossiers", files=[("files", ("broken.pdf", b"%PDF-1.7 not really", "application/pdf"))],
                    data={"types": "{}"})
    status = client.get(f"/dossiers/{r.json()['id']}").json()
    assert status["state"] == "failed" and status["error"]
    assert client.get(f"/dossiers/{r.json()['id']}/assessment").status_code == 409


def test_a_run_that_stopped_moving_is_stale_and_can_be_posted_again(client, upload, monkeypatch):
    files, form = upload
    dossier_id = client.post("/dossiers", files=files, data=form).json()["id"]
    status = json.loads(client.store.get(f"{dossier_id}/status.json"))
    status.update(state="running", updated_at="2026-01-01T00:00:00+00:00")
    client.store.put(f"{dossier_id}/status.json", json.dumps(status).encode())
    assert client.get(f"/dossiers/{dossier_id}").json()["state"] == "stale"
    assert client.post("/dossiers", files=files, data=form).status_code == 202     # resumes without ?rerun
    assert client.get(f"/dossiers/{dossier_id}").json()["state"] == "done"
