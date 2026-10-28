"""The HTTP service: upload a dossier, get the assessment.

    POST /dossiers                    PDFs (multipart) → 202 {id, status_url}; the assessment runs in the background
    GET  /dossiers/{id}               pending | running | done | failed | stale, with the latest progress message
    GET  /dossiers/{id}/assessment    the full assessment (the same JSON the CLI writes)
    GET  /healthz, /readyz            liveness and readiness (no key needed)

    uvicorn credit_review.api:app --port 8000          # locally; CREDIT_API_KEY must be set

The dossier id is content-addressed (documents, policy, dates, types), so posting the same dossier again
returns the stored result instead of paying for it twice; `?rerun=true` assesses it again, which costs nothing
either because every model reply is cached.

The assessment runs as a FastAPI background task in the same replica. If that replica stops mid-run (scale-in,
a new revision), the status stops moving and is reported as `stale` after STALE_AFTER; posting again resumes
from the model cache. A queue and a worker job are the production answer (docs/adr/0002-container-apps.md).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections import Counter
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from credit_review import telemetry

telemetry.setup()   # before FastAPI is imported, so its requests are instrumented too

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from opentelemetry import trace  # noqa: E402

from credit_review.classify import classify_documents  # noqa: E402
from credit_review.llm import LLMClient, LLMConfig  # noqa: E402
from credit_review.pdf import list_dossier  # noqa: E402
from credit_review.pipeline import assess  # noqa: E402
from credit_review.policy import Policy, load_policy  # noqa: E402
from credit_review.security import require_key  # noqa: E402
from credit_review.storage import Store, store_from_env  # noqa: E402

log = logging.getLogger("credit_review.api")
tracer = trace.get_tracer(__name__)

POLICY_FILE = Path("policy") / "example_bank_v3.2.yaml"
MAX_FILES = 25
MAX_BYTES = 15 * 1024 * 1024
STALE_AFTER = 15 * 60          # seconds without progress before a running assessment is reported as stale
ID = re.compile(r"^[0-9a-f]{24}$")
_status_lock = threading.Lock()   # progress arrives from the extraction threads; one status write at a time

app = FastAPI(
    title="Credit Review API",
    version="0.1.0",
    description="Reads a mortgage dossier, checks it against the credit policy, and reports every finding with its "
                "evidence. AI reads, rules decide, analysts conclude.",
)


# ------------------------------------------------------------------ dependencies (overridden in tests)

def policy_path() -> Path:
    """CREDIT_REVIEW_POLICY, else the policy next to the working directory (the CLI's default; /app in the image),
    else the one in the source tree. The package itself may be installed anywhere, so no path is relative to it."""
    if os.environ.get("CREDIT_REVIEW_POLICY"):
        return Path(os.environ["CREDIT_REVIEW_POLICY"])
    if POLICY_FILE.exists():
        return POLICY_FILE
    return Path(__file__).resolve().parents[1] / POLICY_FILE


@lru_cache(maxsize=1)
def get_policy() -> tuple[Policy, str]:
    report = load_policy(policy_path())
    if not report.ok or report.policy is None:
        raise RuntimeError("policy is not valid: " + "; ".join(report.errors[:3]))
    return report.policy, report.sha256


@lru_cache(maxsize=1)
def get_store() -> Store:
    return store_from_env("dossiers", Path(os.environ.get("CREDIT_REVIEW_DATA_DIR", ".credit_review_data")) / "dossiers")


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    config = LLMConfig.from_env()
    return LLMClient(config, store=store_from_env("llm-cache", config.cache_dir))


StoreDep = Annotated[Store, Depends(get_store)]


# ------------------------------------------------------------------ status

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(store: Store, key: str) -> dict[str, Any] | None:
    data = store.get(key)
    return json.loads(data) if data is not None else None


def _write_status(store: Store, dossier_id: str, **changes: Any) -> dict[str, Any]:
    with _status_lock:
        status = _read_json(store, f"{dossier_id}/status.json") or {"id": dossier_id, "created_at": _now()}
        status.update(changes, updated_at=_now())
        store.put(f"{dossier_id}/status.json", json.dumps(status, ensure_ascii=False).encode())
        return status


def _visible(status: dict[str, Any]) -> dict[str, Any]:
    """The status as clients see it: a run that stopped moving is `stale`."""
    if status.get("state") in ("pending", "running"):
        age = datetime.now(UTC) - datetime.fromisoformat(status["updated_at"])
        if age.total_seconds() > STALE_AFTER:
            return {**status, "state": "stale"}
    return status


def _links(dossier_id: str) -> dict[str, str]:
    return {"status_url": f"/dossiers/{dossier_id}", "assessment_url": f"/dossiers/{dossier_id}/assessment"}


# ------------------------------------------------------------------ the background task

def run_assessment(dossier_id: str, store: Store, llm: LLMClient, policy: Policy, policy_sha: str) -> None:
    with tracer.start_as_current_span("assess") as span:
        span.set_attribute("credit_review.dossier_id", dossier_id)
        try:
            request = _read_json(store, f"{dossier_id}/request.json") or {}
            _write_status(store, dossier_id, state="running", progress="Starting", error=None)
            with tempfile.TemporaryDirectory(prefix="dossier-") as folder:
                for name in request["documents"]:
                    data = store.get(f"{dossier_id}/documents/{name}")
                    if data is None:
                        raise RuntimeError(f"uploaded document {name} is missing from storage")
                    (Path(folder) / name).write_bytes(data)
                docs = list_dossier(folder)
                span.set_attribute("credit_review.documents", len(docs))
                types = request.get("types") or {p.doc: p.proposed_type for p in classify_documents(docs, policy, llm)}

                def progress(message: str) -> None:
                    _write_status(store, dossier_id, progress=message)

                assessment = assess(
                    policy, policy_sha, docs, types, llm, request.get("decision_date"), request.get("application_date"),
                    run_judgment=request.get("judgment", True), progress=progress)
            store.put(f"{dossier_id}/assessment.json", assessment.model_dump_json(indent=2).encode())
            calls = [e.llm for e in assessment.extractions if e.llm]
            fresh = [c for c in calls if not c.cached]
            outcomes = dict(Counter(a.outcome for a in assessment.articles))
            costs = [c.cost for c in fresh]
            summary = {
                "outcomes": outcomes, "headline": assessment.summary.get("headline", []),
                "model_calls": len(calls), "cached_calls": len(calls) - len(fresh),
                # what this run paid; 0 when every reply came from the cache, None when a price is unknown
                "cost": round(sum(c for c in costs if c is not None), 4) if None not in costs else None,
            }
            span.set_attributes({f"credit_review.outcome.{k}": v for k, v in outcomes.items()})
            _write_status(store, dossier_id, state="done", progress="Done", summary=summary)
        except Exception as exc:
            log.exception("assessment %s failed", dossier_id)
            span.record_exception(exc)
            _write_status(store, dossier_id, state="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")


# ------------------------------------------------------------------ endpoints

def _safe_name(name: str | None) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if not base:
        raise HTTPException(422, "every file needs a name")
    return base if base.lower().endswith(".pdf") else base + ".pdf"


def _iso_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, f"{field} must be a date (YYYY-MM-DD)") from None


@app.post("/dossiers", status_code=202, dependencies=[Depends(require_key)],
          summary="Upload a dossier and start its assessment")
async def create_dossier(
    background: BackgroundTasks,
    store: StoreDep,
    llm: Annotated[LLMClient, Depends(get_llm)],
    policy: Annotated[tuple[Policy, str], Depends(get_policy)],
    files: Annotated[list[UploadFile], File(description="The dossier's PDF documents")],
    types: Annotated[str | None, Form(description='Optional JSON {"file.pdf": "document type"}; '
                                                  "otherwise the types are detected")] = None,
    decision_date: Annotated[str | None, Form()] = None,
    application_date: Annotated[str | None, Form()] = None,
    judgment: Annotated[bool, Form(description="Run the judgment checks (model calls)")] = True,
    rerun: Annotated[bool, Query(description="Assess again even if a result exists")] = False,
) -> JSONResponse:
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"at most {MAX_FILES} files per dossier")
    documents: dict[str, bytes] = {}
    for f in files:
        data = await f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"each file must be at most {MAX_BYTES // (1024 * 1024)} MB")
        if not data.startswith(b"%PDF-"):
            raise HTTPException(415, "only PDF documents are accepted")
        name = _safe_name(f.filename)
        if name in documents:
            raise HTTPException(422, f"two files are named {name}")
        documents[name] = data
    try:
        type_map = json.loads(types) if types else None
    except json.JSONDecodeError:
        raise HTTPException(422, "types must be a JSON object") from None
    if type_map is not None and not (isinstance(type_map, dict)
                                     and all(isinstance(v, str) for v in type_map.values())):
        raise HTTPException(422, "types must be a JSON object of file name → document type")

    policy_obj, policy_sha = policy
    request = {
        "documents": sorted(documents), "types": type_map, "judgment": judgment,
        "decision_date": _iso_date(decision_date, "decision_date"),
        "application_date": _iso_date(application_date, "application_date"),
    }
    fingerprint = {**request, "policy": policy_sha,
                   "hashes": {n: hashlib.sha256(d).hexdigest() for n, d in documents.items()}}
    dossier_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:24]

    existing = _read_json(store, f"{dossier_id}/status.json")
    if existing and not rerun and _visible(existing)["state"] in ("pending", "running", "done"):
        code = 200 if existing["state"] == "done" else 202
        return JSONResponse({**_visible(existing), **_links(dossier_id)}, status_code=code)

    for name, data in documents.items():
        store.put(f"{dossier_id}/documents/{name}", data)
    store.put(f"{dossier_id}/request.json", json.dumps(request).encode())
    status = _write_status(store, dossier_id, state="pending", progress="Queued", error=None, summary=None)
    background.add_task(run_assessment, dossier_id, store, llm, policy_obj, policy_sha)
    return JSONResponse({**status, **_links(dossier_id)}, status_code=202)


def _status_or_404(store: Store, dossier_id: str) -> dict[str, Any]:
    status = _read_json(store, f"{dossier_id}/status.json") if ID.match(dossier_id) else None
    if status is None:
        raise HTTPException(404, "no such dossier")
    return _visible(status)


@app.get("/dossiers/{dossier_id}", dependencies=[Depends(require_key)], summary="Status of an assessment")
def get_status(dossier_id: str, store: StoreDep) -> dict[str, Any]:
    return {**_status_or_404(store, dossier_id), **_links(dossier_id)}


@app.get("/dossiers/{dossier_id}/assessment", dependencies=[Depends(require_key)], summary="The assessment")
def get_assessment(dossier_id: str, store: StoreDep) -> JSONResponse:
    status = _status_or_404(store, dossier_id)
    if status["state"] != "done":
        raise HTTPException(409, f"the assessment is {status['state']}")
    data = store.get(f"{dossier_id}/assessment.json")
    if data is None:
        raise HTTPException(404, "no assessment stored")
    return JSONResponse(json.loads(data))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """Ready when the policy loads and storage answers; the reason stays in the log, not the response."""
    try:
        get_policy()
        get_store().get("readyz")
    except Exception:
        log.exception("not ready")
        return JSONResponse({"status": "not ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
