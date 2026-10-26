"""The model evaluation report: scoring and the comparison table (with the reference values replayed)."""

from credit_review.extract import extract_dossier
from credit_review.model_eval import summarise, table


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
