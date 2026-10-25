"""Derived values: deterministic computations owned and tested by Example Systems.

Every function returns (value, working), the working being the computation written
out with the actual numbers, so an analyst can redo it by hand. When an input has
conflicting values across documents, the derivation runs once per combination and
every result is kept, labelled with the input value it assumes.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from credit_review.facts import FactBook, Record, display

MAX_COMBINATIONS = 16


class MissingInput(Exception):
    pass


class DerivedValue(BaseModel):
    value: Any
    display: str
    working: str = ""
    basis: dict[str, str] = Field(default_factory=dict)   # conflicting input -> value assumed


class Derived(BaseModel):
    name: str
    type: str
    description: str
    fn: str
    inputs: list[str]
    candidates: list[DerivedValue] = Field(default_factory=list)
    error: str | None = None

    @property
    def conflict(self) -> bool:
        return len({repr(c.value) for c in self.candidates}) > 1


# ----------------------------------------------------------------------- helpers

def _d(x: Any) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def _f(x: date | str) -> str:
    return display(x if isinstance(x, str) else x.isoformat(), "date")


def _eur(x: float) -> str:
    return f"−€ {-x:,.2f}" if x < 0 else f"€ {x:,.2f}"


def need(x: Any, what: str) -> Any:
    if x is None or x == []:
        raise MissingInput(what)
    return x


def known(x: Any, what: str) -> Any:
    """Like need(), but an empty list is a real answer ("the register lists no credits")."""
    if x is None:
        raise MissingInput(what)
    return x


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last))


def _months_back(ym: tuple[int, int], k: int) -> tuple[int, int]:
    y, m = divmod(ym[0] * 12 + ym[1] - 1 - k, 12)
    return y, m + 1


def _sorted_payslips(recs: list[Record]) -> list[Record]:
    return sorted((r for r in recs if r.get("period")), key=lambda r: r.get("period"))


# --------------------------------------------------------------------- functions

def age_in_years(birth, ref):
    b, r = _d(need(birth, "birth date")), _d(need(ref, "reference date"))
    years = r.year - b.year - ((r.month, r.day) < (b.month, b.day))
    return years, f"born {_f(b)}, {years} on {_f(r)}"


def age_at_maturity(birth, decision, term):
    b, dd = _d(need(birth, "birth date")), _d(need(decision, "decision date"))
    end = add_months(dd, 12 * int(need(term, "credit term")))
    age = round((end - b).days / 365.2425, 1)
    return age, f"credit ends {_f(end)} ({term} years after {_f(dd)}); born {_f(b)} → age {age}"


def years_between(a, b):
    if a is None:
        raise MissingInput("start date")
    years = round((_d(need(b, "end date")) - _d(a)).days / 365.2425, 1)
    return years, f"{_f(_d(a))} → {_f(_d(b))}: {years} years"


def date_on_or_after(a, b):
    a, b = _d(need(a, "date")), _d(need(b, "reference date"))
    return a >= b, f"{_f(a)} {'≥' if a >= b else '<'} {_f(b)}"


def payslips_cover_recent_months(recs, ref, n):
    slips = _sorted_payslips(need(recs, "payslips"))
    ad = _d(need(ref, "application date"))
    paydays = [_d(r.get("pay_date")).day for r in slips if r.get("pay_date")]
    payday = max(paydays) if paydays else 28
    latest = (ad.year, ad.month) if ad.day >= payday else _months_back((ad.year, ad.month), 1)
    required = [f"{y:04d}-{m:02d}" for y, m in (_months_back(latest, k) for k in reversed(range(int(n))))]
    present = sorted({r.get("period") for r in slips})
    missing = [m for m in required if m not in present]
    working = (f"pay day is the {payday}th; on {_f(ad)} the most recent paid month is "
               f"{required[-1]}; required {', '.join(required)}; present {', '.join(present)}")
    if missing:
        working += f"; missing {', '.join(missing)}"
    return not missing, working


def payslips_have_advance_or_seizure(recs):
    hits = [f"{r.get('period')}: {name} {_eur(r.get(sub))}" for r in _sorted_payslips(need(recs, "payslips"))
            for sub, name in (("salary_advance", "advance"), ("seizure", "seizure")) if r.get(sub)]
    return bool(hits), "; ".join(hits) if hits else "no advance or seizure on any payslip"


def average_net_pay(recs, n):
    slips = _sorted_payslips(need(recs, "payslips"))[-int(n):]
    nets = [need(r.get("net_pay"), f"net pay {r.get('period')}") for r in slips]
    avg = round(sum(nets) / len(nets), 2)
    return avg, f"({' + '.join(_eur(x) for x in nets)}) / {len(nets)} = {_eur(avg)}"


def relative_deviation(a, b):
    a, b = need(a, "declared income"), need(b, "evidenced income")
    dev = round(abs(a - b) / b, 4)
    return dev, f"|{_eur(a)} − {_eur(b)}| / {_eur(b)} = {dev:.1%}"


def sum_non_professional_income(items):
    rows = [(r.get("label") or r.get("type"), r.get("monthly_amount") or 0) for r in known(items, "declared income")
            if r.get("type") != "professional"]
    total = round(sum(a for _, a in rows), 2)
    return total, " + ".join(f"{l} {_eur(a)}" for l, a in rows) or "none declared"


def sum_child_benefit(items):
    rows = [r.get("monthly_amount") or 0 for r in known(items, "declared income") if r.get("type") == "child_benefit"]
    total = round(sum(rows), 2)
    return total, f"declared child benefit {_eur(total)}" if rows else "no child benefit declared"


def child_benefit_consistent_with_household(cb, dependents, members):
    if known(cb, "declared child benefit") == 0:
        return True, "no child benefit declared"
    deps = dependents or 0
    ok = deps >= 1
    return ok, (f"child benefit {_eur(cb)} declared; dependents on payslips: {deps}; "
                f"household members in the application: {members if members is not None else '—'}")


def counted_income(evidenced, employment_type):
    if employment_type != "salaried":
        raise MissingInput("income of non-salaried applicants is not modelled in the pilot")
    ev = need(evidenced, "evidenced income")
    return ev, f"evidenced net professional income {_eur(ev)}; non-professional income not counted until 3.3 is confirmed"


def tax_income_ratio(aggregate_taxable, recs):
    jt = need(aggregate_taxable, "taxable income")
    slips = _sorted_payslips(need(recs, "payslips"))
    taxable = [r.get("taxable") for r in slips if r.get("taxable")]
    avg = sum(need(taxable, "taxable pay on payslips")) / len(taxable)
    ratio = round((jt / 12) / avg, 4)
    return ratio, f"tax data {_eur(jt)} / 12 = {_eur(jt / 12)} vs payslip taxable average {_eur(avg)}: {ratio:.0%}"


def max_relative_change(recs):
    slips = _sorted_payslips(need(recs, "payslips"))
    gross = [(r.get("period"), r.get("gross_monthly")) for r in slips if r.get("gross_monthly")]
    changes = [(b[0], (b[1] - a[1]) / a[1]) for a, b in zip(gross, gross[1:])]
    if not changes:
        raise MissingInput("at least two payslips with gross pay")
    period, worst = max(changes, key=lambda c: abs(c[1]))
    trail = " → ".join(f"{p} {_eur(g)}" for p, g in gross)
    return round(abs(worst), 4), f"gross pay {trail}; largest change {worst:+.1%} ({period})"


def _match(credit: Record, declared: list[Record], tol: float) -> Record | None:
    for d in declared:
        if (d.get("principal") is not None and credit.get("contract_amount") is not None
                and abs(d.get("principal") - credit.get("contract_amount")) <= tol
                and abs((d.get("monthly_payment") or 0) - (credit.get("instalment") or 0)) <= tol):
            return d
    return None


def existing_obligations(credits, declared, tol):
    credits = known(credits, "BECRIS credit register (CKP extract)")
    declared = known(declared, "declared liabilities (application form)")
    register = [c.get("instalment") or 0 for c in credits]
    matched = {id(_match(c, declared, tol)) for c in credits}
    extra = [d.get("monthly_payment") or 0 for d in declared if id(d) not in matched]
    total = round(sum(register) + sum(extra), 2)
    parts = [_eur(x) for x in register] + [f"{_eur(x)} (declared only)" for x in extra]
    return total, f"{' + '.join(parts) or '0'} = {_eur(total)}"


def annuity_payment(amount, term, rate, reference_rate):
    p, years = need(amount, "credit amount"), int(need(term, "credit term"))
    annual = rate if rate is not None else reference_rate
    r, n = annual / 12, years * 12
    pmt = round(p / n if r == 0 else p * r / (1 - (1 + r) ** -n), 2)
    source = "offered rate" if rate is not None else "reference rate (no rate offered)"
    return pmt, f"annuity of {_eur(p)} over {n} months at {annual:.2%} ({source}) = {_eur(pmt)}/month"


def sum_amounts(*xs):
    vals = [need(x, "amount") for x in xs]
    total = round(sum(vals), 2)
    return total, f"{' + '.join(_eur(x) for x in vals)} = {_eur(total)}"


def ratio(a, b):
    a, b = need(a, "numerator"), need(b, "denominator")
    r = round(a / b, 4)
    return r, f"{_eur(a)} / {_eur(b)} = {r:.1%}"


def difference(a, b):
    a, b = need(a, "amount"), need(b, "amount")
    d = round(a - b, 2)
    return d, f"{_eur(a)} − {_eur(b)} = {_eur(d)}"


def required_free_income(dependents, minimum, per_dependent):
    deps = known(dependents, "number of dependents")
    req = minimum + per_dependent * deps
    return req, f"{_eur(minimum)} + {deps} × {_eur(per_dependent)} = {_eur(req)}"


def within_months(d, ref, n):
    d, ref = _d(need(d, "document date")), _d(need(ref, "decision date"))
    limit = add_months(d, int(n))
    return ref <= limit, f"dated {_f(d)}, valid until {_f(limit)}; decision date {_f(ref)}"


def _in_arrear(c: Record) -> bool:
    status = str(c.get("arrear_status") or "").casefold()
    return bool(status) and not any(w in status for w in ("geen", "aucun", "no ", "none"))


def has_unregularized_arrear(indicator, credits):
    known(indicator, "BECRIS arrear indicator (CKP extract)")
    open_ = [c for c in known(credits, "BECRIS credit register") if _in_arrear(c) and not c.get("regularized_date")]
    flag = bool(indicator) or bool(open_)
    return flag, f"arrear indicator {'set' if indicator else 'not set'}; credits in arrear: {len(open_)}"


def has_regularized_arrear(credits, min_age_years):
    reg = [c for c in known(credits, "BECRIS credit register (CKP extract)") if c.get("regularized_date")]
    return bool(reg), f"regularized arrears registered: {len(reg)}"


def all_register_credits_declared(credits, declared, tol):
    credits = known(credits, "BECRIS credit register (CKP extract)")
    declared = known(declared, "declared liabilities (application form)")
    missing = [c for c in credits if _match(c, declared, tol) is None]
    found = len(credits) - len(missing)
    working = f"{found} of {len(credits)} BECRIS credits matched to a declared liability (amount and instalment)"
    if missing:
        working += "; undeclared: " + ", ".join(_eur(c.get("contract_amount") or 0) for c in missing)
    return not missing, working


def applicant_owns_property(owner_rrn, applicant_rrn, declared_owner):
    owner = need(owner_rrn, "buyer on the deed")
    ok = owner == need(applicant_rrn, "applicant national number")
    return ok, f"buyer on the deed {'is' if ok else 'is not'} the applicant (national number)"


def includes_any_category(works, allowed):
    cats = [(w.get("label"), w.get("category")) for w in known(works, "planned works (application form)")]
    hits = [l for l, c in cats if c in allowed]
    return bool(hits), (f"eligible: {', '.join(hits)}" if hits else "no eligible energy measure") + \
        f" (works: {', '.join(f'{l} [{c}]' for l, c in cats) or 'none'})"


def required_own_funds(total, minimum, share):
    t = need(total, "total investment")
    req = round(max(minimum, share * t), 2)
    return req, f"max({_eur(minimum)}, {share:.0%} × {_eur(t)} = {_eur(share * t)}) = {_eur(req)}"


def offer_blocking_documents_present(statuses):
    blocking = [s for s in need(statuses, "document checklist") if s.blocks_offer]
    missing = [s.id for s in blocking if not s.ok]
    return not missing, ("D1–D6 present" if not missing else f"missing or incomplete: {', '.join(missing)}")


FUNCTIONS: dict[str, Callable[..., tuple[Any, str]]] = {
    f.__name__: f for f in [
        age_in_years, age_at_maturity, years_between, date_on_or_after, payslips_cover_recent_months,
        payslips_have_advance_or_seizure, average_net_pay, relative_deviation, sum_non_professional_income,
        sum_child_benefit, child_benefit_consistent_with_household, counted_income, tax_income_ratio,
        max_relative_change, existing_obligations, annuity_payment, sum_amounts, ratio, difference,
        required_free_income, within_months, has_unregularized_arrear, has_regularized_arrear,
        all_register_credits_declared, applicant_owns_property, includes_any_category, required_own_funds,
        offer_blocking_documents_present,
    ]
}


# ------------------------------------------------------------------------ runner

def _order(book: FactBook) -> list[str]:
    spec = book.policy.derived
    done: list[str] = []

    def visit(n: str) -> None:
        if n in done:
            return
        for i in spec[n].inputs:
            if i in spec:
                visit(i)
        done.append(n)

    for n in spec:
        visit(n)
    return done


def _input_options(book: FactBook, name: str) -> list[Any]:
    if name == "$documents":
        return [book.run.get("documents")]
    fact = book.facts.get(name)
    if fact is not None and fact.cardinality != "one":
        return [book.records(name)]                       # records, [] (none listed) or None (unknown)
    vals = book.values(name)
    return vals if vals else [None]


def _root_causes(book: FactBook, names: list[str]) -> list[str]:
    """Why inputs are missing, traced back to the document-level cause."""
    out: list[str] = []
    for n in names:
        d = book.derived.get(n)
        if d is not None and d.error:
            cause = d.error.removeprefix("cannot compute: ")
        elif n.startswith("$run."):
            cause = f"{n[5:].replace('_', ' ')} not available"
        else:
            spec = book.policy.fields.get(n)
            label = spec.description.split(". ")[0].rstrip(".") if spec else n
            fact = book.facts.get(n)
            where = f"not found in {', '.join(fact.missing_in)}" if fact and fact.missing_in else \
                "not available (no document of a type that carries it)"
            cause = f"{label}: {where}"
        if cause not in out:
            out.append(cause)
    return out


def compute_derived(book: FactBook) -> dict[str, Derived]:
    for name in _order(book):
        spec = book.policy.derived[name]
        d = Derived(name=name, type=spec.type, description=spec.description, fn=spec.fn, inputs=spec.inputs)
        options = [_input_options(book, i) for i in spec.inputs]
        params = [book.params[p] for p in spec.params]
        combos = list(itertools.islice(itertools.product(*options), MAX_COMBINATIONS))
        seen: dict[str, DerivedValue] = {}
        try:
            for combo in combos:
                value, working = FUNCTIONS[spec.fn](*combo, *params)
                basis = {i: display(v, book.type_of(i)) for i, v, opts in zip(spec.inputs, combo, options)
                         if len(opts) > 1}
                key = repr(value)
                if key not in seen:
                    seen[key] = DerivedValue(value=value, display=display(value, spec.type),
                                             working=working, basis=basis)
        except MissingInput as exc:
            causes = _root_causes(book, [i for i, o in zip(spec.inputs, options) if o == [None]])
            d.error = f"cannot compute: {'; '.join(causes) if causes else str(exc) + ' not available'}"
        except Exception as exc:                      # a bug, not a finding: surface it, don't hide it
            d.error = f"computation failed: {type(exc).__name__}: {exc}"
        d.candidates = list(seen.values()) if not d.error else []
        book.derived[name] = d
    return book.derived
