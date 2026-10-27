"""The model evaluation report: scoring and the comparison table (with the reference values replayed)."""

import json
from pathlib import Path

import pytest

from credit_review.extract import extract_dossier
from credit_review.model_eval import default_out, main, summarise, table


def test_replayed_reference_scores_full_marks_and_renders(docs, policy, golden, golden_llm):
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    result = summarise(extract_dossier(docs, types, policy, golden_llm), golden, golden_llm.config, 1.0)
    assert result["accuracy"] == 1.0 and result["wrong"] == result["missed"] == 0
    assert result["accuracy_scans"] == 1.0 and result["accuracy_text"] == 1.0
    assert result["calls"] == len(types) and result["errors"] == {}
    assert result["cost_eur"] is None                       # the replay has no price: "n/a", not free
    text = table([result])
    assert "**100.0%**" in text and "n/a" in text and "noise" in text


def test_runs_at_different_reasoning_efforts_are_labelled_apart(docs, policy, golden, golden_llm):
    types = {name: d["doc_type"] for name, d in golden["documents"].items()}
    result = summarise(extract_dossier(docs, types, policy, golden_llm), golden, golden_llm.config, 1.0)
    low, medium = dict(result, reasoning_effort="low"), dict(result, reasoning_effort="medium")
    text = table([low, medium])
    assert "`golden-replay @ low`" in text and "`golden-replay @ medium`" in text


def _row(model, effort, matched):
    return {"model": model, "reasoning_effort": effort, "accuracy": matched / 186, "matched": matched, "expected": 186,
            "accuracy_text": 1.0, "accuracy_scans": 1.0, "wrong": 186 - matched, "missed": 0, "repair_rate": 0.0,
            "cost_eur": None, "latency_p50": 7.0, "latency_p95": 16.0, "errors": {}, "prompt_version": "extract-v2"}


def test_each_model_and_effort_gets_its_own_result_file():
    assert default_out("gpt-5.4-mini", "low") == Path("runs/eval/gpt-5.4-mini-low.json")
    assert default_out("gpt-5.4-mini", None) == Path("runs/eval/gpt-5.4-mini.json")
    assert default_out("org/model:v1", "low") == Path("runs/eval/org_model_v1-low.json")


def test_table_expands_glob_patterns_itself(tmp_path, capsys):
    """PowerShell passes runs/eval/*.json through unexpanded; the tool must expand it."""
    for effort, matched in (("low", 183), ("medium", 185)):
        (tmp_path / f"gpt-5.4-mini-{effort}.json").write_text(json.dumps(_row("gpt-5.4-mini", effort, matched)))
    assert main(["table", str(tmp_path / "*.json")]) == 0
    out = capsys.readouterr().out
    assert "`gpt-5.4-mini @ low`" in out and "`gpt-5.4-mini @ medium`" in out
    assert out.index("@ medium") < out.index("@ low")          # sorted by accuracy
    with pytest.raises(SystemExit, match="no result files"):
        main(["table", str(tmp_path / "nothing-*.json")])
