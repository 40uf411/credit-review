"""Field extraction: the policy decides what to read, the model reads, code verifies.

For each confirmed document, the fields whose `sources` include its type are
requested in one call. The model returns, per value: the raw text, its normalised
value and a verbatim quote. Code then
  1. re-parses the raw text (code wins on disagreement, which is flagged),
  2. finds the quote in the text layer to get page, lines and box,
  3. checks the raw value really appears in the quote.
Scanned documents go to a vision model; their evidence is the page image.

    python -m credit_review.extract case_pack/dossier --policy policy/example_bank_v3.2.yaml --out runs/demo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from credit_review.llm import LLMClient, LLMConfig, LLMError, LLMRequest
from credit_review.models import DocumentExtraction, Evidence, LLMCallInfo, Observation
from credit_review.parsing import CODE_PARSEABLE, ParseError, comparable, parse_value
from credit_review.pdf import PdfDocument, list_dossier, normalise_for_search
from credit_review.policy import FieldSpec, ItemSpec, Policy, load_policy

PROMPT_VERSION = "extract-v2"

SYSTEM = """You extract data from documents in Belgian mortgage credit files. A credit analyst will verify every value you return against the document.
Documents are mostly in Dutch, sometimes French or German. Never translate values.

Rules:
1. Extract only what is printed. Never infer, compute, add up or guess a value.
2. raw: the value exactly as printed — the value only, not its label.
3. value: the same value normalised as instructed for its type.
4. quote: a short verbatim excerpt that contains BOTH the label and the value itself (at most about 15 words), copied character for character from the document, so the analyst can find and check it. A label without its value is not a valid quote.
5. page: the page number where the quote appears.
6. If a field is not in this document: found=false, raw="", value="", quote="", page=0.
7. Ignore watermark letters scattered over the document (for example the letters of SPECIMEN).
8. Amounts are positive numbers: a deduction printed as "- 416,94" has value 416.94."""

TYPE_RULES = {
    "name": "as printed, in normal capitalisation",
    "string": "as printed",
    "rrn": "the 11 digits only, no dots or dashes",
    "date": "YYYY-MM-DD",
    "month": "YYYY-MM",
    "money": "plain number, '.' as decimal separator, no thousands separator, no currency (3.190,07 -> 3190.07)",
    "number": "plain number, '.' as decimal separator",
    "percent": "decimal fraction (4,0% -> 0.04)",
    "integer": "whole number",
    "boolean": "true or false",
    "address": "Street Number[ bus Box], Postcode Municipality",
    "iban": "as printed, without spaces",
    "year_range": "YYYY or YYYY-YYYY",
    "area": "number of m², '.' as decimal separator",
    "enum": "one of the allowed values",
}

# single-token types whose raw text must appear inside the quote
QUOTE_CHECKED = {"money", "number", "integer", "date", "rrn", "iban", "area", "percent"}


def schema_key(name: str) -> str:
    return name.replace(".", "__")


def fields_for_type(policy: Policy, doc_type: str) -> dict[str, FieldSpec]:
    return {name: spec for name, spec in policy.fields.items() if doc_type in spec.sources}


# ------------------------------------------------------------------------- schema

def _obs_schema(ftype: str, values: list[str] | None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "string"}
    if ftype == "enum" and values:
        value["enum"] = [*values, ""]
    return {
        "type": "object",
        "properties": {"found": {"type": "boolean"}, "raw": {"type": "string"}, "value": value,
                       "quote": {"type": "string"}, "page": {"type": "integer"}},
        "required": ["found", "raw", "value", "quote", "page"],
        "additionalProperties": False,
    }


def _item_schema(items: dict[str, ItemSpec]) -> dict[str, Any]:
    return {"type": "object",
            "properties": {sub: _obs_schema(s.type, s.values) for sub, s in items.items()},
            "required": list(items), "additionalProperties": False}


def build_schema(fields: dict[str, FieldSpec]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, spec in fields.items():
        if spec.cardinality == "list":
            props[schema_key(name)] = {
                "type": "object",
                "properties": {"found": {"type": "boolean"},
                               "items": {"type": "array", "items": _item_schema(spec.items or {})}},
                "required": ["found", "items"], "additionalProperties": False}
        elif spec.cardinality == "per_document":
            props[schema_key(name)] = {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "item": _item_schema(spec.items or {})},
                "required": ["found", "item"], "additionalProperties": False}
        else:
            props[schema_key(name)] = _obs_schema(spec.type, spec.values)
    return {"type": "object",
            "properties": {"fields": {"type": "object", "properties": props,
                                      "required": list(props), "additionalProperties": False}},
            "required": ["fields"], "additionalProperties": False}


# ------------------------------------------------------------------------- prompt

def _rule(ftype: str, values: list[str] | None) -> str:
    if ftype == "enum" and values:
        return "one of: " + ", ".join(values)
    return TYPE_RULES.get(ftype, "as printed")


def build_prompt(policy: Policy, doc_type: str, fields: dict[str, FieldSpec], doc: PdfDocument,
                 vision: bool) -> str:
    dt = policy.document_types[doc_type]
    out = [f"Document type: {dt.label}" + (f" ({dt.note})" if dt.note else ""), "", "Fields to extract:"]
    for name, spec in fields.items():
        where = spec.sources.get(doc_type, "")
        if spec.cardinality in ("list", "per_document"):
            shape = ("a list: one entry per item, in document order" if spec.cardinality == "list"
                     else "one record for this document")
            out.append(f"- {schema_key(name)}: {spec.description} Where: {where}. Return {shape}, with:")
            for sub, s in (spec.items or {}).items():
                hint = f" {s.description}" if s.description else ""
                out.append(f"    - {sub}:{hint} Value: {_rule(s.type, s.values)}.")
        else:
            out.append(f"- {schema_key(name)}: {spec.description} Where: {where}. "
                       f"Value: {_rule(spec.type, spec.values)}.")
    out.append("")
    if vision:
        out.append(f"The document is attached as {min(doc.page_count, 4)} page image(s), in order: "
                   "image 1 is page 1. Quotes must be copied exactly as they appear in the image.")
    else:
        out += ["Document text (the '=== Page N ===' markers are not part of the document):", "",
                doc.text_for_model()]
    return "\n".join(out)


# ------------------------------------------------------------------ interpretation

def _squash(text: str) -> str:
    return re.sub(r"\s+", "", normalise_for_search(text))


def make_observation(field: str, sub: str | None, item: int | None, spec: FieldSpec | ItemSpec,
                     r: dict[str, Any] | None, doc: PdfDocument, doc_type: str, vision: bool) -> Observation | None:
    """Turn one model answer into a checked observation; None when the value is not in the document."""
    ftype, values = spec.type, spec.values
    if not isinstance(r, dict):
        return None
    raw = str(r.get("raw") or "").strip()
    model_value = str(r.get("value") or "").strip()
    quote = str(r.get("quote") or "").strip()
    page = r.get("page") if isinstance(r.get("page"), int) and r.get("page") > 0 else None
    if not r.get("found") or not (raw or model_value):
        return None

    flags: list[str] = []
    value: Any = None
    parse = "failed"
    if ftype == "enum":
        match = next((v for v in values or [] if v.casefold() == model_value.casefold()), None)
        if match is not None:
            value, parse = match, "model"
        else:
            flags.append("invalid_enum")
    elif ftype in CODE_PARSEABLE:
        code_v = model_v = None
        try:
            code_v = parse_value(raw, ftype)
        except ParseError:
            pass
        try:
            model_v = parse_value(model_value, ftype)
        except ParseError:
            pass
        if code_v is not None:
            value, parse = code_v, "code"
            if model_v is not None and comparable(model_v, ftype) != comparable(code_v, ftype):
                flags.append("raw_value_mismatch")
        elif model_v is not None:
            value, parse = model_v, "model"
        else:
            flags.append("unparseable")
    else:
        value, parse = (model_value or raw), "model"

    span = doc.locate(quote, page_hint=page)
    if span.located == "not_found":
        flags.append("quote_not_found")
    elif span.located == "fuzzy":
        flags.append("quote_fuzzy")
    elif span.located == "none":
        flags.append("no_quote")
    if raw and quote and ftype in QUOTE_CHECKED and _squash(raw) not in _squash(quote):
        flags.append("raw_not_in_quote")
    if raw and span.located == "fuzzy" and ftype in QUOTE_CHECKED and _squash(raw) not in _squash(span.matched_text):
        flags.append("raw_not_in_document")     # quote resembles the page, but the value is not on it

    evidence = Evidence(
        doc=doc.name, doc_type=doc_type, method="vision" if vision else "text", quote=quote,
        page=span.page or page, lines=span.lines, bbox=list(span.bbox) if span.bbox else None,
        located=span.located, score=span.score)
    return Observation(field=field, sub=sub, item=item, type=ftype, raw=raw or model_value,
                       model_value=model_value, value=value, parse=parse, flags=flags, evidence=evidence)


def interpret(data: dict[str, Any], fields: dict[str, FieldSpec], doc: PdfDocument, doc_type: str,
              vision: bool) -> tuple[list[Observation], list[str]]:
    """Model reply -> (observations found, paths requested but not found)."""
    reply = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    observations: list[Observation] = []
    not_found: list[str] = []

    def add(obs: Observation | None, path: str) -> None:
        if obs is None:
            not_found.append(path)
        else:
            observations.append(obs)

    for name, spec in fields.items():
        r = reply.get(schema_key(name))
        if spec.cardinality == "one":
            add(make_observation(name, None, None, spec, r, doc, doc_type, vision), name)
            continue
        if not (isinstance(r, dict) and r.get("found")):
            not_found.append(name)
            continue
        if spec.cardinality == "per_document":                     # one record for this document
            item = r.get("item") if isinstance(r.get("item"), dict) else {}
            for sub, s in (spec.items or {}).items():
                add(make_observation(name, sub, None, s, item.get(sub), doc, doc_type, vision), f"{name}.{sub}")
            continue
        rows = [x for x in r.get("items", []) if isinstance(x, dict)]
        if not rows:
            not_found.append(name)
        for i, row in enumerate(rows):
            for sub, s in (spec.items or {}).items():
                add(make_observation(name, sub, i, s, row.get(sub), doc, doc_type, vision), f"{name}[{i}].{sub}")
    return observations, not_found


# --------------------------------------------------------------------------- run

def extract_document(doc: PdfDocument, doc_type: str, policy: Policy, llm: LLMClient) -> DocumentExtraction:
    fields = fields_for_type(policy, doc_type)
    vision = not doc.has_text_layer
    result = DocumentExtraction(doc=doc.name, sha256=doc.sha256, doc_type=doc_type,
                                method="vision" if vision else "text", requested=list(fields))
    if not fields:
        return result                                   # nothing to read from this type
    req = LLMRequest(
        purpose="extract", system=SYSTEM, prompt=build_prompt(policy, doc_type, fields, doc, vision),
        schema=build_schema(fields), schema_name="extraction",
        images=doc.images_for_model() if vision else [],
        meta={"doc": doc.name, "sha256": doc.sha256, "doc_type": doc_type, "prompt_version": PROMPT_VERSION},
    )
    try:
        res = llm.complete(req)
    except LLMError as exc:
        result.error = str(exc)
        return result
    result.llm = LLMCallInfo(provider=res.provider, model=res.model, mode=res.mode, cache_key=res.cache_key,
                             cached=res.cached, prompt_version=PROMPT_VERSION, usage=res.usage,
                             seconds=res.seconds, cost=res.cost)
    result.observations, result.not_found = interpret(res.data, fields, doc, doc_type, vision)
    flags = [f for o in result.observations for f in o.flags]
    result.flags = sorted({f"{f} x{flags.count(f)}" for f in flags})
    return result


def extract_dossier(docs: list[PdfDocument], types: dict[str, str], policy: Policy, llm: LLMClient,
                    workers: int = 4, progress: Callable[[str, str], None] | None = None
                    ) -> list[DocumentExtraction]:
    todo = [d for d in docs if types.get(d.name) and types[d.name] in policy.document_types]

    def run(doc: PdfDocument) -> DocumentExtraction:
        if progress:
            progress(doc.name, "started")
        out = extract_document(doc, types[doc.name], policy, llm)
        if progress:
            progress(doc.name, "error" if out.error else "done")
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(run, todo))


# --------------------------------------------------------------------------- CLI

def _summary(results: list[DocumentExtraction]) -> str:
    rows = []
    for r in results:
        found = len(r.observations)
        how = "error" if r.error else (
            f"{r.llm.model} [{r.llm.mode}]{' (cache)' if r.llm.cached else ''}" if r.llm else "—")
        rows.append(f"{r.doc[:42]:42s} {r.doc_type:20s} {r.method:6s} {found:3d} values  {how}")
        if r.error:
            rows.append(f"    ! {r.error}")
        for f in r.flags:
            rows.append(f"    · {f}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    from credit_review.classify import classify_documents

    ap = argparse.ArgumentParser(description="Classify and extract a dossier.")
    ap.add_argument("dossier")
    ap.add_argument("--policy", default="policy/example_bank_v3.2.yaml")
    ap.add_argument("--out", default="runs/latest")
    ap.add_argument("--types", help="JSON file {file name: document type}; skips auto-detection")
    ap.add_argument("--provider", choices=["anthropic", "lmstudio", "openai"])
    ap.add_argument("--model")
    ap.add_argument("--offline", action="store_true", help="use cached model results only")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    report = load_policy(args.policy)
    if not report.ok or report.policy is None:
        print("Policy is not valid:\n" + "\n".join(report.errors))
        return 1
    policy = report.policy

    import os
    if args.provider:
        os.environ["CREDIT_REVIEW_LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["CREDIT_REVIEW_LLM_MODEL"] = args.model
    config = LLMConfig.from_env(offline=True) if args.offline else LLMConfig.from_env()
    llm = LLMClient(config)
    print(f"Model: {llm.describe()}\n")

    docs = list_dossier(args.dossier)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.types:
        types = json.loads(Path(args.types).read_text(encoding="utf-8"))
    else:
        proposals = classify_documents(docs, policy, llm)
        (out / "classification.json").write_text(
            json.dumps([p.model_dump(mode="json") for p in proposals], indent=2, ensure_ascii=False), encoding="utf-8")
        types = {p.doc: p.proposed_type for p in proposals if p.proposed_type}
        for p in proposals:
            print(f"{p.doc[:42]:42s} -> {p.proposed_type or '?':20s} {p.method:8s} {p.confidence}")
        print()
    results = extract_dossier(docs, types, policy, llm, workers=args.workers,
                              progress=lambda d, s: print(f"  {s:7s} {d}", file=sys.stderr))
    (out / "extraction.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in results], indent=2, ensure_ascii=False), encoding="utf-8")
    print(_summary(results))
    print(f"\nWritten to {out}/")
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
