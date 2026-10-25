"""The full run: documents → extraction → facts → checks → assessment.

    python -m credit_review.pipeline case_pack/dossier --decision-date 2026-10-12 --out runs/latest

Every input that determines the result is recorded in the assessment: document
hashes, policy version and hash, model and prompt versions, run dates. Re-running
with the same inputs (and the model cache) gives the same assessment.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from credit_review.classify import classify_documents
from credit_review.derive import Derived, compute_derived
from credit_review.documents import DossierDocument, RequirementStatus, check_documents
from credit_review.engine import ArticleResult, Engine, summarise
from credit_review.extract import extract_dossier
from credit_review.facts import Fact, FactBook, consolidate
from credit_review.integrity import IntegrityResult, run_integrity
from credit_review.judge import make_judge
from credit_review.llm import LLMClient, LLMConfig
from credit_review.models import DocumentExtraction
from credit_review.pdf import PdfDocument, list_dossier
from credit_review.policy import Policy, load_policy


class Assessment(BaseModel):
    policy: dict[str, Any]
    run: dict[str, Any]
    documents: list[DossierDocument]
    requirements: list[RequirementStatus]
    extractions: list[DocumentExtraction]
    facts: list[Fact]
    derived: list[Derived]
    integrity: list[IntegrityResult]
    articles: list[ArticleResult]
    summary: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


def run_inputs(policy: Policy, facts: dict[str, Fact], decision_date: str | None,
               application_date: str | None) -> tuple[dict[str, Any], list[str]]:
    """Resolve run dates; say where each one came from."""
    notes: list[str] = []
    values: dict[str, Any] = {}
    given = {"decision_date": decision_date, "application_date": application_date}
    for name, spec in policy.run_inputs.items():
        if given.get(name):
            values[name] = given[name]
            notes.append(f"{name} = {given[name]} (set for this run)")
            continue
        if spec.default == "today":
            values[name] = date.today().isoformat()
            notes.append(f"{name} = {values[name]} (today)")
            continue
        dates: list[str] = []
        for ref in spec.from_fields:
            if ref in facts:
                dates += [v for v in facts[ref].resolved if isinstance(v, str)]
            else:
                parent, _, sub = ref.rpartition(".")
                if parent in facts:
                    dates += [r.get(sub) for r in facts[parent].records if r.get(sub)]
        values[name] = max(dates) if dates else date.today().isoformat()
        notes.append(f"{name} = {values[name]} ("
                     + ("latest of " + ", ".join(spec.from_fields) if dates else "no dates found; today") + ")")
    return values, notes


def precheck(policy: Policy, docs: list[PdfDocument], types: dict[str, str | None]) -> list[RequirementStatus]:
    """Checklist before extraction: types only, conditions unknown."""
    dossier = [DossierDocument(doc=d.name, sha256=d.sha256, doc_type=types.get(d.name)) for d in docs]
    return check_documents(policy, dossier)


def evaluate(policy: Policy, policy_sha256: str, dossier: list[DossierDocument],
             extractions: list[DocumentExtraction], llm: LLMClient | None = None,
             decision_date: str | None = None, application_date: str | None = None,
             run_judgment: bool = True, progress: Callable[[str], None] | None = None) -> Assessment:
    """Everything after extraction. No model call except (cached) judgment checks, so a
    policy change or a different decision date can be re-evaluated instantly."""
    say = progress or (lambda _: None)
    warnings = [f"{d.doc}: no document type, not read" for d in dossier if not d.doc_type]
    warnings += [f"{e.doc}: extraction failed — {e.error}" for e in extractions if e.error]

    say("Pooling values across documents")
    facts = consolidate(policy, extractions)
    run, notes = run_inputs(policy, facts, decision_date, application_date)
    book = FactBook(policy, facts, run)

    say("Checking documents")
    requirements = check_documents(policy, dossier, book, extractions)
    book.run["documents"] = requirements

    say("Computing")
    derived = compute_derived(book)
    integrity = run_integrity(book)

    say("Applying the policy")
    judge = make_judge(llm) if (llm and run_judgment) else None
    articles = Engine(book, requirements, integrity, judge).run()
    rank = {k: v.rank for k, v in policy.outcomes.items()}

    calls = [e.llm for e in extractions if e.llm]
    return Assessment(
        policy={"id": policy.policy.id, "version": policy.policy.version, "status": policy.policy.status,
                "sha256": policy_sha256, "reviewed_by": policy.policy.translation.get("reviewed_by")},
        run={"created_at": datetime.now().isoformat(timespec="seconds"),
             **{k: v for k, v in run.items() if k != "documents"},
             "dates": notes, "model": llm.describe() if llm else "none",
             "models_used": sorted({f"{c.provider}/{c.model} [{c.mode}] {c.prompt_version}" for c in calls}),
             "judgment": "on" if judge else "off"},
        documents=dossier, requirements=requirements, extractions=extractions,
        facts=list(facts.values()), derived=list(derived.values()), integrity=integrity,
        articles=articles, summary=summarise(articles, rank), warnings=warnings,
    )


def assess(policy: Policy, policy_sha256: str, docs: list[PdfDocument], types: dict[str, str | None],
           llm: LLMClient | None, decision_date: str | None = None, application_date: str | None = None,
           run_judgment: bool = True, workers: int = 4,
           progress: Callable[[str], None] | None = None) -> Assessment:
    say = progress or (lambda _: None)
    dossier = [DossierDocument(doc=d.name, sha256=d.sha256, doc_type=types.get(d.name)) for d in docs]
    say("Reading documents")
    extractions = extract_dossier(docs, {k: v for k, v in types.items() if v}, policy, llm, workers=workers,
                                  progress=lambda doc, status: say(f"Reading {doc}: {status}")) if llm else []
    return evaluate(policy, policy_sha256, dossier, extractions, llm, decision_date, application_date,
                    run_judgment, progress)


def compare(old: Assessment, new: Assessment) -> list[dict[str, str]]:
    """What a policy change does to a file: every check whose outcome differs."""
    a = {c.id: c for art in old.articles for c in art.checks}
    b = {c.id: c for art in new.articles for c in art.checks}
    rows = []
    for cid in list(a) + [x for x in b if x not in a]:
        before, after = a.get(cid), b.get(cid)
        if before and after and before.outcome == after.outcome and before.reason == after.reason:
            continue
        rows.append({
            "check": cid, "article": (after or before).article,
            "change": "added" if before is None else "removed" if after is None else
                      "outcome changed" if before.outcome != after.outcome else "reason changed",
            "before": before.outcome if before else "—", "after": after.outcome if after else "—",
            "reason": (after or before).reason,
        })
    return rows


# ---------------------------------------------------------------------------- CLI

ICON = {"pass": "✓", "fail": "✗", "escalate": "‼", "warning": "!", "document_missing": "□",
        "unresolved": "?", "analyst_review": "→", "info": "i", "not_applicable": "–"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assess a dossier against the policy.")
    ap.add_argument("dossier")
    ap.add_argument("--policy", default="policy/example_bank_v3.2.yaml")
    ap.add_argument("--out", default="runs/latest")
    ap.add_argument("--types", help="JSON {file name: document type}; otherwise auto-detected")
    ap.add_argument("--decision-date")
    ap.add_argument("--application-date")
    ap.add_argument("--no-judgment", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    report = load_policy(args.policy)
    if not report.ok or report.policy is None:
        print("Policy is not valid:\n" + "\n".join(report.errors))
        return 1
    policy = report.policy
    llm = LLMClient(LLMConfig.from_env(offline=True) if args.offline else LLMConfig.from_env())
    docs = list_dossier(args.dossier)
    if args.types:
        types = json.loads(Path(args.types).read_text(encoding="utf-8"))
    else:
        types = {p.doc: p.proposed_type for p in classify_documents(docs, policy, llm)}

    a = assess(policy, report.sha256, docs, types, llm, args.decision_date, args.application_date,
               run_judgment=not args.no_judgment, workers=args.workers,
               progress=lambda m: print(f"  {m}", file=sys.stderr))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "assessment.json").write_text(a.model_dump_json(indent=2), encoding="utf-8")

    print(f"\n{policy.policy.id} v{policy.policy.version} · {a.run['model']} · " + " · ".join(a.run["dates"]))
    for art in a.articles:
        print(f"\n{ICON.get(art.outcome, ' ')} {art.id:5s} {art.outcome.upper()}")
        for c in art.checks:
            print(f"    {ICON.get(c.outcome, ' ')} {c.id:9s} {c.reason[:150]}")
    print("\n" + "\n".join(a.summary["headline"]))
    for w in a.warnings:
        print(f"! {w}")
    print(f"\nWritten to {out / 'assessment.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
