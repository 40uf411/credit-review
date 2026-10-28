"""Score one model on the hand-labelled reference set: accuracy, cost and latency for the sample dossier.

    # one model (here an Azure OpenAI deployment; signs in with `az login` or a managed identity)
    CREDIT_REVIEW_LLM_PROVIDER=azure_openai CREDIT_REVIEW_LLM_BASE_URL=https://<resource>.openai.azure.com \\
    CREDIT_REVIEW_LLM_MODEL=<deployment> python -m credit_review.model_eval run --fresh

    # the same deployment at another reasoning effort (reasoning models only)
    ... python -m credit_review.model_eval run --fresh --reasoning-effort low

Each run is written to runs/eval/<model>[-<effort>].json unless --out says otherwise.

    # compare the runs (the pattern is expanded here, so it works in PowerShell too)
    python -m credit_review.model_eval table "runs/eval/*.json"

Document types come from the reference labels, so only extraction is measured. Scanned documents
(read from page images) and text documents are scored separately. --fresh calls the model for every
document instead of reusing cached replies, which is what latency and cost should be measured on.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
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
from credit_review.prices import currency

POLICY = Path(__file__).resolve().parents[1] / "policy" / "example_bank_v3.2.yaml"
RESULTS = Path("runs") / "eval"


def default_out(model: str, reasoning_effort: str | None) -> Path:
    """One file per model and effort, so a second run never overwrites a different one."""
    name = model + (f"-{reasoning_effort}" if reasoning_effort else "")
    return RESULTS / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".json")


def expand(patterns: list[str]) -> list[Path]:
    """Expand glob patterns ourselves: PowerShell and cmd pass `*.json` through unexpanded."""
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        if not matches:
            raise SystemExit(f"no result files match {pattern!r}")
        paths += [Path(m) for m in matches]
    return paths


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


def applied_effort(config: LLMConfig) -> str | None:
    """The reasoning effort actually sent: only OpenAI-compatible providers send one."""
    return config.reasoning_effort if config.provider == "openai" else None


def _cost(r: dict[str, Any]) -> tuple[float | None, str]:
    """Cost and currency of a result; files written before the rename carry `cost_eur`."""
    if "cost" in r:
        return r["cost"], r.get("currency", "")
    return r.get("cost_eur"), "EUR"


def summarise(extractions: list[DocumentExtraction], golden: dict[str, Any], config: LLMConfig,
              wall_seconds: float) -> dict[str, Any]:
    rows = evaluate(extractions, golden)
    overall = score(rows)
    method = {e.doc: e.method for e in extractions}
    by_method = {m: score([r for r in rows if method.get(r["doc"]) == m]) for m in ("text", "vision")}
    calls = [e.llm for e in extractions if e.llm]
    fresh = [c for c in calls if not c.cached]
    costs = [c.cost for c in calls]
    modes = Counter(c.mode for c in calls)
    return {
        "model": config.model, "reasoning_effort": applied_effort(config),
        "provider": config.label or config.provider, "prompt_version": PROMPT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accuracy": overall["accuracy"], "matched": overall["match"], "expected": overall["expected"],
        "wrong": overall["mismatch"], "missed": overall["missed"], "extra": overall["extra"],
        "accuracy_text": by_method["text"]["accuracy"], "accuracy_scans": by_method["vision"]["accuracy"],
        "calls": len(calls), "cached_calls": len(calls) - len(fresh), "modes": dict(modes),
        "repair_rate": round(modes.get("prompt+repair", 0) / len(calls), 3) if calls else None,
        # cost of reading the dossier once, whether or not this run paid it (cached replies keep their cost)
        "cost": round(sum(c for c in costs if c is not None), 4) if costs and None not in costs else None,
        "currency": currency(),
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
        if config.reasoning_effort and applied_effort(config) is None:
            print(f"Reasoning effort applies to OpenAI / Azure OpenAI models; this run uses {config.label or config.provider}. "
                  "Drop --reasoning-effort (and CREDIT_REVIEW_LLM_REASONING_EFFORT), or set CREDIT_REVIEW_LLM_PROVIDER.")
            return 2
        llm = LLMClient(config)
        started = time.monotonic()
        extractions = extract_dossier(docs, types, report.policy, llm, workers=args.workers)
        result = summarise(extractions, golden, config, time.monotonic() - started)
    out = args.out or default_out(config.model, config.reasoning_effort)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(table([result]))
    print(f"\nWritten to {out}")
    return 0 if not result["errors"] else 1


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def table(results: list[dict[str, Any]]) -> str:
    lines = ["| Model | Accuracy | Text documents | Scans | Wrong / missed | Repair rounds | Cost per dossier | "
             "Latency p50 / p95 | Errors |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (-(r["accuracy"] or 0), _cost(r)[0] or 0)):
        amount, unit = _cost(r)
        cost = "n/a" if amount is None else f"{amount:.4f} {unit}".strip()
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
    r.add_argument("--out", type=Path, help="where to write the result (default: runs/eval/<model>[-<effort>].json)")
    r.add_argument("--policy", type=Path, default=POLICY)
    r.add_argument("--dossier", type=Path, default=DOSSIER)
    r.add_argument("--golden", type=Path, default=GOLDEN)
    r.add_argument("--workers", type=int, default=4)
    t = sub.add_parser("table", help="compare result files as a markdown table")
    t.add_argument("results", nargs="+", help="result files or glob patterns, e.g. \"runs/eval/*.json\"")
    args = ap.parse_args(argv)
    if args.command == "run":
        return run(args)
    print(table([json.loads(p.read_text(encoding="utf-8")) for p in expand(args.results)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
