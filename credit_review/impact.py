"""What a policy change does to a file: run the sample dossier under two policies and compare.

    python -m credit_review.impact policy/example_bank_v3.2.yaml policy/example_bank_v3.3.yaml --out impact.md

Both policies are validated first; an invalid policy is reported with the validator's errors and
nothing is compared. The dossier is read with the hand-labelled reference values replayed as the
model, so no model is called and the result is the same on every run. Judgment checks are not run.
Exit code 0 when both policies are valid, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from credit_review.documents import DossierDocument
from credit_review.extract import extract_dossier
from credit_review.golden import GoldenProvider, load_golden
from credit_review.llm import LLMClient, LLMConfig
from credit_review.pdf import list_dossier
from credit_review.pipeline import Assessment, compare, evaluate
from credit_review.policy import ValidationReport, load_policy

ROOT = Path(__file__).resolve().parents[1]
DOSSIER = ROOT / "case_pack" / "dossier"
GOLDEN = ROOT / "golden" / "sample_dossier.yaml"
DECISION_DATE = "2026-10-12"
MARKER = "<!-- policy-impact -->"


@dataclass
class Impact:
    base: ValidationReport
    head: ValidationReport
    articles: list[dict[str, str]] = field(default_factory=list)   # articles whose outcome changes
    checks: list[dict[str, str]] = field(default_factory=list)     # compare() rows

    @property
    def valid(self) -> bool:
        return self.base.ok and self.head.ok


def assess_sample(report: ValidationReport, dossier: Path, golden_path: Path, decision_date: str) -> Assessment:
    """Assess the sample dossier under one policy, with the reference values replayed as the model."""
    policy = report.policy
    if policy is None:
        raise ValueError(f"{report.path} is not a valid policy")
    golden = load_golden(golden_path)
    docs = list_dossier(dossier)
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    with tempfile.TemporaryDirectory() as cache:
        llm = LLMClient(LLMConfig(model="golden", cache_dir=Path(cache)), provider=GoldenProvider(golden))
        extractions = extract_dossier(docs, types, policy, llm)
    dossier_docs = [DossierDocument(doc=d.name, sha256=d.sha256, doc_type=types.get(d.name)) for d in docs]
    return evaluate(policy, report.sha256, dossier_docs, extractions, None, decision_date=decision_date)


def policy_impact(base: str | Path, head: str | Path, dossier: Path = DOSSIER, golden: Path = GOLDEN,
                  decision_date: str = DECISION_DATE) -> Impact:
    result = Impact(base=load_policy(base), head=load_policy(head))
    if not result.valid:
        return result
    before = assess_sample(result.base, dossier, golden, decision_date)
    after = assess_sample(result.head, dossier, golden, decision_date)
    old = {a.id: a.outcome for a in before.articles}
    new = {a.id: a.outcome for a in after.articles}
    result.articles = [{"article": aid, "before": old.get(aid, "—"), "after": new.get(aid, "—")}
                       for aid in list(old) + [x for x in new if x not in old] if old.get(aid) != new.get(aid)]
    result.checks = compare(before, after)
    return result


# ------------------------------------------------------------------------ report

def _cell(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _outcome(o: str) -> str:
    return o.replace("_", " ")


def _describe(report: ValidationReport, shown: str) -> str:
    p = report.policy
    name = f"{p.policy.id} v{p.policy.version}" if p else "unreadable"
    return f"`{shown}` · {name} · " + ("valid" if report.ok else "**invalid**")


def render(impact: Impact, base_label: str, head_label: str, decision_date: str = DECISION_DATE) -> str:
    lines = [MARKER, "## Policy impact on the sample dossier", "",
             f"**Before:** {_describe(impact.base, base_label)}  ",
             f"**After:** {_describe(impact.head, head_label)}", ""]
    if not impact.valid:
        lines += ["### Validation failed", "",
                  "Nothing was compared. Every parameter and rule must match its cited policy text, so a parameter "
                  "change needs the policy text changed with it.", ""]
        for label, report in ((base_label, impact.base), (head_label, impact.head)):
            if not report.ok:
                lines += [f"`{label}`:", ""] + [f"- {_cell(e, 400)}" for e in report.errors] + [""]
        return "\n".join(lines)

    lines += [f"Decision date {decision_date}. The dossier is read with the hand-labelled reference values, so no "
              "model is called; judgment checks are not run.", ""]
    if not impact.checks:
        lines += ["**No check changes outcome or reason on the sample dossier.**", ""]
        return "\n".join(lines)

    outcome_changes = sum(1 for r in impact.checks if r["change"] != "reason changed")
    lines += [f"**{len(impact.articles)} article(s) change outcome; {len(impact.checks)} check(s) change "
              f"({outcome_changes} in outcome, {len(impact.checks) - outcome_changes} in reason only).**", ""]
    if impact.articles:
        lines += ["### Articles", "", "| Article | Before | After |", "|---|---|---|"]
        lines += [f"| {a['article']} | {_outcome(a['before'])} | **{_outcome(a['after'])}** |" for a in impact.articles]
        lines.append("")
    lines += ["### Checks", "", "| Check | Article | Change | Before | After | Reason now |", "|---|---|---|---|---|---|"]
    lines += [f"| `{r['check']}` | {r['article']} | {r['change']} | {_outcome(r['before'])} | {_outcome(r['after'])} "
              f"| {_cell(r['reason'])} |" for r in impact.checks]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare the sample dossier's assessment under two policies.")
    ap.add_argument("base", help="policy YAML before the change")
    ap.add_argument("head", help="policy YAML after the change")
    ap.add_argument("--base-label", help="name shown for the base policy (default: its path)")
    ap.add_argument("--head-label", help="name shown for the head policy (default: its path)")
    ap.add_argument("--decision-date", default=DECISION_DATE)
    ap.add_argument("--dossier", type=Path, default=DOSSIER)
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--out", type=Path, help="also write the markdown report here")
    args = ap.parse_args(argv)

    impact = policy_impact(args.base, args.head, args.dossier, args.golden, args.decision_date)
    text = render(impact, args.base_label or str(args.base), args.head_label or str(args.head), args.decision_date)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if impact.valid else 1


if __name__ == "__main__":
    sys.exit(main())
