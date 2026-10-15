"""Document checklist: which required documents are present, missing, duplicated.

Runs twice. Before extraction (types only), so the analyst sees gaps before
spending a model call; requirements that depend on file content ("salaried
applicants") are shown as conditional. After extraction, with facts, conditions are
resolved and duplicates by content (two payslips for the same month) are caught.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from credit_review.facts import FactBook
from credit_review.models import DocumentExtraction
from credit_review.policy import Policy, RequiredDocument

Status = Literal["satisfied", "missing", "too_few", "too_many", "duplicate", "not_applicable", "analyst"]


class DossierDocument(BaseModel):
    doc: str
    sha256: str
    doc_type: str | None              # confirmed by the analyst; None = not classified


class RequirementStatus(BaseModel):
    id: str
    description: str
    source: str
    accepts: list[str]
    applies: Literal["yes", "no", "unknown"]
    status: Status
    docs: list[str] = Field(default_factory=list)
    detail: str = ""
    blocks_offer: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("satisfied", "not_applicable")


def _condition_text(req: RequiredDocument) -> str:
    return " and ".join(f"{c.field} {c.op} {c.param or c.value}" for c in req.applies_when)


def check_documents(policy: Policy, dossier: list[DossierDocument],
                    book: FactBook | None = None,
                    extractions: list[DocumentExtraction] | None = None) -> list[RequirementStatus]:
    out = []
    by_type: dict[str, list[DossierDocument]] = {}
    for d in dossier:
        if d.doc_type:
            by_type.setdefault(d.doc_type, []).append(d)

    for req in policy.required_documents:
        docs = [d for t in req.accepts for d in by_type.get(t, [])]
        base = dict(id=req.id, description=req.description, source=req.source, accepts=req.accepts,
                    docs=[d.doc for d in docs], blocks_offer=req.blocks_offer)

        # does the requirement apply to this file?
        applies: Literal["yes", "no", "unknown"] = "yes"
        if req.applies_when:
            results = [book.condition(c) for c in req.applies_when] if book else [None]
            applies = "no" if False in results else ("unknown" if None in results else "yes")
        if applies == "no":
            out.append(RequirementStatus(**base, applies="no", status="not_applicable",
                                         detail=f"Applies only when {_condition_text(req)}."))
            continue
        conditional = f" Required when {_condition_text(req)}." if applies == "unknown" else ""

        # same file twice, or two documents with the same identifying value (e.g. the same month)
        dup_files = [s for s, n in Counter(d.sha256 for d in docs).items() if n > 1]
        dup_keys: list[str] = []
        if req.unique_by and extractions:
            field, _, sub = req.unique_by.rpartition(".") if req.unique_by not in policy.fields else (req.unique_by, "", "")
            keys = []
            for e in extractions:
                if e.sha256 in {d.sha256 for d in docs}:
                    keys += [str(o.value) for o in e.observations
                             if o.field == field and (o.sub or "") == sub and o.value is not None]
            dup_keys = [k for k, n in Counter(keys).items() if n > 1]

        n = len({d.sha256 for d in docs})
        if dup_files or dup_keys:
            what = "the same file twice" if dup_files else f"two documents for {', '.join(dup_keys)}"
            status, detail = "duplicate", f"Contains {what}."
        elif n == 0:
            if req.decided_by == "analyst":
                status, detail = "analyst", "Not present. Whether it is required depends on regional regulation: analyst decides."
            else:
                status, detail = "missing", "Not present." + conditional
        elif n < req.count.min:
            status, detail = "too_few", f"{n} present, {req.count.min} required." + conditional
        elif req.count.max is not None and n > req.count.max:
            status, detail = "too_many", f"{n} present, at most {req.count.max} expected."
        else:
            status, detail = "satisfied", f"{n} present."
        out.append(RequirementStatus(**base, applies=applies, status=status, detail=detail))
    return out


def unclassified(dossier: list[DossierDocument]) -> list[str]:
    return [d.doc for d in dossier if not d.doc_type]
