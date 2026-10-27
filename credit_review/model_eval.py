"""Score one model on the hand-labelled reference set: accuracy, cost and latency for the sample dossier.

    # one model (here an Azure OpenAI deployment; signs in with `az login` or a managed identity)
    CREDIT_REVIEW_LLM_PROVIDER=azure_openai CREDIT_REVIEW_LLM_BASE_URL=https://<resource>.openai.azure.com \\
    CREDIT_REVIEW_LLM_MODEL=<deployment> python -m credit_review.model_eval run --fresh --out runs/eval/<deployment>.json

    # the same deployment at another reasoning effort (reasoning models only)
    ... python -m credit_review.model_eval run --fresh --reasoning-effort low --out runs/eval/<deployment>-low.json

    # compare the runs
    python -m credit_review.model_eval table runs/eval/*.json

Document types come from the reference labels, so only extraction is measured. Scanned documents
(read from page images) and text documents are scored separately. --fresh calls the model for every
document instead of reusing cached replies, which is what latency and cost should be measured on.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from credit_review.extract import PROMPT_VERSION, extract_dossier
from credit_review.golden import evaluate, load_golden, score
from credit_review.impact import DOSSIER, GOLDEN
from credit_review.llm import LLMClient, LLMConfig
from credit_review.models import DocumentExtraction
from credit_review.pdf import list_dossier
from credit_review.policy import load_policy

POLICY = Path(__file__).resolve().parents[1] / "policy" / "example_bank_v3.2.yaml"


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


def summarise(extractions: list[DocumentExtraction], golden: dict[str, Any], config: LLMConfig,
              wall_seconds: float) -> dict[str, Any]:
    rows = evaluate(extractions, golden)
    overall = score(rows)
    method = {e.doc: e.method for e in extractions}
    by_method = {m: score([r for r in rows if method.get(r["doc"]) == m]) for m in ("text", "vision")}
    calls = [e.llm for e in extractions if e.llm]
    fresh = [c for c in calls if not c.cached]
    costs = [c.cost_eur for c in calls]
    modes = Counter(c.mode for c in calls)
    return {
        "model": config.model, "reasoning_effort": config.reasoning_effort,
        "provider": config.label or config.provider, "prompt_version": PROMPT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accuracy": overall["accuracy"], "matched": overall["match"], "expected": overall["expected"],
        "wrong": overall["mismatch"], "missed": overall["missed"], "extra": overall["extra"],
        "accuracy_text": by_method["text"]["accuracy"], "accuracy_scans": by_method["vision"]["accuracy"],
        "calls": len(calls), "cached_calls": len(calls) - len(fresh), "modes": dict(modes),
        "repair_rate": round(modes.get("prompt+repair", 0) / len(calls), 3) if calls else None,
        # cost of reading the dossier once, whether or not this run paid it (cached replies keep their cost)
        "cost_eur": round(sum(c for c in costs if c is not None), 4) if costs and None not in costs else None,
        "latency_p50": _percentile([c.seconds for c in fresh], 0.50),
        "latency_p95": _percentile([c.seconds for c in fresh], 0.95),
        "wall_seconds": round(wall_seconds, 1),
        "errors": {e.doc: e.error for e in extractions if e.error},
        "differences": [r for r in rows if r["status"] != "match"],
    }


def run(args: argparse.Namespace) -> int:
    report = load_policy(args.policy)
    if not report.ok or report.policy is None:
        print("Policy is not valid:\n" + "\n".join(report.errors))
        return 1
    golden = load_golden(args.golden)
    docs = list_dossier(args.dossier)
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    with tempfile.TemporaryDirectory() as fresh_cache:
        overrides: dict[str, Any] = {"cache_dir": Path(fresh_cache)} if args.fresh else {}
        if args.reasoning_effort:
            overrides["reasoning_effort"] = args.reasoning_effort
        config = LLMConfig.from_env(**overrides)
        llm = LLMClient(config)
        started = time.monotonic()
        extractions = extract_dossier(docs, types, report.policy, llm, workers=args.workers)
        result = summarise(extractions, golden, config, time.monotonic() - started)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(table([result]))
    return 0 if not result["errors"] else 1


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def table(results: list[dict[str, Any]]) -> str:
    lines = ["| Model | Accuracy | Text documents | Scans | Wrong / missed | Repair rounds | Cost per dossier | "
             "Latency p50 / p95 | Errors |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (-(r["accuracy"] or 0), r["cost_eur"] or 0)):
        cost = "n/a" if r["cost_eur"] is None else f"€ {r['cost_eur']:.4f}"
        latency = "n/a" if r["latency_p50"] is None else f"{r['latency_p50']:.1f} s / {r['latency_p95']:.1f} s"
        label = r["model"] + (f" @ {r['reasoning_effort']}" if r.get("reasoning_effort") else "")
        lines.append(f"| `{label}` | **{_pct(r['accuracy'])}** ({r['matched']}/{r['expected']}) "
                     f"| {_pct(r['accuracy_text'])} | {_pct(r['accuracy_scans'])} | {r['wrong']} / {r['missed']} "
                     f"| {_pct(r['repair_rate'])} | {cost} | {latency} | {len(r['errors'])} |")
    lines += ["", f"Reference set: {results[0]['expected'] if results else 0} hand-labelled values; prompt "
              f"{results[0]['prompt_version'] if results else '?'}. One value is about "
              f"{1 / results[0]['expected']:.1%}, so differences of one or two values are noise."
              if results and results[0]["expected"] else ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a model on the hand-labelled reference set.")
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="extract the sample dossier with the configured model and score it")
    r.add_argument("--fresh", action="store_true", help="ignore cached replies: call the model for every document")
    r.add_argument("--reasoning-effort", help="reasoning models only, e.g. low | medium | high (default: the model's)")
    r.add_argument("--out", type=Path, help="write the result as JSON")
    r.add_argument("--policy", type=Path, default=POLICY)
    r.add_argument("--dossier", type=Path, default=DOSSIER)
    r.add_argument("--golden", type=Path, default=GOLDEN)
    r.add_argument("--workers", type=int, default=4)
    t = sub.add_parser("table", help="compare result files as a markdown table")
    t.add_argument("results", nargs="+", type=Path)
    args = ap.parse_args(argv)
    if args.command == "run":
        return run(args)
    print(table([json.loads(p.read_text(encoding="utf-8")) for p in args.results]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
