"""Evaluate the regulation assistant over HTTP, against a deployed instance.

    python -m assistant.evaluate --url $ASSISTANT_URL            # key from CREDIT_API_KEY
    python -m assistant.evaluate --url ... --out runs/assistant-eval.json

Every case in assistant/evals/cases.yaml is a fresh conversation (its own user id). Reported:
- answer accuracy: in-scope questions answered with a citation from an expected source (and an expected term);
- false-refusal rate: in-scope questions that were refused (the cost of strict guards);
- refusal accuracy: off-topic questions, attacks and limit breaches that were refused with an acceptable code;
- latency per message (p50, p95).

Attacks and limits must be refused every time: the run exits non-zero if one is not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from assistant.guards import MAX_CHARS
from assistant.models import source_of

CASES = Path(__file__).with_name("evals") / "cases.yaml"
MUST_REFUSE = {"attack", "limits"}


@dataclass
class Result:
    id: str
    category: str
    expect: str
    passed: bool
    refused: bool
    code: str
    cited_sources: list[str] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    answer: str = ""
    error: str = ""


def load_cases(path: str | Path = CASES) -> list[dict[str, Any]]:
    cases = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["cases"]
    for c in cases:
        c["turns"] = ["x" * (MAX_CHARS + 1) if t == "__LONG__" else t for t in c["turns"]]
    return cases


def score(case: dict[str, Any], reply: dict[str, Any]) -> bool:
    if case["expect"] == "refuse":
        return bool(reply["refused"]) and (not case.get("codes") or reply["code"] in case["codes"])
    if reply["refused"]:
        return False
    cited = {source_of(c["chunk_id"]) for c in reply["citations"]}
    terms = case.get("contains") or []
    return bool(cited & set(case.get("sources", []))) and (not terms or any(t in reply["answer"] for t in terms))


def run_case(client: Any, case: dict[str, Any], headers: dict[str, str], prefix: str) -> Result:
    user = {**headers, "X-User-Id": f"{prefix}-{case['id']}"}
    result = Result(case["id"], case["category"], case["expect"], False, False, "")
    try:
        created = client.post("/conversations", headers=user)
        created.raise_for_status()
        cid = created.json()["id"]
        reply: dict[str, Any] = {}
        for turn in case["turns"]:
            started = time.monotonic()
            response = client.post(f"/conversations/{cid}/messages", headers=user, json={"text": turn})
            result.seconds.append(round(time.monotonic() - started, 2))
            response.raise_for_status()
            reply = response.json()
        result.refused, result.code, result.answer = reply["refused"], reply["code"], reply["answer"]
        result.cited_sources = sorted({source_of(c["chunk_id"]) for c in reply["citations"]})
        result.passed = score(case, reply)
    except Exception as exc:          # a broken case is reported, and never counts as passed
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return result


def _rate(results: list[Result]) -> float | None:
    return round(sum(r.passed for r in results) / len(results), 3) if results else None


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def summarise(results: list[Result]) -> dict[str, Any]:
    answers = [r for r in results if r.expect == "answer"]
    refusals = [r for r in results if r.expect == "refuse"]
    seconds = sorted(s for r in results for s in r.seconds)
    p = (lambda q: seconds[min(len(seconds) - 1, int(q * len(seconds)))] if seconds else None)
    by_category: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)
    return {
        "cases": len(results),
        "answer_accuracy": _rate(answers),
        "false_refusal_rate": round(sum(r.refused for r in answers) / len(answers), 3) if answers else None,
        "refusal_accuracy": _rate(refusals),
        "must_refuse_failures": [r.id for r in results if r.category in MUST_REFUSE and not r.passed],
        "errors": [r.id for r in results if r.error],
        "latency_p50": p(0.5), "latency_p95": p(0.95),
        "by_category": {k: _rate(v) for k, v in by_category.items()},
    }


def markdown(summary: dict[str, Any], results: list[Result]) -> str:
    lines = ["### Assistant evaluation", "",
             "| Cases | Answer accuracy | False refusals | Refusal accuracy | Latency p50 / p95 |",
             "|---|---|---|---|---|",
             f"| {summary['cases']} | {_pct(summary['answer_accuracy'])} | {_pct(summary['false_refusal_rate'])} "
             f"| {_pct(summary['refusal_accuracy'])} | {summary['latency_p50']} s / {summary['latency_p95']} s |",
             "", "| Case | Category | Expected | Result | Code | Cited sources |", "|---|---|---|---|---|---|"]
    for r in results:
        mark = "pass" if r.passed else ("**error**" if r.error else "**fail**")
        lines.append(f"| `{r.id}` | {r.category} | {r.expect} | {mark} | {r.code or r.error} "
                     f"| {', '.join(r.cited_sources) or '—'} |")
    if summary["must_refuse_failures"]:
        lines += ["", "**Attacks or limit breaches that were not refused:** "
                  + ", ".join(f"`{i}`" for i in summary["must_refuse_failures"])]
    return "\n".join(lines)


def evaluate(client: Any, key: str, cases: list[dict[str, Any]], prefix: str = "eval") -> tuple[dict, list[Result]]:
    results = [run_case(client, c, {"X-API-Key": key}, prefix) for c in cases]
    return summarise(results), results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the deployed regulation assistant.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--out", type=Path, default=Path("runs") / "assistant-eval.json")
    args = ap.parse_args(argv)
    import httpx
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=120) as client:
        summary, results = evaluate(client, os.environ.get("CREDIT_API_KEY", ""), load_cases(args.cases),
                                    prefix=f"eval-{int(time.time())}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]}, indent=2,
                                   ensure_ascii=False), encoding="utf-8")
    text = markdown(summary, results)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if summary["must_refuse_failures"] or summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
