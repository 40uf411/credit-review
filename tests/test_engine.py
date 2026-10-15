"""The whole evaluation on the case dossier: every article outcome pinned, plus what
happens when documents change, go missing, or the policy changes (Part B)."""

import copy
from types import SimpleNamespace as NS

import pytest
import yaml

from credit_review.documents import DossierDocument
from credit_review.extract import extract_dossier
from credit_review.llm import LLMClient, LLMConfig
from credit_review.pipeline import compare, evaluate
from credit_review.policy import load_policy

from tests.conftest import ROOT

DECISION = "2026-10-12"

EXPECTED = {
    "1": "pass", "2.1": "pass", "2.2": "pass", "2.3": "pass",
    "3.1": "document_missing",        # August payslip already existed at application
    "3.2": "analyst_review",          # judgment, not run here
    "3.3": "warning",                 # child benefit with no child in the household
    "3.4": "fail",                    # DSTI 122.7%
    "3.5": "info", "3.6": "fail",     # free income negative
    "4.1": "fail",                    # BECRIS older than a month at the decision date
    "4.2": "pass", "4.3": "pass", "5.1": "pass",
    "5.2": "not_applicable",          # acquisition only
    "5.3": "analyst_review",          # 5.3.d plausibility is judgment
    "5.4": "unresolved",              # own funds: depends on which total investment is right
    "6": "document_missing",          # no bank statements (D8)
    "7.1": "warning",                 # salary advance, +75% pay jump
    "7.2": "escalate",                # fraud signals
    "7.3": "analyst_review",          # the conclusion is never Credit Review's
}


@pytest.fixture(scope="module")
def extracted(docs, policy, golden, tmp_path_factory):
    from credit_review.golden import GoldenProvider
    llm = LLMClient(LLMConfig(model="golden", cache_dir=tmp_path_factory.mktemp("cache")),
                    provider=GoldenProvider(golden))
    types = {n: d["doc_type"] for n, d in golden["documents"].items()}
    extractions = extract_dossier(docs, types, policy, llm)
    dossier = [DossierDocument(doc=d.name, sha256=d.sha256, doc_type=types[d.name]) for d in docs]
    return dossier, extractions


def run(policy, extracted, **kw):
    dossier, extractions = extracted
    return evaluate(policy, "sha", dossier, extractions, None, decision_date=kw.pop("decision", DECISION), **kw)


def outcomes(a):
    return {art.id: art.outcome for art in a.articles}


def check(a, cid):
    return next(c for art in a.articles for c in art.checks if c.id == cid)


# --- the case dossier ----------------------------------------------------------------

def test_every_article_outcome(policy, extracted):
    assert outcomes(run(policy, extracted)) == EXPECTED


def test_conflicts_and_precision_differences(policy, extracted):
    a = run(policy, extracted)
    by = {f.name: f.status for f in a.facts}
    assert {n for n, s in by.items() if s == "conflict"} == {
        "applicant.civil_status", "applicant.domicile_address", "renovation.total_investment"}
    assert {n for n, s in by.items() if s == "compatible"} == {"property.construction_year", "property.floor_area_m2"}
    assert by["applicant.rrn"] == "consistent"


def test_integrity_signals(policy, extracted):
    a = run(policy, extracted)
    failed = {r.id for r in a.integrity if r.status == "fail"}
    assert failed == {"I-06", "I-07", "I-08", "I-09", "I-10", "I-11", "I-13"}
    assert all(r.status == "pass" for r in a.integrity if r.id in {"I-01", "I-02", "I-03", "I-04", "I-05", "I-12"})


def test_dsti_is_explained_number_by_number(policy, extracted):
    c = check(run(policy, extracted), "3.4.a")
    assert "122.7%" in c.reason and c.outcome == "fail"
    assert any("€ 3,385.09 / € 2,758.33 = 122.7%" in w for w in c.working)
    assert any("reference rate" in w for w in c.working)
    assert {e.doc for e in c.evidence} >= {"credit_registry.pdf", "payslip_may2026.pdf"}


def test_unresolved_names_the_documents(policy, extracted):
    c = check(run(policy, extracted), "5.4.a")
    assert c.outcome == "unresolved" and {x.outcome for x in c.candidates} == {"pass", "fail"}
    assert "conformity.pdf" in c.reason and "application_form.pdf" in c.reason


def test_conclusion_is_never_evaluated(policy, extracted):
    assert check(run(policy, extracted), "7.3.a").outcome == "analyst_review"


def test_decision_date_drives_becris_age(policy, extracted):
    assert outcomes(run(policy, extracted, decision="2026-09-15"))["4.1"] == "pass"


# --- documents change ----------------------------------------------------------------

def without(extracted, name):
    dossier, ex = extracted
    return [d for d in dossier if d.doc != name], [e for e in ex if e.doc != name]


def test_missing_becris_is_never_a_silent_pass(policy, extracted):
    """Absence of evidence is not evidence of absence: without the register, nothing that
    depends on it may pass."""
    a = run(policy, without(extracted, "credit_registry.pdf"))
    assert check(a, "4.1.a").outcome == "document_missing"
    for cid in ("4.1.b", "4.2.a", "4.2.b", "4.3.a", "3.4.a", "3.6.a"):
        c = check(a, cid)
        assert c.outcome == "analyst_review" and "not available" in c.reason, (cid, c.outcome, c.reason)
    assert {r.id for r in a.integrity if r.status == "not_evaluable"} >= {"I-07", "I-08"}


def test_two_payslips_for_the_same_month_are_a_duplicate(policy, extracted):
    dossier, ex = extracted
    ex = copy.deepcopy(ex)
    june = next(e for e in ex if e.doc == "payslip_june2026.pdf")
    next(o for o in june.observations if o.path == "payslips.period").value = "2026-05"
    a = run(policy, (dossier, ex))
    assert check(a, "6.D3").outcome == "warning" and "2026-05" in check(a, "6.D3").reason


def test_consistent_documents_clear_the_conflict(policy, extracted):
    dossier, ex = extracted
    ex = copy.deepcopy(ex)
    conf = next(e for e in ex if e.doc == "conformity.pdf")
    next(o for o in conf.observations if o.field == "renovation.total_investment").value = 45788.05
    a = run(policy, (dossier, ex))
    assert outcomes(a)["5.4"] == "pass"


# --- Part B: the risk team changes the policy ------------------------------------------

V33 = ROOT / "policy" / "example_bank_v3.3.yaml"


def test_policy_change_v33_shows_exactly_what_it_changes(policy, extracted):
    """v3.3: a new rule (5.3.e, renovation term at most 15 years) and a tightened one (5.4, own
    funds 10% instead of 2%). On this file exactly two findings change, and 5.4 stops depending
    on which document is right because both candidate totals now fail."""
    report = load_policy(V33)
    assert report.ok, report.errors
    rows = compare(run(policy, extracted), run(report.policy, extracted))
    assert {r["check"]: (r["before"], r["after"]) for r in rows} == {
        "5.3.e": ("—", "fail"),
        "5.4.a": ("unresolved", "fail"),
    }
    reason = next(r["reason"] for r in rows if r["check"] == "5.4.a")
    assert reason.startswith("Fail whichever document is right") and "€ 5,260.00" in reason


def test_policy_change_reads_no_document_again(docs, golden, tmp_path):
    """The change touches only the bank's lane (parameters, rules), so every extraction under
    v3.3 is served from what was read under v3.2: the model is not called again."""
    from credit_review.golden import GoldenProvider

    class Counting(GoldenProvider):
        calls = 0

        def complete(self, req, model, max_tokens):
            Counting.calls += 1
            return super().complete(req, model, max_tokens)

    llm = LLMClient(LLMConfig(model="golden", cache_dir=tmp_path), provider=Counting(golden))
    types = {n: d["doc_type"] for n, d in golden["documents"].items()}
    extract_dossier(docs, types, load_policy(ROOT / "policy" / "example_bank_v3.2.yaml").policy, llm)
    first = Counting.calls
    extract_dossier(docs, types, load_policy(V33).policy, llm)
    assert first > 0 and Counting.calls == first


# --- judgment guardrails ----------------------------------------------------------------

class FakeJudge:
    name = "fake"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, req, model, max_tokens):
        return self.reply, {}, "json_schema", ""


def judged(policy, extracted, reply, tmp_path):
    llm = LLMClient(LLMConfig(model="m", cache_dir=tmp_path), provider=FakeJudge(reply))
    dossier, ex = extracted
    return check(evaluate(policy, "sha", dossier, ex, llm, decision_date=DECISION), "7.1.d")


def test_concerns_need_valid_citations(policy, extracted, tmp_path):
    c = judged(policy, extracted, {"outcome": "concern", "summary": "Stress signals.", "concerns": [
        {"text": "Salary advance in July.", "facts": ["F3"]},
        {"text": "Something invented.", "facts": ["F999"]}]}, tmp_path)
    assert c.outcome == "warning"
    assert len(c.judgment["concerns"]) == 1 and len(c.judgment["rejected"]) == 1


def test_a_concern_without_any_valid_citation_is_not_a_finding(policy, extracted, tmp_path):
    c = judged(policy, extracted, {"outcome": "concern", "summary": "x",
                                   "concerns": [{"text": "vague worry", "facts": []}]}, tmp_path)
    assert c.outcome == "analyst_review"


def test_no_concern_never_passes(policy, extracted, tmp_path):
    c = judged(policy, extracted, {"outcome": "no_concern_found", "summary": "Stable.", "concerns": []}, tmp_path)
    assert c.outcome == "analyst_review" and "analyst to confirm" in c.reason
