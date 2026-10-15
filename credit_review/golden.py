"""Hand-labelled reference values: replay them as a model, or score a model against them.

    python -m credit_review.golden runs/latest/extraction.json golden/sample_dossier.yaml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from credit_review.llm import LLMError, LLMRequest
from credit_review.models import DocumentExtraction
from credit_review.parsing import CODE_PARSEABLE, ParseError, comparable, parse_value


def load_golden(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _reply_obs(g: dict[str, Any] | None) -> dict[str, Any]:
    if not g:
        return {"found": False, "raw": "", "value": "", "quote": "", "page": 0}
    raw = str(g.get("raw", ""))
    return {"found": True, "raw": raw, "value": str(g.get("value", raw)),
            "quote": str(g.get("quote", raw)), "page": int(g.get("page", 0))}


class GoldenProvider:
    """Answers model calls from the hand-labelled file. For tests and offline development only;
    results are recorded with provider 'golden', so they can never pass for model output."""
    name = "golden"

    def __init__(self, golden: dict[str, Any]):
        self.docs = golden["documents"]
        self.by_sha = {d.get("sha256"): d for d in self.docs.values() if d.get("sha256")}

    def complete(self, req: LLMRequest, model: str, max_tokens: int):
        doc = self.by_sha.get(req.meta.get("sha256")) or self.docs.get(req.meta.get("doc", ""))
        if doc is None:
            raise LLMError(f"no golden labels for {req.meta.get('doc')!r}")
        if req.purpose == "classify":
            data = {"doc_type": doc["doc_type"], "confidence": "high", "rationale": "Hand-labelled reference."}
            return data, {}, "golden", json.dumps(data)
        if req.purpose != "extract":
            raise LLMError("the golden provider only answers classify and extract calls")
        requested = req.schema["properties"]["fields"]["properties"]
        labels = doc.get("fields", {})
        fields: dict[str, Any] = {}
        for key, shape in requested.items():
            g = labels.get(key.replace("__", "."))
            props = shape["properties"]
            if "items" in props:                                   # list
                subs = props["items"]["items"]["properties"]
                items = [{s: _reply_obs(it.get(s)) for s in subs} for it in (g or [])]
                fields[key] = {"found": bool(items), "items": items}
            elif "item" in props:                                  # record
                subs = props["item"]["properties"]
                fields[key] = {"found": bool(g), "item": {s: _reply_obs((g or {}).get(s)) for s in subs}}
            else:
                fields[key] = _reply_obs(g)
        data = {"fields": fields}
        return data, {}, "golden", json.dumps(data, ensure_ascii=False)


# -------------------------------------------------------------------- evaluation

def _canonical(g: dict[str, Any], ftype: str) -> Any:
    text = str(g.get("value", g.get("raw", "")))
    if ftype in CODE_PARSEABLE:
        try:
            return comparable(parse_value(text, ftype), ftype)
        except ParseError:
            return None
    return comparable(text, ftype)


def _expected(labels: dict[str, Any]) -> dict[tuple[str, int | None, str | None], dict[str, Any]]:
    """Reference labels keyed like observations: (field, item, sub)."""
    out: dict[tuple[str, int | None, str | None], dict[str, Any]] = {}
    for name, g in labels.items():
        if isinstance(g, list):                                    # list field
            for i, row in enumerate(g):
                for sub, gv in row.items():
                    out[(name, i, sub)] = gv
        elif isinstance(g, dict) and ("raw" in g or "value" in g):  # scalar
            out[(name, None, None)] = g
        elif isinstance(g, dict):                                  # record (one per document)
            for sub, gv in g.items():
                out[(name, None, sub)] = gv
    return out


def evaluate(extractions: list[DocumentExtraction], golden: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per expected or extracted value: match, mismatch, missed or extra."""
    rows: list[dict[str, Any]] = []
    by_sha = {e.sha256: e for e in extractions}          # content identity: file names get mangled
    by_doc = {e.doc: e for e in extractions}
    for doc_name, gdoc in golden["documents"].items():
        ext = by_sha.get(gdoc.get("sha256")) or by_doc.get(doc_name)
        if ext is None:
            rows.append({"doc": doc_name, "field": "*", "status": "missed", "detail": "document not extracted"})
            continue
        expected = _expected(gdoc.get("fields", {}))
        got = {(o.field, o.item, o.sub): o for o in ext.observations}
        for key in sorted(set(expected) | set(got), key=str):
            field, item, sub = key
            label = field + (f"[{item}]" if item is not None else "") + (f".{sub}" if sub else "")
            gv, obs = expected.get(key), got.get(key)
            if gv is None:
                rows.append({"doc": doc_name, "field": label, "status": "extra",
                             "detail": f"extracted {obs.raw!r}, not in the reference"})
            elif obs is None:
                rows.append({"doc": doc_name, "field": label, "status": "missed",
                             "detail": f"expected {gv.get('raw')!r}"})
            else:
                want, have = _canonical(gv, obs.type), comparable(obs.value, obs.type)
                ok = want == have
                rows.append({"doc": doc_name, "field": label, "status": "match" if ok else "mismatch",
                             "detail": "" if ok else f"expected {want!r}, got {have!r} (raw {obs.raw!r})",
                             "located": obs.evidence.located})
    return rows


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: sum(r["status"] == s for r in rows) for s in ("match", "mismatch", "missed", "extra")}
    expected = counts["match"] + counts["mismatch"] + counts["missed"]
    return {**counts, "expected": expected,
            "accuracy": round(counts["match"] / expected, 4) if expected else None}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m credit_review.golden <extraction.json> <golden.yaml>")
        return 2
    data = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    rows = evaluate([DocumentExtraction.model_validate(d) for d in data], load_golden(argv[1]))
    for r in rows:
        if r["status"] != "match":
            print(f"{r['status']:8s} {r['doc'][:38]:38s} {r['field']:45s} {r['detail']}")
    s = score(rows)
    print(f"\n{s['match']}/{s['expected']} reference values matched ({s['accuracy']:.1%}); "
          f"{s['mismatch']} wrong, {s['missed']} missed, {s['extra']} extra.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
