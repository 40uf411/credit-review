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
    assert result["cost"] is None                       # the replay has no price: "n/a", not free
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
            "cost": None, "latency_p50": 7.0, "latency_p95": 16.0, "errors": {}, "prompt_version": "extract-v2"}


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


def test_results_written_before_the_cost_rename_still_compare(tmp_path, capsys):
    old = {k: v for k, v in _row("gpt-5.4-mini", "low", 183).items() if k != "cost"} | {"cost_eur": 0.0123}
    (tmp_path / "old.json").write_text(json.dumps(old))
    (tmp_path / "new.json").write_text(json.dumps(_row("gpt-5.4-mini", "medium", 185) | {"cost": 0.02, "currency": "USD"}))
    assert main(["table", str(tmp_path / "*.json")]) == 0
    out = capsys.readouterr().out
    assert "0.0123 EUR" in out and "0.0200 USD" in out


def test_reasoning_effort_is_refused_where_it_would_not_be_sent(tmp_path, monkeypatch, capsys):
    """Anthropic ignores reasoning_effort, so a run labelled "@ low" would claim something that never happened."""
    monkeypatch.setenv("CREDIT_REVIEW_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("CREDIT_REVIEW_LLM_REASONING_EFFORT", raising=False)
    out = tmp_path / "result.json"
    assert main(["run", "--reasoning-effort", "low", "--out", str(out)]) == 2
    assert "applies to OpenAI / Azure OpenAI" in capsys.readouterr().out
    assert not out.exists()
