"""Facts: every observation of a field, pooled across documents.

For a single-valued field (a national number, a birth date), all observations are
grouped by their canonical value. One group = the documents agree. Several groups =
a conflict, shown with the evidence for each value. Some differences are not
conflicts: a construction year of 1922 sits inside "1919-1945", and 198 m² vs
198,30 m² is within tolerance. Those are 'compatible', resolved to the most precise value.

Records (one per payslip) and lists (credits in BECRIS) are kept per document.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from credit_review.models import DocumentExtraction, Evidence, Observation
from credit_review.parsing import comparable, format_address
from credit_review.policy import Condition, Policy


class Value(BaseModel):
    """One distinct value of a field, with all the evidence that supports it."""
    value: Any
    display: str
    raws: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @property
    def docs(self) -> list[str]:
        return sorted({e.doc for e in self.evidence})


class Record(BaseModel):
    """One payslip (record) or one list item (a credit), from one document."""
    doc: str
    sha256: str
    index: int | None = None
    values: dict[str, Value] = Field(default_factory=dict)

    def get(self, sub: str) -> Any:
        v = self.values.get(sub)
        return v.value if v else None


FactStatus = Literal["consistent", "compatible", "conflict", "single_source", "missing"]


class Fact(BaseModel):
    name: str
    type: str
    cardinality: Literal["one", "per_document", "list"]
    description: str
    candidates: list[Value] = Field(default_factory=list)      # cardinality one
    records: list[Record] = Field(default_factory=list)        # per_document / list
    resolved: list[Any] = Field(default_factory=list)          # what rules use: 1 value, or every conflicting one
    status: FactStatus = "missing"
    missing_in: list[str] = Field(default_factory=list)        # documents asked for it, without it
    note: str = ""

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def evidence(self) -> list[Evidence]:
        ev = [e for c in self.candidates for e in c.evidence]
        ev += [e for r in self.records for v in r.values.values() for e in v.evidence]
        return ev


# --------------------------------------------------------------------- display

def display(value: Any, ftype: str) -> str:
    if value is None:
        return "—"
    if ftype == "money":
        return f"−€ {abs(value):,.2f}" if value < 0 else f"€ {value:,.2f}"
    if ftype == "ratio":
        return f"{value:.1%}"
    if ftype in ("number", "area"):
        return f"{value:,.2f}".rstrip("0").rstrip(".") + (" m²" if ftype == "area" else "")
    if ftype == "date" and isinstance(value, str) and len(value) == 10:
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    if ftype == "address" and isinstance(value, dict):
        return format_address(value)
    if ftype == "year_range" and isinstance(value, (list, tuple)):
        return str(value[0]) if value[0] == value[1] else f"{value[0]}–{value[1]}"
    if ftype == "boolean":
        return "yes" if value else "no"
    if isinstance(value, list):
        return f"{len(value)} items"
    return str(value)


# --------------------------------------------------------------- consolidation

def _compatible(values: list[Any], ftype: str, tolerance: float | None) -> tuple[bool, Any]:
    """Whether differing values can all be true at once, and the most precise one."""
    if ftype == "year_range":
        lo, hi = max(v[0] for v in values), min(v[1] for v in values)
        return lo <= hi, [lo, hi]
    if ftype in ("area", "number") and tolerance is not None:
        nums = [float(v) for v in values]
        return max(nums) - min(nums) <= tolerance, max(nums, key=lambda x: len(str(x)))
    return False, None


def _group(observations: list[Observation], ftype: str) -> list[Value]:
    groups: dict[Any, Value] = {}
    for o in observations:
        key = comparable(o.value, ftype)
        key = tuple(key) if isinstance(key, list) else key
        if key not in groups:
            groups[key] = Value(value=o.value, display=display(o.value, ftype))
        g = groups[key]
        g.raws.append(o.raw)
        g.evidence.append(o.evidence)
        g.flags.extend(f for f in o.flags if f not in g.flags)
    return sorted(groups.values(), key=lambda v: -len(v.evidence))


def consolidate(policy: Policy, extractions: list[DocumentExtraction]) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    for name, spec in policy.fields.items():
        obs = [o for e in extractions for o in e.observations if o.field == name and o.value is not None]
        asked = [e for e in extractions if name in e.requested]
        have = {o.evidence.doc for o in obs}
        fact = Fact(name=name, type=spec.type, cardinality=spec.cardinality, description=spec.description,
                    missing_in=sorted(e.doc for e in asked if e.doc not in have))
        if spec.cardinality == "one":
            fact.candidates = _group(obs, spec.type)
            if not fact.candidates:
                fact.status = "missing"
            elif len(fact.candidates) == 1:
                fact.status = "single_source" if len(have) == 1 else "consistent"
                fact.resolved = [fact.candidates[0].value]
            else:
                tol = policy.parameters[spec.tolerance_param].value if spec.tolerance_param else None
                ok, merged = _compatible([c.value for c in fact.candidates], spec.type,
                                         tol)  # pyright: ignore[reportArgumentType]  (tolerance parameters are numeric)
                if ok:
                    fact.status, fact.resolved = "compatible", [merged]
                    fact.note = f"Documents differ in precision only; using {display(merged, spec.type)}."
                else:
                    fact.status, fact.resolved = "conflict", [c.value for c in fact.candidates]
        else:
            by_key: dict[tuple, Record] = {}
            sha = {e.doc: e.sha256 for e in extractions}
            for o in obs:
                key = (o.evidence.doc, o.item)
                rec = by_key.setdefault(key, Record(doc=o.evidence.doc, sha256=sha.get(o.evidence.doc, ""),
                                                    index=o.item))
                sub_type = spec.items[o.sub].type if spec.items and o.sub in spec.items else o.type
                rec.values[o.sub or ""] = Value(value=o.value, display=display(o.value, sub_type),
                                                raws=[o.raw], evidence=[o.evidence], flags=o.flags)
            fact.records = sorted(by_key.values(), key=lambda r: (r.doc, r.index or 0))
            fact.status = "consistent" if fact.records else "missing"
            fact.resolved = [fact.records] if fact.records else []
        facts[name] = fact
    return facts


# --------------------------------------------------------------------- fact book

class FactBook:
    """Everything a rule can read: facts, derived values, run inputs, parameters."""

    def __init__(self, policy: Policy, facts: dict[str, Fact], run: dict[str, Any]):
        self.policy = policy
        self.facts = facts
        self.run = run
        self.derived: dict[str, Any] = {}          # name -> Derived (see derive.py)
        self.params = {k: p.value for k, p in policy.parameters.items()}

    def type_of(self, name: str) -> str:
        if name in self.policy.fields:
            return self.policy.fields[name].type
        if name in self.policy.derived:
            return self.policy.derived[name].type
        return "date" if name.startswith("$run.") else "string"

    def values(self, name: str) -> list[Any]:
        """Candidate values: one when the documents agree, several on a conflict, none if missing."""
        if name.startswith("$run."):
            v = self.run.get(name[5:])
            return [] if v is None else [v]
        if name in self.derived:
            return [c.value for c in self.derived[name].candidates if c.value is not None]
        fact = self.facts.get(name)
        return list(fact.resolved) if fact else []

    def records(self, name: str) -> list[Record] | None:
        """Records of a list/record field. None when no document that carries the field was
        read (unknown); [] when such documents were read and list nothing (a real empty list)."""
        fact = self.facts.get(name)
        if fact is None:
            return None
        if fact.records:
            return fact.records
        return [] if fact.missing_in else None

    def condition(self, cond: Condition) -> bool | None:
        """True / False, or None when the input is unknown or the documents conflict on it."""
        values = self.values(cond.field)
        if cond.op == "present":
            return bool(values)
        if not values:
            return None
        target = self.params[cond.param] if cond.param else cond.value
        outcomes = {_compare(v, cond.op, target) for v in values}
        return outcomes.pop() if len(outcomes) == 1 else None


def _compare(v: Any, op: str, target: Any) -> bool:
    if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v):
        v = v[1] if op in ("less_than", "at_most") else v[0]      # year ranges: be conservative
    if op == "equals":
        return v == target
    if op == "in":
        return v in target
    if op == "is_true":
        return v is True
    if op == "is_false":
        return v is False
    if op == "less_than":
        return v < target
    if op == "greater_than":
        return v > target
    if op == "at_most":
        return v <= target
    if op == "at_least":
        return v >= target
    raise ValueError(op)


def iso(d: date | str) -> str:
    return d if isinstance(d, str) else d.isoformat()
