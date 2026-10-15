"""Policy validation tests.

The shipped policy must validate cleanly, and the validator must catch the
mistakes that would silently break an assessment: unknown references,
untraceable rules, a model allowed to 'pass', impossible consistency checks.
"""

import copy
from pathlib import Path

import pytest
import yaml

from credit_review.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policy" / "example_bank_v3.2.yaml"
SOURCE = ROOT / "policy" / "example_bank_credit_policy_extract.md"


@pytest.fixture
def policy_data() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def validate(data: dict, tmp_path: Path, text: list[tuple[str, str]] = ()):
    """Write a modified policy next to a copy of the source document (with the bank's own
    text edits, if any) and validate it."""
    src = SOURCE.read_text(encoding="utf-8")
    for old, new in text:
        assert old in src, old
        src = src.replace(old, new)
    (tmp_path / SOURCE.name).write_text(src, encoding="utf-8")
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_policy(path)


def find_check(data: dict, check_id: str) -> dict:
    for section in data["sections"]:
        for article in section["articles"]:
            for check in article["checks"]:
                if check["id"] == check_id:
                    return check
    raise KeyError(check_id)


def article(data: dict, article_id: str) -> dict:
    return next(a for s in data["sections"] for a in s["articles"] if a["id"] == article_id)


# --- the shipped policy ------------------------------------------------------

def test_shipped_policy_is_valid_without_warnings():
    report = load_policy(POLICY)
    assert report.errors == []
    assert report.warnings == []


def test_every_policy_article_is_covered():
    report = load_policy(POLICY)
    covered = {a.id for s in report.policy.sections for a in s.articles}
    expected = {"1", "2.1", "2.2", "2.3", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6",
                "4.1", "4.2", "4.3", "5.1", "5.2", "5.3", "5.4", "6", "7.1", "7.2", "7.3"}
    assert covered == expected


def test_every_bank_threshold_is_a_parameter_not_a_literal():
    report = load_policy(POLICY)
    for _, _, check in report.policy.checks():
        if check.op in ("at_most", "at_least", "less_than"):
            assert check.limit is not None


def test_conclusion_is_reserved_for_the_analyst():
    report = load_policy(POLICY)
    check = next(c for _, a, c in report.policy.checks() if a.id == "7.3")
    assert check.op == "reserved_for_analyst"


# --- traceability --------------------------------------------------------------

def test_invented_rule_text_is_rejected(policy_data, tmp_path):
    article(policy_data, "3.4")["source"] = "The DSTI may not exceed 50%."
    report = validate(policy_data, tmp_path)
    assert any("article 3.4" in e and "verbatim" in e for e in report.errors)


# --- references ----------------------------------------------------------------

def test_unknown_field_is_rejected(policy_data, tmp_path):
    find_check(policy_data, "3.4.a")["field"] = "dsti_typo"
    report = validate(policy_data, tmp_path)
    assert any("unknown field 'dsti_typo'" in e for e in report.errors)


def test_unknown_parameter_is_rejected(policy_data, tmp_path):
    find_check(policy_data, "3.4.a")["limit"] = {"param": "max_dsti_v2"}
    report = validate(policy_data, tmp_path)
    assert any("unknown parameter 'max_dsti_v2'" in e for e in report.errors)


def test_function_outside_catalogue_is_rejected(policy_data, tmp_path):
    policy_data["derived"]["dsti"]["fn"] = "magic_ratio"
    report = validate(policy_data, tmp_path)
    assert any("not in the Example Systems catalogue" in e for e in report.errors)


def test_derived_cycle_is_rejected(policy_data, tmp_path):
    policy_data["derived"]["obligations.total_monthly"]["inputs"].append("dsti")
    report = validate(policy_data, tmp_path)
    assert any("derived cycle" in e for e in report.errors)


# --- guardrails on AI judgment ---------------------------------------------------

def test_model_may_never_map_to_pass(policy_data, tmp_path):
    policy_data["judgment"]["outcome_mapping"]["no_concern_found"] = "pass"
    report = validate(policy_data, tmp_path)
    assert any("never map to 'pass'" in e for e in report.errors)


def test_judgment_op_requires_judgment_kind(policy_data, tmp_path):
    find_check(policy_data, "7.1.d")["kind"] = "deterministic"
    report = validate(policy_data, tmp_path)
    assert any("must have kind 'judgment'" in e for e in report.errors)


def test_consistency_only_on_single_valued_fields(policy_data, tmp_path):
    find_check(policy_data, "5.1.c")["fields"].append("becris.credits")
    report = validate(policy_data, tmp_path)
    assert any("only 'one' fields can conflict" in e for e in report.errors)


# --- Part B rehearsal: the risk team changes the policy ---------------------------

def test_tightening_a_threshold_is_pure_configuration(policy_data, tmp_path):
    """Parameters, the bank's text and the description change together; no code changes."""
    policy_data["parameters"]["max_dsti"]["value"] = 0.40
    policy_data["parameters"]["dsti_exception_threshold"]["value"] = 0.35
    art = article(policy_data, "3.4")
    art["source"] = art["source"].replace("exceed 45%. Between 40% and 45%", "exceed 40%. Between 35% and 40%")
    find_check(policy_data, "3.4.a")["description"] = (
        "DSTI does not exceed 40%; between 35% and 40% a motivated senior-analyst exception is required.")
    report = validate(policy_data, tmp_path,
                      text=[("exceed **45%**. Between 40% and 45%", "exceed **40%**. Between 35% and 40%")])
    assert report.ok, report.errors


def test_new_rule_on_existing_facts_is_pure_configuration(policy_data, tmp_path):
    sentence = " The credit term may not exceed 25 years."
    article(policy_data, "2.2")["source"] += sentence
    article(policy_data, "2.2")["checks"].append({
        "id": "2.2.c", "description": "Credit term is at most 25 years.",
        "kind": "deterministic", "op": "at_most", "field": "credit.term_years",
        "limit": {"param": "max_term_years"}, "on_fail": "fail",
    })
    policy_data["parameters"]["max_term_years"] = {
        "value": 25, "unit": "years", "owner": "example_bank", "source": "2.2",
        "description": "Maximum credit term.",
    }
    used = "the youngest applicant's age is used."
    report = validate(policy_data, tmp_path, text=[(used, used + sentence)])
    assert report.ok, report.errors


def test_new_rule_needing_an_unextracted_fact_fails_loudly(policy_data, tmp_path):
    article(policy_data, "3.1")["checks"].append({
        "id": "3.1.c", "description": "Applicant has a permanent employment contract.",
        "kind": "deterministic", "op": "is_true", "field": "applicant.permanent_contract",
        "on_fail": "warning",
    })
    report = validate(policy_data, tmp_path)
    assert any("unknown field 'applicant.permanent_contract'" in e for e in report.errors)


# --- a policy change must be complete ----------------------------------------

def test_shipped_change_v33_is_valid():
    report = load_policy(ROOT / "policy" / "example_bank_v3.3.yaml")
    assert report.errors == []
    assert report.warnings == []
    assert report.policy.parameters["min_own_funds_share"].value == 0.10


def test_parameter_changed_without_the_policy_text_is_rejected(policy_data, tmp_path):
    policy_data["parameters"]["max_age_at_maturity"]["value"] = 70
    errors = validate(policy_data, tmp_path).errors
    assert any("max_age_at_maturity = 70 (years) is not stated in the text of article 2.2" in e for e in errors)
    assert any(e.startswith("check 2.2.b: description mentions 76") for e in errors)


def test_stale_description_is_rejected_even_when_the_text_still_matches(policy_data, tmp_path):
    # DSTI 45% -> 40%: article 3.4 still contains "40%" (the exception band), so the text check
    # alone would pass; the description of 3.4.a still promises 45%.
    policy_data["parameters"]["max_dsti"]["value"] = 0.40
    errors = validate(policy_data, tmp_path).errors
    assert not [e for e in errors if e.startswith("parameter max_dsti")]
    assert any(e.startswith("check 3.4.a: description mentions 45") for e in errors)


def test_list_parameter_must_match_the_text(policy_data, tmp_path):
    policy_data["parameters"]["eligible_energy_measures"]["value"].append("heat pump")
    errors = validate(policy_data, tmp_path).errors
    assert any("heat pump not found in the text of article 5.3" in e for e in errors)
