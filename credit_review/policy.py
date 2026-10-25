"""Policy configuration: schema, loading and validation.

Validation answers one question before any policy goes live: does every rule
refer to things that exist, and does every rule trace back to a sentence the
bank actually wrote?

    python -m credit_review.policy policy/example_bank_v3.2.yaml
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from credit_review.contracts import DERIVED_FUNCTIONS, INTEGRITY_FUNCTIONS

# --------------------------------------------------------------------------- types

FieldType = Literal[
    "name", "string", "rrn", "date", "month", "money", "number", "percent", "integer",
    "boolean", "address", "iban", "year_range", "area", "enum", "list", "record", "ratio",
]
Outcome = Literal["fail", "warning", "escalate", "document_missing"]
ConditionOp = Literal[
    "equals", "in", "less_than", "greater_than", "at_most", "at_least",
    "is_true", "is_false", "present",
]
CheckOp = Literal[
    "at_most", "at_least", "less_than", "equals", "in", "is_true", "is_false",
    "present", "consistent", "document_requirement", "integrity", "computed",
    "judgment", "reserved_for_analyst",
]

RUN_REF = re.compile(r"^\$run\.(\w+)$")
SPECIAL_INPUTS = {"$documents"}
ALL_SINGLE_VALUED = "$all_single_valued"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemSpec(Strict):
    type: FieldType
    values: list[str] | None = None
    description: str | None = None


class FieldSpec(Strict):
    type: FieldType
    cardinality: Literal["one", "per_document", "list"]
    required: bool
    description: str
    sources: dict[str, str]
    values: list[str] | None = None
    items: dict[str, ItemSpec] | None = None
    sensitive: bool = False
    tolerance_param: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> FieldSpec:
        if self.type == "enum" and not self.values:
            raise ValueError("enum fields need 'values'")
        if self.type in ("list", "record") and not self.items:
            raise ValueError(f"{self.type} fields need 'items'")
        if self.type == "record" and self.cardinality != "per_document":
            raise ValueError("record fields must have cardinality per_document")
        if self.type == "list" and self.cardinality != "list":
            raise ValueError("list fields must have cardinality list")
        if not self.sources:
            raise ValueError("a field needs at least one source document type")
        return self


class Parameter(Strict):
    value: int | float | list[str]
    unit: str
    owner: Literal["example_bank", "example_systems"]
    source: str
    description: str


class Condition(Strict):
    field: str
    op: ConditionOp
    value: Any = None
    param: str | None = None

    @model_validator(mode="after")
    def _operand(self) -> Condition:
        unary = self.op in ("is_true", "is_false", "present")
        has_operand = self.value is not None or self.param is not None
        if unary and has_operand:
            raise ValueError(f"condition op '{self.op}' takes no value")
        if not unary and not has_operand:
            raise ValueError(f"condition op '{self.op}' needs a value or a param")
        if self.value is not None and self.param is not None:
            raise ValueError("condition takes a value or a param, not both")
        return self


class Count(Strict):
    min: int = 1
    max: int | None = None


class RequiredDocument(Strict):
    id: str
    source: str
    description: str
    accepts: list[str]
    count: Count
    per_applicant: bool = False
    applies_when: list[Condition] = []
    decided_by: Literal["analyst"] | None = None
    unique_by: str | None = None
    blocks_offer: bool = False


class DocumentType(Strict):
    label: str
    detect: list[str]
    note: str | None = None


class Derived(Strict):
    type: FieldType
    fn: str
    inputs: list[str]
    params: list[str] = []
    description: str


class IntegrityCheck(Strict):
    id: str
    description: str
    fn: str
    inputs: list[str]
    params: list[str] = []
    severity: Literal["fraud_signal", "warning", "info"]


class Limit(Strict):
    param: str | None = None
    field: str | None = None

    @model_validator(mode="after")
    def _one(self) -> Limit:
        if (self.param is None) == (self.field is None):
            raise ValueError("a limit references exactly one of 'param' or 'field'")
        return self


class Check(Strict):
    id: str
    description: str
    kind: Literal["deterministic", "judgment", "analyst"]
    op: CheckOp
    field: str | None = None
    fields: list[str] | None = None
    limit: Limit | None = None
    warn_above: Limit | None = None
    warn_below: Limit | None = None
    value: Any = None
    requirement: str | None = None
    severity: Literal["fraud_signal", "warning", "info"] | None = None
    question: str | None = None
    inputs: list[str] | None = None
    params: list[str] = []
    applies_when: list[Condition] = []
    on_fail: Outcome | None = None

    @model_validator(mode="after")
    def _arguments(self) -> Check:
        op = self.op
        need: dict[str, tuple[str, ...]] = {
            "at_most": ("field", "limit", "on_fail"),
            "at_least": ("field", "limit", "on_fail"),
            "less_than": ("field", "limit", "on_fail"),
            "equals": ("field", "value", "on_fail"),
            "in": ("field", "value", "on_fail"),
            "is_true": ("field", "on_fail"),
            "is_false": ("field", "on_fail"),
            "present": ("fields", "on_fail"),
            "consistent": ("fields", "on_fail"),
            "document_requirement": ("requirement",),
            "integrity": ("severity", "on_fail"),
            "computed": ("field",),
            "judgment": ("question", "inputs"),
            "reserved_for_analyst": (),
        }
        missing = [a for a in need[op] if getattr(self, a) in (None, [])]
        if missing:
            raise ValueError(f"op '{op}' needs: {', '.join(missing)}")
        if self.params and op != "judgment":
            raise ValueError("params on a check are only for judgment questions")
        if self.warn_above and op != "at_most":
            raise ValueError("warn_above is only valid with at_most")
        if self.warn_below and op != "at_least":
            raise ValueError("warn_below is only valid with at_least")
        expected_kind = {"judgment": "judgment", "reserved_for_analyst": "analyst"}.get(op, "deterministic")
        if self.kind != expected_kind:
            raise ValueError(f"op '{op}' must have kind '{expected_kind}', not '{self.kind}'")
        return self


class Article(Strict):
    id: str
    source: str
    checks: list[Check]


class Section(Strict):
    id: str
    title: str
    articles: list[Article]


class RunInput(Strict):
    type: Literal["date"]
    default: Literal["today", "latest_of"]
    from_fields: list[str] = []          # for latest_of: the dates that count (issue/consultation dates only)
    description: str

    @model_validator(mode="after")
    def _from(self) -> RunInput:
        if (self.default == "latest_of") != bool(self.from_fields):
            raise ValueError("default 'latest_of' needs from_fields, and only it")
        return self


class OutcomeSpec(Strict):
    rank: int
    description: str


class JudgmentSpec(Strict):
    allowed_outcomes: list[str]
    outcome_mapping: dict[str, str]
    rules: list[str]


class PolicyMeta(Strict):
    id: str
    version: str
    title: str
    owner: str
    status: Literal["draft", "approved", "in_force"]
    effective_from: str | None = None
    source_document: str
    translation: dict[str, str | None]


class Policy(Strict):
    schema_version: int
    policy: PolicyMeta
    run_inputs: dict[str, RunInput]
    outcomes: dict[str, OutcomeSpec]
    judgment: JudgmentSpec
    parameters: dict[str, Parameter]
    document_types: dict[str, DocumentType]
    required_documents: list[RequiredDocument]
    fields: dict[str, FieldSpec]
    derived: dict[str, Derived]
    integrity_checks: list[IntegrityCheck]
    sections: list[Section]

    # convenience -----------------------------------------------------------
    def checks(self) -> list[tuple[Section, Article, Check]]:
        return [(s, a, c) for s in self.sections for a in s.articles for c in a.checks]

    def requirement(self, req_id: str) -> RequiredDocument:
        return next(r for r in self.required_documents if r.id == req_id)


# ---------------------------------------------------------------------- validation

@dataclass
class ValidationReport:
    path: Path
    sha256: str
    policy: Policy | None = None
    errors: list[str] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _normalise_text(text: str) -> str:
    """Compare policy text ignoring markdown emphasis and line wrapping."""
    text = text.replace("**", "")
    text = re.sub(r"(?m)^\s*\d+(\.\d+)*\.\s+", " ", text)   # article numbers at line start
    return re.sub(r"\s+", " ", text).strip()


def load_policy(path: str | Path) -> ValidationReport:
    path = Path(path)
    raw = path.read_bytes()
    report = ValidationReport(path=path, sha256=hashlib.sha256(raw).hexdigest())
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        report.errors.append(f"YAML syntax: {exc}")
        return report
    try:
        report.policy = Policy.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            report.errors.append(f"schema: {loc}: {err['msg']}")
        return report
    _cross_reference(report)
    return report


def _cross_reference(report: ValidationReport) -> None:
    p = report.policy
    if p is None:
        return
    err, warn = report.errors.append, report.warnings.append

    names = set(p.fields) | set(p.derived)
    params = set(p.parameters)
    doc_types = set(p.document_types)
    requirements = {r.id for r in p.required_documents}
    used_names: set[str] = set()
    used_params: set[str] = set()

    def ref_name(name: str, where: str, allow_special: bool = False) -> None:
        if m := RUN_REF.match(name):
            if m.group(1) not in p.run_inputs:
                err(f"{where}: unknown run input '{name}'")
            return
        if name in SPECIAL_INPUTS:
            if not allow_special:
                err(f"{where}: '{name}' is only allowed as an input")
            return
        if name not in names:
            err(f"{where}: unknown field '{name}'")
        used_names.add(name)

    def ref_param(name: str | None, where: str) -> None:
        if name is None:
            return
        if name not in params:
            err(f"{where}: unknown parameter '{name}'")
        used_params.add(name)

    def ref_limit(limit: Limit | None, where: str) -> None:
        if limit is None:
            return
        ref_param(limit.param, where)
        if limit.field:
            ref_name(limit.field, where)

    def ref_conditions(conds: list[Condition], where: str) -> None:
        for c in conds:
            ref_name(c.field, f"{where} applies_when")
            ref_param(c.param, f"{where} applies_when")

    # run inputs ---------------------------------------------------------------
    for rname, ri in p.run_inputs.items():
        for ref in ri.from_fields:                      # a field, or field.sub of a record/list
            parent, sub = (ref, None) if ref in p.fields else (ref.rpartition(".")[0], ref.rpartition(".")[2])
            spec = p.fields.get(parent)
            if spec is None or (sub and sub not in (spec.items or {})):
                err(f"run input {rname}: unknown field '{ref}'")
                continue
            if (spec.items[sub].type if sub else spec.type) != "date":
                err(f"run input {rname}: '{ref}' is not a date field")
            used_names.add(parent)

    # unique ids ---------------------------------------------------------------
    for label, ids in [
        ("check", [c.id for _, _, c in p.checks()]),
        ("article", [a.id for s in p.sections for a in s.articles]),
        ("integrity check", [i.id for i in p.integrity_checks]),
        ("required document", [r.id for r in p.required_documents]),
    ]:
        for dup, n in Counter(ids).items():
            if n > 1:
                err(f"duplicate {label} id '{dup}'")

    # fields ---------------------------------------------------------------------
    for fname, spec in p.fields.items():
        for dt in spec.sources:
            if dt not in doc_types:
                err(f"field {fname}: unknown source document type '{dt}'")
        ref_param(spec.tolerance_param, f"field {fname}")

    # required documents ---------------------------------------------------------
    for r in p.required_documents:
        for dt in r.accepts:
            if dt not in doc_types:
                err(f"requirement {r.id}: unknown document type '{dt}'")
        ref_conditions(r.applies_when, f"requirement {r.id}")
        if r.unique_by:
            parent, _, sub = r.unique_by.rpartition(".") if r.unique_by not in p.fields else (r.unique_by, "", "")
            spec = p.fields.get(parent)
            if spec is None or (sub and sub not in (spec.items or {})):
                err(f"requirement {r.id}: unique_by '{r.unique_by}' is not a known field")
            elif not set(r.accepts) & set(spec.sources):
                err(f"requirement {r.id}: unique_by '{r.unique_by}' is not read from the accepted documents")
        if r.decided_by and r.applies_when:
            err(f"requirement {r.id}: use either applies_when or decided_by, not both")

    # derived: references, contracts, no cycles -----------------------------------
    for dname, d in p.derived.items():
        if dname in p.fields:
            err(f"derived '{dname}' shadows an extracted field")
        if d.fn not in DERIVED_FUNCTIONS:
            err(f"derived {dname}: function '{d.fn}' is not in the Example Systems catalogue")
        for i in d.inputs:
            ref_name(i, f"derived {dname}", allow_special=True)
        for prm in d.params:
            ref_param(prm, f"derived {dname}")
    _check_cycles(p, err)

    # integrity checks -------------------------------------------------------------
    for ic in p.integrity_checks:
        if ic.fn not in INTEGRITY_FUNCTIONS:
            err(f"integrity {ic.id}: function '{ic.fn}' is not in the Example Systems catalogue")
        for i in ic.inputs:
            ref_name(i, f"integrity {ic.id}", allow_special=True)
        for prm in ic.params:
            ref_param(prm, f"integrity {ic.id}")

    # checks ---------------------------------------------------------------------
    for _, _art, c in p.checks():
        where = f"check {c.id}"
        if c.field:
            ref_name(c.field, where)
        for f in c.fields or []:
            if f == ALL_SINGLE_VALUED:
                if c.op != "consistent":
                    err(f"{where}: {ALL_SINGLE_VALUED} only works with op 'consistent'")
                continue
            ref_name(f, where)
            if c.op == "consistent":
                spec = p.fields.get(f)
                if spec is None:
                    err(f"{where}: consistency applies to extracted fields only, not '{f}'")
                elif spec.cardinality != "one":
                    err(f"{where}: '{f}' has cardinality {spec.cardinality}; only 'one' fields can conflict")
                elif len(spec.sources) < 2:
                    warn(f"{where}: '{f}' has a single source, so it can never conflict")
        for i in c.inputs or []:
            ref_name(i, where)
        for prm in c.params:
            ref_param(prm, where)
        ref_limit(c.limit, where)
        ref_limit(c.warn_above, where)
        ref_limit(c.warn_below, where)
        ref_conditions(c.applies_when, where)
        if c.requirement and c.requirement not in requirements:
            err(f"{where}: unknown required document '{c.requirement}'")
        if c.on_fail and c.on_fail not in p.outcomes:
            err(f"{where}: outcome '{c.on_fail}' is not in the outcome vocabulary")

    # judgment contract -----------------------------------------------------------
    j = p.judgment
    if set(j.outcome_mapping) != set(j.allowed_outcomes):
        err("judgment: outcome_mapping must map exactly the allowed outcomes")
    for model_out, outcome in j.outcome_mapping.items():
        if outcome not in p.outcomes:
            err(f"judgment: '{model_out}' maps to unknown outcome '{outcome}'")
        if outcome == "pass":
            err("judgment: a model outcome may never map to 'pass'")

    # traceability to the bank's own text ---------------------------------------
    src = report.path.parent / p.policy.source_document
    if not src.exists():
        err(f"source document not found: {src}")
    else:
        policy_text = _normalise_text(src.read_text(encoding="utf-8"))
        for s in p.sections:
            for a in s.articles:
                if _normalise_text(a.source) not in policy_text:
                    err(f"article {a.id}: source text is not found verbatim in the policy document")
        for r in p.required_documents:
            if _normalise_text(r.source) not in policy_text:
                err(f"requirement {r.id}: source text is not found verbatim in the policy document")

    # the numbers the engine uses are the numbers the bank wrote ------------------
    _check_numbers(p, err)

    # every article covered, every parameter documented ---------------------------
    for prm, spec in p.parameters.items():
        if spec.owner == "example_bank" and spec.source not in {a.id for s in p.sections for a in s.articles} | {"6"}:
            err(f"parameter {prm}: bank-owned parameter cites unknown article '{spec.source}'")

    # drift: things declared but never used --------------------------------------
    for prm in sorted(params - used_params):
        warn(f"parameter '{prm}' is declared but never used")
    for name in sorted(set(p.derived) - used_names):
        warn(f"derived '{name}' is computed but never used")
    unused_fields = sorted(set(p.fields) - used_names)
    if unused_fields:
        report.notes.append("fields used only by the cross-document consistency sweep (7.2): " + ", ".join(unused_fields))


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def _numbers_in(text: str) -> set[float]:
    """Numbers as written in policy prose: 45%, €1,200, 4.0%, 76th, 'three months'.
    Identifiers such as D1, 7.2 or 2.2.b are not numbers."""
    found: set[float] = set()
    text = re.sub(r"\b[A-Z]\d+\b", " ", text)                                   # D1, I-06 style ids
    text = re.sub(r"(?<![€\d.,])\b\d{1,2}\.\d{1,2}(\.[a-z0-9]+)*\b(?![\d%]|\s*%)", " ", text)   # article references
    for m in re.finditer(r"\d+(?:[.,]\d+)*", text):
        tok = m.group()
        if "." in tok and "," in tok:                                                # 52.600,00 or 52,600.00
            dec = max(tok.rfind("."), tok.rfind(","))
            tok = re.sub(r"[.,]", "", tok[:dec]) + "." + tok[dec + 1:]
        elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", tok):                            # 1,200 or 1.200
            tok = re.sub(r"[.,]", "", tok)
        try:
            found.add(float(tok.replace(",", ".")))
        except ValueError:
            pass
    for word, n in _NUMBER_WORDS.items():          # "three most recent months", not "at least one measure"
        if re.search(rf"\b{word}\b(\s+\w+){{0,3}}?\s+(day|week|month|year)s?\b", text, re.IGNORECASE):
            found.add(float(n))
    return found


def _as_written(prm: Parameter) -> float | None:
    """The number a person would write for this parameter: ratios as percentages."""
    if isinstance(prm.value, list):
        return None
    return round(prm.value * 100, 6) if prm.unit == "ratio" else float(prm.value)


def _check_numbers(p: Policy, err) -> None:
    """Catch a half-done change: a parameter changed without the bank's text, or the text
    without the parameter, or a check description still quoting the old number."""
    articles = {a.id: a for s in p.sections for a in s.articles}
    for name, prm in p.parameters.items():
        if prm.owner != "example_bank" or prm.source not in articles:
            continue
        text = articles[prm.source].source
        if isinstance(prm.value, list):
            absent = [v for v in prm.value if v.lower() not in text.lower()]
            if absent:
                err(f"parameter {name}: {', '.join(absent)} not found in the text of article {prm.source}")
        elif _as_written(prm) not in _numbers_in(text):
            err(f"parameter {name} = {prm.value} ({prm.unit}) is not stated in the text of article "
                f"{prm.source}; change the parameter and the policy text together")

    def params_behind(name: str | None, seen: set[str]) -> set[str]:
        if not name or name in seen or name not in p.derived:
            return set()
        seen.add(name)
        d = p.derived[name]
        return set(d.params) | {x for i in d.inputs for x in params_behind(i, seen)}

    for _, _art, c in p.checks():
        if c.kind != "deterministic":
            continue
        used = {x.param for x in (c.limit, c.warn_above, c.warn_below) if x and x.param}
        used |= {cond.param for cond in c.applies_when if cond.param}
        counts: set[float] = set()
        if c.requirement:
            req = p.requirement(c.requirement)
            used |= {cond.param for cond in req.applies_when if cond.param}
            counts = {float(x) for x in (req.count.min, req.count.max) if x is not None}
        for name in [c.field, *(c.fields or []), *(x.field for x in (c.limit, c.warn_above, c.warn_below) if x)]:
            used |= params_behind(name, set())
        allowed = {_as_written(p.parameters[x]) for x in used if x in p.parameters} | counts
        stale = sorted(n for n in _numbers_in(c.description) if n not in allowed)
        if stale:
            values = ", ".join(f"{x} = {p.parameters[x].value}" for x in sorted(used) if x in p.parameters) or "none"
            err(f"check {c.id}: description mentions {', '.join(f'{n:g}' for n in stale)}, which is not the value "
                f"of any parameter it uses ({values}); update the description")


def _check_cycles(p: Policy, err) -> None:
    graph = {d: [i for i in spec.inputs if i in p.derived] for d, spec in p.derived.items()}
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            err("derived cycle: " + " -> ".join(trail + [node]))
            return
        state[node] = 1
        for nxt in graph[node]:
            visit(nxt, trail + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])


# ------------------------------------------------------------------------- summary

def summary(report: ValidationReport) -> str:
    lines = [f"Policy file : {report.path}", f"SHA-256     : {report.sha256}"]
    p = report.policy
    if p:
        meta = p.policy
        kinds = Counter(c.kind for _, _, c in p.checks())
        owners = Counter(prm.owner for prm in p.parameters.values())
        lines += [
            f"Policy      : {meta.id} v{meta.version} ({meta.status})",
            f"Reviewed by : {meta.translation.get('reviewed_by') or '— not yet reviewed'}",
            "",
            f"Sections {len(p.sections)} · articles {sum(len(s.articles) for s in p.sections)} · "
            f"checks {sum(kinds.values())} "
            f"(deterministic {kinds['deterministic']}, judgment {kinds['judgment']}, analyst {kinds['analyst']})",
            f"Required documents {len(p.required_documents)} · document types {len(p.document_types)}",
            f"Fields {len(p.fields)} · derived {len(p.derived)} · integrity checks {len(p.integrity_checks)}",
            f"Parameters {len(p.parameters)} (bank-owned {owners['example_bank']}, implementation {owners['example_systems']})",
        ]
        impl = [k for k, v in p.parameters.items() if v.owner == "example_systems"]
        if impl:
            lines += ["", "Implementation parameters to confirm with Example Bank:"]
            lines += [f"  - {k} = {p.parameters[k].value} {p.parameters[k].unit}" for k in impl]
    if report.notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in report.notes]
    if report.warnings:
        lines += ["", f"Warnings ({len(report.warnings)}):"] + [f"  - {w}" for w in report.warnings]
    lines += ["", f"Errors ({len(report.errors)}):" if report.errors else "Result: VALID"]
    lines += [f"  - {e}" for e in report.errors]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m credit_review.policy <policy.yaml>")
        return 2
    report = load_policy(argv[0])
    print(summary(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
