"""The policy-impact report that CI posts on policy pull requests."""

import pytest

from credit_review.impact import MARKER, main, policy_impact, render
from tests.conftest import DOSSIER, ROOT

V32 = ROOT / "policy" / "example_bank_v3.2.yaml"
V33 = ROOT / "policy" / "example_bank_v3.3.yaml"


@pytest.fixture(autouse=True)
def _dossier():
    if not DOSSIER.exists():
        pytest.skip("case pack not unpacked in case_pack/dossier")


def test_v33_changes_own_funds_and_adds_the_renovation_term_rule():
    impact = policy_impact(V32, V33)
    assert impact.valid
    assert {a["article"]: (a["before"], a["after"]) for a in impact.articles} == {
        "5.3": ("analyst_review", "fail"),
        "5.4": ("unresolved", "fail"),
    }
    checks = {r["check"]: r for r in impact.checks}
    assert (checks["5.4.a"]["before"], checks["5.4.a"]["after"]) == ("unresolved", "fail")
    assert checks["5.3.e"]["change"] == "added"
    text = render(impact, "v3.2", "v3.3")
    assert text.startswith(MARKER)
    assert "| 5.4 | unresolved | **fail** |" in text


def test_same_policy_on_both_sides_reports_no_change():
    impact = policy_impact(V32, V32)
    assert impact.valid and not impact.articles and not impact.checks
    assert "No check changes outcome or reason" in render(impact, "a", "b")


def test_parameter_changed_without_the_policy_text_is_rejected(tmp_path, capsys):
    """The validator ties every parameter to its cited policy text, so the report shows the errors
    and compares nothing. Exit code 1 fails the CI step."""
    for f in (ROOT / "policy").iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    changed = tmp_path / V32.name
    text = changed.read_text(encoding="utf-8")
    old = "min_own_funds_share:          {value: 0.02,"
    assert text.count(old) == 1
    changed.write_text(text.replace(old, "min_own_funds_share:          {value: 0.10,"), encoding="utf-8")

    assert main([str(V32), str(changed), "--head-label", "head"]) == 1
    out = capsys.readouterr().out
    assert "### Validation failed" in out
    assert "min_own_funds_share = 0.1" in out and "not stated in the text of article 5.4" in out
