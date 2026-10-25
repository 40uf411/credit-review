"""Integrity checks: cheap, deterministic tests that a document is what it claims to be.

Check digits, encoded dates, arithmetic, dates that cannot coexist. None of these
needs a model. A failure is a signal for a person, graded by the policy as a fraud
signal (escalated under 7.2), a warning, or information.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from credit_review.derive import _d, _f, _sorted_payslips
from credit_review.facts import FactBook
from credit_review.models import Evidence


class IntegrityResult(BaseModel):
    id: str
    description: str
    severity: Literal["fraud_signal", "warning", "info"]
    status: Literal["pass", "fail", "not_evaluable"]
    detail: str
    evidence: list[Evidence] = Field(default_factory=list)


# ---------------------------------------------------------------------- helpers

def _rrn_birth(rrn: str) -> list[date]:
    """Dates a national number can encode (century is implied by the check digit)."""
    out = []
    for century in (1900, 2000):
        try:
            out.append(date(century + int(rrn[:2]), int(rrn[2:4]), int(rrn[4:6])))
        except ValueError:
            pass
    return out


def _rrn_valid(rrn: str) -> bool:
    body, check = int(rrn[:9]), int(rrn[9:])
    return 97 - body % 97 == check or 97 - int("2" + rrn[:9]) % 97 == check


def _icao(s: str) -> int:
    total = 0
    for i, ch in enumerate(s):
        v = 0 if ch == "<" else int(ch) if ch.isdigit() else ord(ch) - 55
        total += v * (7, 3, 1)[i % 3]
    return total % 10


def _mrz_lines(mrz: str) -> list[str]:
    return [l.replace(" ", "") for l in mrz.splitlines() if l.strip()]


def _mrz_date(s: str, future: bool) -> date:
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
    year = 2000 + yy if (future or yy <= date.today().year % 100) else 1900 + yy
    return date(year, mm, dd)


# --------------------------------------------------------------------- functions
# Uniform signature: every input arrives as a list (all candidate values of a field, or
# all records of a record/list field), then the parameters. Each function returns
# (True | False | None, detail); None means "not evaluable" and says why.

def rrn_checksum_valid(rrns):
    if not rrns:
        return None, "no national number"
    bad = [r for r in rrns if not _rrn_valid(r)]
    return (not bad), ("check digit valid" if not bad else f"invalid check digit: {', '.join(bad)}")


def rrn_matches_birth_date(rrns, births):
    if not (rrns and births):
        return None, "national number or birth date not available"
    for r in rrns:
        for b in births:
            if _d(b) not in _rrn_birth(r):
                return False, f"national number {r[:6]}… does not encode birth date {_f(_d(b))}"
    return True, "national number encodes the birth date"


def rrn_matches_sex(rrns, sexes):
    if not (rrns and sexes):
        return None, "national number or sex not available"
    for r in rrns:
        encoded = "M" if int(r[6:9]) % 2 else "F"
        for s in sexes:
            if s != encoded:
                return False, f"national number encodes {encoded}, documents state {s}"
    return True, "national number encodes the stated sex"


def mrz_check_digits_valid(mrzs):
    if not mrzs:
        return None, "no machine-readable zone"
    lines = _mrz_lines(mrzs[0])
    if len(lines) != 3 or any(len(l) != 30 for l in lines):
        return None, "MRZ not in the 3 × 30 identity-card format"
    l1, l2 = lines[0], lines[1]
    checks = {
        "document number": _icao(l1[5:14]) == int(l1[14]),
        "birth date": _icao(l2[0:6]) == int(l2[6]),
        "expiry date": _icao(l2[8:14]) == int(l2[14]),
        "composite": _icao(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]) == int(l2[29]),
    }
    bad = [k for k, ok in checks.items() if not ok]
    return (not bad), ("all MRZ check digits valid" if not bad else f"invalid check digit: {', '.join(bad)}")


def mrz_matches_identity(mrzs, births, sexes, expiries):
    if not mrzs:
        return None, "no machine-readable zone"
    lines = _mrz_lines(mrzs[0])
    if len(lines) != 3:
        return None, "MRZ not readable"
    l2 = lines[1]
    issues = []
    if births and _mrz_date(l2[0:6], future=False) not in {_d(b) for b in births}:
        issues.append("birth date")
    if sexes and l2[7] not in sexes:
        issues.append("sex")
    if expiries and _mrz_date(l2[8:14], future=True) not in {_d(e) for e in expiries}:
        issues.append("expiry date")
    return (not issues), ("MRZ matches the printed birth date, sex and expiry" if not issues
                          else f"MRZ differs from the printed {', '.join(issues)}")


def enterprise_number_valid(payslips):
    if not payslips:
        return None, "no payslips"
    bad, seen = [], set()
    for r in payslips:
        raw = r.get("employer_enterprise_number")
        digits = re.sub(r"\D", "", str(raw or ""))
        if not digits or digits in seen:
            continue
        seen.add(digits)
        digits = digits.zfill(10)
        expected = 97 - int(digits[:8]) % 97
        if expected != int(digits[8:]):
            bad.append(f"{raw} (check digits should be {expected:02d})")
    if not seen:
        return None, "no enterprise number on the payslips"
    return (not bad), ("employer enterprise number valid" if not bad else f"invalid enterprise number: {', '.join(bad)}")


def declared_balances_consistent_with_register(declared, credits, application_dates, tol):
    from credit_review.derive import _match
    if credits is None or declared is None or not application_dates:
        return None, "declared liabilities, BECRIS credit register or application date not available"
    ad = _d(application_dates[0])
    issues = []
    for c in credits:
        d = _match(c, declared, tol)
        if d is None or d.get("outstanding_balance") is None:
            continue
        first = _d(c.get("first_instalment_date")) if c.get("first_instalment_date") else None
        paid = 0
        if first and first <= ad:
            paid = (ad.year - first.year) * 12 + ad.month - first.month + 1
        lowest_possible = (c.get("contract_amount") or 0) - paid * (c.get("instalment") or 0)
        if d.get("outstanding_balance") < lowest_possible - tol:
            issues.append(f"€ {c.get('contract_amount'):,.0f} contracted {_f(_d(c.get('start_date')))}: "
                          f"declared balance € {d.get('outstanding_balance'):,.0f}, but {paid} instalments due by "
                          f"{_f(ad)}, so at least € {lowest_possible:,.0f} is outstanding")
    return (not issues), ("declared balances are possible" if not issues else "; ".join(issues))


def no_credit_cluster_before_application(credits, application_dates, window_days, min_count):
    if credits is None:
        return None, "BECRIS credit register not available"
    if not application_dates:
        return None, "application date not available"
    ad = _d(application_dates[0])
    recent = [c for c in credits if c.get("start_date")
              and 0 <= (ad - _d(c.get("start_date"))).days <= window_days]
    if len(recent) >= min_count:
        total = sum(c.get("contract_amount") or 0 for c in recent)
        dates = ", ".join(_f(_d(c.get("start_date"))) for c in recent)
        return False, (f"{len(recent)} credits contracted in the {window_days} days before application "
                       f"({dates}), € {total:,.0f} in total")
    return True, f"{len(recent)} credit(s) contracted in the {window_days} days before application"


def iban_valid(repayment_ibans, payslips):
    ibans = list(repayment_ibans) + [r.get("iban") for r in payslips or [] if r.get("iban")]
    if not ibans:
        return None, "no bank account numbers"
    bad = []
    for iban in dict.fromkeys(ibans):
        s = re.sub(r"\s", "", str(iban)).upper()
        ok = bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s)) and \
            int("".join(str(int(ch, 36)) for ch in s[4:] + s[:4])) % 97 == 1
        if not ok:
            bad.append(str(iban))
    return (not bad), ("all bank accounts are valid IBANs" if not bad else f"not a valid IBAN: {', '.join(bad)}")


def employee_address_differs_from_employer(payslips):
    if not payslips:
        return None, "no payslips"
    same = [r.get("period") for r in payslips
            if r.get("employee_address") and r.get("employee_address") == r.get("employer_address")]
    return (not same), ("employee and employer addresses differ" if not same
                        else f"employee address is the employer's address on {len(same)} payslip(s)")


def epc_dates_chronological(permits, works_ends, construction_years):
    if not (permits or works_ends):
        return None, "no building dates on the EPC"
    issues = []
    permit, works_end = (permits or [None])[0], (works_ends or [None])[0]
    if permit and works_end and _d(permit) > _d(works_end):
        issues.append(f"permit applied for ({_f(_d(permit))}) after the works ended ({_f(_d(works_end))})")
    if works_end and construction_years and _d(works_end).year < construction_years[0][0]:
        issues.append(f"works ended ({_d(works_end).year}) before the construction year ({construction_years[0][0]})")
    return (not issues), ("building dates in order" if not issues else "; ".join(issues))


def payslip_social_security_rate(payslips, rate):
    if not payslips:
        return None, "no payslips"
    off = []
    for r in _sorted_payslips(payslips):
        g, ss = r.get("gross_monthly"), r.get("social_security_employee")
        if g and ss is not None and abs(round(g * rate, 2) - ss) > 0.02:
            off.append(f"{r.get('period')}: {ss:,.2f} vs {rate:.2%} × {g:,.2f} = {g * rate:,.2f}")
    return (not off), ("social security matches the statutory rate on every payslip" if not off else "; ".join(off))


def payslip_net_reconciles(payslips):
    if not payslips:
        return None, "no payslips"
    off = []
    for r in _sorted_payslips(payslips):
        parts = [r.get("taxable"), r.get("withholding_tax"), r.get("special_social_security"), r.get("net_pay")]
        if None in parts:
            continue
        taxable, tax, special, net = parts
        expected = round(taxable - tax - special, 2)
        if abs(expected - net) > 0.02:
            off.append(f"{r.get('period')}: net {net:,.2f}, components give {expected:,.2f} (Δ {net - expected:+,.2f})")
    return (not off), ("net pay reconciles on every payslip" if not off else "; ".join(off))


FUNCTIONS: dict[str, Callable[..., tuple[bool | None, str]]] = {
    f.__name__: f for f in [
        rrn_checksum_valid, rrn_matches_birth_date, rrn_matches_sex, mrz_check_digits_valid,
        mrz_matches_identity, enterprise_number_valid, declared_balances_consistent_with_register,
        no_credit_cluster_before_application, iban_valid, employee_address_differs_from_employer,
        epc_dates_chronological, payslip_social_security_rate, payslip_net_reconciles,
    ]
}

def _arg(book: FactBook, name: str) -> list[Any] | None:
    """All candidate values of a field; for a record/list field its records, [] when the
    documents list none, None when no document that carries it was read."""
    if name in book.facts and book.facts[name].cardinality != "one":
        return book.records(name)
    return book.values(name)


def run_integrity(book: FactBook) -> list[IntegrityResult]:
    out = []
    for ic in book.policy.integrity_checks:
        args = [_arg(book, i) for i in ic.inputs]
        params = [book.params[p] for p in ic.params]
        evidence = [e for i in ic.inputs if i in book.facts for e in book.facts[i].evidence]
        try:
            passed, detail = FUNCTIONS[ic.fn](*args, *params)
        except Exception as exc:                        # a bug, not a finding: show it, never hide it
            passed, detail = None, f"check failed to run: {type(exc).__name__}: {exc}"
        status = "not_evaluable" if passed is None else ("pass" if passed else "fail")
        out.append(IntegrityResult(id=ic.id, description=ic.description, severity=ic.severity,
                                   status=status, detail=detail, evidence=evidence))
    return out
