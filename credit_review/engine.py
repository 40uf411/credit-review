"""Rule engine: every check of every article, with the reason and the evidence.

The engine never guesses. When documents disagree on an input, the check runs once
per candidate value: if every candidate gives the same outcome, that outcome stands;
if not, the check is 'unresolved' and shows which value leads where. Judgment checks
go to judge.py, and can never pass on their own. The conclusion (7.3) is never
evaluated.
"""

# Check fields are optional in the schema because each op uses a different subset; the policy
# validator guarantees the ones an op needs. Pyright cannot see that, so these stay warnings here.
# pyright: reportArgumentType=warning

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from credit_review.documents import RequirementStatus
from credit_review.facts import FactBook, _compare, display
from credit_review.integrity import IntegrityResult
from credit_review.models import Evidence
from credit_review.policy import ALL_SINGLE_VALUED, Article, Check, Limit


class CandidateOutcome(BaseModel):
    outcome: str
    detail: str
    basis: dict[str, str] = Field(default_factory=dict)


class CheckResult(BaseModel):
    id: str
    article: str
    description: str
    kind: str
    op: str
    outcome: str
    reason: str
    working: list[str] = Field(default_factory=list)          # how derived numbers were computed
    details: list[str] = Field(default_factory=list)          # conflicts, integrity failures, ...
    candidates: list[CandidateOutcome] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    judgment: dict[str, Any] | None = None


class ArticleResult(BaseModel):
    id: str
    section: str
    section_title: str
    source: str
    outcome: str
    checks: list[CheckResult]


Judge = Callable[[Check, FactBook], dict[str, Any]]


class Engine:
    def __init__(self, book: FactBook, requirements: list[RequirementStatus],
                 integrity: list[IntegrityResult], judge: Judge | None = None):
        self.book = book
        self.policy = book.policy
        self.requirements = {r.id: r for r in requirements}
        self.integrity = integrity
        self.judge = judge
        self.rank = {k: v.rank for k, v in self.policy.outcomes.items()}

    # ------------------------------------------------------------------ helpers
    def _disp(self, name: str, value: Any) -> str:
        return display(value, self.book.type_of(name))

    def _label(self, name: str) -> str:
        if name in self.policy.fields:
            return self.policy.fields[name].description.split(". ")[0].rstrip(".")
        if name in self.policy.derived:
            return self.policy.derived[name].description.split(" — ")[0].split(" (")[0].rstrip(".")
        return name

    def _options(self, name: str) -> list[tuple[Any, dict[str, str]]]:
        """Candidate values of a field or derived value, each with the assumption it rests on."""
        if name in self.book.derived:
            return [(c.value, c.basis) for c in self.book.derived[name].candidates]
        fact = self.book.facts.get(name)
        if fact and fact.conflict:
            return [(v, {name: self._disp(name, v)}) for v in fact.resolved]
        return [(v, {}) for v in self.book.values(name)]

    def _why_missing(self, name: str) -> str:
        d = self.book.derived.get(name)
        if d and d.error:
            return d.error.removeprefix("cannot compute: ")
        fact = self.book.facts.get(name)
        if fact is not None and not fact.resolved:
            where = f"in {', '.join(fact.missing_in)}" if fact.missing_in else "(no document of a type that carries it)"
            return f"{self._label(name)}: not found {where}"
        return f"{name} not available"

    def evidence_for(self, names: list[str]) -> list[Evidence]:
        seen: set[str] = set()
        out: list[Evidence] = []

        def walk(n: str) -> None:
            if n in seen:
                return
            seen.add(n)
            if n in self.policy.derived:
                for i in self.policy.derived[n].inputs:
                    walk(i)
            elif n in self.book.facts:
                out.extend(self.book.facts[n].evidence)

        for n in names:
            walk(n)
        return out

    def working_for(self, names: list[str]) -> list[str]:
        """The derivation chain behind a value, innermost first."""
        out: list[str] = []

        def walk(n: str) -> None:
            spec = self.policy.derived.get(n)
            if spec is None:
                return
            for i in spec.inputs:
                walk(i)
            d = self.book.derived.get(n)
            for c in (d.candidates if d else []):
                line = f"{self._label(n)}: {c.working}"
                if c.basis:
                    line += "  [if " + ", ".join(f"{k} = {v}" for k, v in c.basis.items()) + "]"
                if line not in out:
                    out.append(line)

        for n in names:
            walk(n)
        return out

    # ----------------------------------------------------------------- evaluate
    def run(self) -> list[ArticleResult]:
        results = []
        for section in self.policy.sections:
            for article in section.articles:
                checks = [self.check(article, c) for c in article.checks]
                outcome = max((c.outcome for c in checks), key=lambda o: self.rank.get(o, 0))
                results.append(ArticleResult(id=article.id, section=section.id, section_title=section.title,
                                             source=article.source, outcome=outcome, checks=checks))
        return results

    def check(self, article: Article, c: Check) -> CheckResult:
        base: dict[str, Any] = dict(id=c.id, article=article.id, description=c.description, kind=c.kind, op=c.op)
        applies = [self.book.condition(cond) for cond in c.applies_when]
        if False in applies:
            why = next(cond for cond, a in zip(c.applies_when, applies) if a is False)
            return CheckResult(**base, outcome="not_applicable",
                               reason=f"Does not apply: {self._label(why.field)} is "
                                      f"{', '.join(self._disp(why.field, v) for v in self.book.values(why.field)) or 'not stated'}.")
        if None in applies:
            why = next(cond for cond, a in zip(c.applies_when, applies) if a is None)
            return CheckResult(**base, outcome="analyst_review",
                               reason=f"Cannot tell whether this applies: {self._why_missing(why.field)}"
                                      if not self.book.values(why.field) else
                                      f"Cannot tell whether this applies: documents disagree on {self._label(why.field)}.")
        handler = getattr(self, f"_op_{c.op}")
        return handler(c, base)

    # comparisons ---------------------------------------------------------------
    def _limit_options(self, limit: Limit) -> list[tuple[Any, dict[str, str], str]]:
        if limit.param:
            p = self.policy.parameters[limit.param]
            return [(p.value, {}, f"{limit.param} = {self._disp_param(limit.param)}")]
        return [(v, b, self._label(limit.field)) for v, b in self._options(limit.field)]

    def _disp_param(self, name: str) -> str:
        p = self.policy.parameters[name]
        if p.unit == "ratio":
            return f"{p.value:.1%}"
        if p.unit == "EUR":
            return f"€ {p.value:,.2f}"
        return f"{p.value} {p.unit}".strip()

    def _compare_check(self, c: Check, base: dict, op: str) -> CheckResult:
        field = c.field
        options = self._options(field)
        limits = self._limit_options(c.limit)
        working = self.working_for([field] + ([c.limit.field] if c.limit.field else []))
        evidence = self.evidence_for([field] + ([c.limit.field] if c.limit.field else []))
        if not options or not limits or limits[0][0] is None:
            missing = field if not options else c.limit.field
            return CheckResult(**base, outcome="analyst_review", reason=f"Cannot evaluate: {self._why_missing(missing)}.",
                               working=working, evidence=evidence)
        warn = c.warn_above or c.warn_below
        warn_v = self.policy.parameters[warn.param].value if warn and warn.param else None
        sym = {"at_most": "≤", "at_least": "≥", "less_than": "<"}[op]
        cands = []
        for (v, vb), (lim, lb, _lname) in itertools.product(options, limits):
            ok = _compare(v, op, lim)
            outcome = "pass" if ok else c.on_fail
            if ok and warn_v is not None and ((c.warn_above and v > warn_v) or (c.warn_below and v < warn_v)):
                outcome = "warning"
            vt, lt = self._disp(field, v), (self._disp(c.limit.field, lim) if c.limit.field else self._disp_param(c.limit.param))
            detail = f"{self._label(field)} {vt}; required {sym} {lt}"
            if outcome == "warning" and warn_v is not None:
                detail += f" (exception band above {self._disp_param(warn.param)})"
            cands.append(CandidateOutcome(outcome=outcome, detail=detail, basis={**vb, **lb}))
        return self._settle(base, cands, working, evidence)

    def _settle(self, base: dict, cands: list[CandidateOutcome], working: list[str],
                evidence: list[Evidence], details: list[str] | None = None) -> CheckResult:
        outcomes = {x.outcome for x in cands}
        if len(outcomes) == 1:
            reason = cands[0].detail if len(cands) == 1 else \
                f"{cands[0].outcome.replace('_', ' ').capitalize()} whichever document is right. " + \
                " ".join(f"If {self._basis_text(x.basis)}: {x.detail}." for x in cands)
            return CheckResult(**base, outcome=cands[0].outcome, reason=reason, working=working,
                               evidence=evidence, details=details or [],
                               candidates=cands if len(cands) > 1 else [])
        return CheckResult(**base, outcome="unresolved",
                           reason="Depends on which document is right. " + " ".join(
                               f"If {self._basis_text(x.basis)}: {x.detail} → {x.outcome.replace('_', ' ')}." for x in cands),
                           working=working, evidence=evidence, details=details or [], candidates=cands)

    def _basis_text(self, basis: dict[str, str]) -> str:
        parts = []
        for name, shown in basis.items():
            fact = self.book.facts.get(name)
            docs = next((v.docs for v in fact.candidates if v.display == shown), []) if fact else []
            parts.append(f"{self._label(name).lower()} is {shown}" + (f" ({', '.join(docs)})" if docs else ""))
        return " and ".join(parts) or "this value"

    def _op_at_most(self, c, base):
        return self._compare_check(c, base, "at_most")

    def _op_at_least(self, c, base):
        return self._compare_check(c, base, "at_least")

    def _op_less_than(self, c, base):
        return self._compare_check(c, base, "less_than")

    def _simple(self, c: Check, base: dict, test: Callable[[Any], bool], expect: str) -> CheckResult:
        options = self._options(c.field)
        working, evidence = self.working_for([c.field]), self.evidence_for([c.field])
        if not options:
            return CheckResult(**base, outcome="analyst_review", reason=f"Cannot evaluate: {self._why_missing(c.field)}.",
                               working=working, evidence=evidence)
        d = self.book.derived.get(c.field)
        why = {repr(x.value): x.working for x in d.candidates} if d else {}
        cands = [CandidateOutcome(outcome="pass" if test(v) else c.on_fail,
                                  detail=f"{self._label(c.field)}: {self._disp(c.field, v)}"
                                         + (f" — {why[repr(v)]}" if why.get(repr(v)) else "")
                                         + ("" if test(v) else f" (required: {expect})"), basis=b)
                 for v, b in options]
        return self._settle(base, cands, working, evidence)

    def _op_equals(self, c, base):
        return self._simple(c, base, lambda v: v == c.value, str(c.value))

    def _op_in(self, c, base):
        return self._simple(c, base, lambda v: v in c.value, "one of " + ", ".join(map(str, c.value)))

    def _op_is_true(self, c, base):
        return self._simple(c, base, lambda v: v is True, "yes")

    def _op_is_false(self, c, base):
        return self._simple(c, base, lambda v: v is False, "no")

    # presence & consistency ------------------------------------------------------
    def _op_present(self, c, base):
        missing = [f for f in c.fields if not self.book.values(f)]
        evidence = self.evidence_for([f for f in c.fields if f not in missing])
        if missing:
            return CheckResult(**base, outcome=c.on_fail, evidence=evidence,
                               reason="; ".join(self._why_missing(f) for f in missing) + ".")
        shown = "; ".join(f"{self._label(f)}: {', '.join(self._disp(f, v) for v in self.book.values(f))}"
                          for f in c.fields)
        return CheckResult(**base, outcome="pass", reason=shown, evidence=evidence)

    def _op_consistent(self, c, base):
        names = [n for n, s in self.policy.fields.items() if s.cardinality == "one"] \
            if c.fields == [ALL_SINGLE_VALUED] else c.fields
        conflicts, notes, evidence = [], [], []
        for n in names:
            fact = self.book.facts.get(n)
            if not fact:
                continue
            if fact.conflict:
                conflicts.append(f"{self._label(n)}: " + " vs ".join(
                    f"{v.display} ({', '.join(v.docs)})" for v in fact.candidates))
                evidence += fact.evidence
            elif fact.status == "compatible":
                notes.append(f"{self._label(n)}: {fact.note}")
        if conflicts:
            differ = [self._label(n) for n in names if self.book.facts.get(n) and self.book.facts[n].conflict]
            return CheckResult(**base, outcome=c.on_fail, evidence=evidence, details=conflicts + notes,
                               reason="Documents disagree on: " + "; ".join(d.lower() for d in differ) + ".")
        agreed = [n for n in names if self.book.facts.get(n) and self.book.facts[n].status in ("consistent", "compatible")]
        shown = [f"{self._label(n)} {display(self.book.facts[n].resolved[0], self.book.facts[n].type)} "
                 f"({len({e.doc for e in self.book.facts[n].evidence})} documents)" for n in agreed]
        if c.fields == [ALL_SINGLE_VALUED]:
            reason = f"No differences: {len(agreed)} values that appear in several documents all agree."
        else:
            reason = "Same on every document: " + "; ".join(shown) + "." if shown else "Found in one document only."
        return CheckResult(**base, outcome="pass", details=notes, evidence=self.evidence_for(agreed), reason=reason)

    # documents, integrity, values ---------------------------------------------
    def _op_document_requirement(self, c, base):
        req = self.requirements[c.requirement]
        outcome = {"satisfied": "pass", "not_applicable": "not_applicable", "missing": "document_missing",
                   "too_few": "document_missing", "duplicate": "warning", "too_many": "warning",
                   "analyst": "analyst_review"}[req.status]
        docs = f" ({', '.join(req.docs)})" if req.docs else ""
        return CheckResult(**base, outcome=outcome,
                           reason=f"{req.id} {req.source}: {req.detail[0].lower() + req.detail[1:].rstrip('.')}{docs}.")

    def _op_integrity(self, c, base):
        rel = [r for r in self.integrity if r.severity == c.severity]
        failed = [r for r in rel if r.status == "fail"]
        skipped = [r for r in rel if r.status == "not_evaluable"]
        details = [f"{r.id} {r.description}: {r.detail}" for r in failed] + \
                  [f"{r.id} not evaluable — {r.detail}" for r in skipped]
        evidence = [e for r in failed for e in r.evidence]
        if failed:
            return CheckResult(**base, outcome=c.on_fail, details=details, evidence=evidence,
                               reason=f"{len(failed)} of {len(rel)} failed: "
                                      + "; ".join(r.description.rstrip(".") for r in failed) + ".")
        return CheckResult(**base, outcome="pass", details=details,
                           reason=f"All {len(rel) - len(skipped)} evaluable checks passed.")

    def _op_computed(self, c, base):
        options = self._options(c.field)
        working, evidence = self.working_for([c.field]), self.evidence_for([c.field])
        if not options:
            return CheckResult(**base, outcome="analyst_review", reason=f"Cannot compute: {self._why_missing(c.field)}.",
                               working=working, evidence=evidence)
        values = ", ".join(self._disp(c.field, v) for v, _ in options)
        return CheckResult(**base, outcome="info", reason=f"{self._label(c.field)}: {values}",
                           working=working, evidence=evidence)

    def _op_judgment(self, c, base):
        evidence = self.evidence_for(c.inputs or [])
        if self.judge is None:
            return CheckResult(**base, outcome="analyst_review", evidence=evidence,
                               reason="Judgment check not run: no model configured. The analyst assesses this.")
        j = self.judge(c, self.book)
        return CheckResult(**base, outcome=j["outcome"], reason=j["reason"], details=j.get("details", []),
                           evidence=evidence, judgment=j)

    def _op_reserved_for_analyst(self, c, base):
        return CheckResult(**base, outcome="analyst_review",
                           reason="Reserved for the analyst. Credit Review provides the findings and evidence, not the conclusion.")


def summarise(articles: list[ArticleResult], rank: dict[str, int]) -> dict[str, Any]:
    by = lambda o: [a.id for a in articles if a.outcome == o]
    counts = {o: len(by(o)) for o in rank}
    lines = []
    if by("escalate"):
        lines.append(f"Fraud signals: escalate to the fraud desk ({', '.join(by('escalate'))}).")
    if by("fail"):
        lines.append(f"Criteria not met: {', '.join(by('fail'))}.")
    if by("document_missing"):
        lines.append(f"Evidence missing: {', '.join(by('document_missing'))}.")
    if by("unresolved"):
        lines.append(f"Documents disagree, outcome open: {', '.join(by('unresolved'))}.")
    if by("warning"):
        lines.append(f"Exceptions or clarifications needed: {', '.join(by('warning'))}.")
    lines.append("Conclusion (7.3) reserved for the analyst.")
    return {"counts": counts, "headline": lines,
            "articles": {o: by(o) for o in rank if by(o)}}
