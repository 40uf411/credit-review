"""The assistant evaluation, run over HTTP against the API with fake dependencies."""

import pytest
from fastapi.testclient import TestClient

from assistant import api
from assistant.chat import Chat
from assistant.config import load_topic
from assistant.evaluate import evaluate, load_cases, markdown, score, source_of
from assistant.guards import Guards
from assistant.memory import InMemoryStore
from assistant.retriever import LocalRetriever
from credit_review.llm import LLMClient, LLMConfig
from tests.assistant.fakes import CHUNKS, Model, Shields, cited

KEY = "k"


class AllChunks(LocalRetriever):
    """Returns every chunk: the evaluation is under test here, not ranking."""

    def search(self, query, k=5, countries=()):
        return list(self.chunks)


def topic(req):
    text = req.prompt.lower()
    return {"in_scope": not any(w in text for w in ("bread", "python", "shares")), "reason": ""}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_API_KEY", KEY)
    llm = LLMClient(LLMConfig(provider="openai", model="mini", cache_dir=tmp_path),
                    provider=Model(topic=topic, answer=cited()))
    store = InMemoryStore()
    chat = Chat(Guards(Shields(), llm, load_topic()), AllChunks(CHUNKS), llm, store)
    api.app.dependency_overrides = {api.get_store: lambda: store, api.get_chat: lambda: chat}
    with TestClient(api.app) as c:
        yield c
    api.app.dependency_overrides = {}


def test_the_case_file_loads_and_covers_every_category():
    cases = load_cases()
    assert len(cases) >= 25
    assert {c["category"] for c in cases} == {"single", "cross", "memory", "off_topic", "attack", "limits"}
    assert len(next(c for c in cases if c["id"] == "too-long")["turns"][0]) == 1001
    assert len(next(c for c in cases if c["id"] == "sixth-question")["turns"]) == 6


def test_scoring():
    nl = {"id": "x", "expect": "answer", "sources": ["nl-thk"], "contains": ["100"]}
    good = {"refused": False, "code": "ok", "answer": "100%", "citations": [{"chunk_id": "nl-thk-0003", "quote": ""}]}
    assert score(nl, good)
    assert not score(nl, {**good, "answer": "it depends"})
    assert not score(nl, {**good, "citations": [{"chunk_id": "fr-hcsf-credit-0001", "quote": ""}]})
    assert not score(nl, {**good, "refused": True})
    refuse = {"id": "y", "expect": "refuse", "codes": ["off_topic"]}
    assert score(refuse, {"refused": True, "code": "off_topic"})
    assert not score(refuse, {"refused": True, "code": "prompt_attack"})
    assert source_of("fr-hcsf-credit-0012") == "fr-hcsf-credit"


def test_a_run_against_the_api(client):
    cases = [c for c in load_cases() if c["id"] in {"nl-ltv", "off-bread", "too-long", "sixth-question", "fr-dsti"}]
    summary, results = evaluate(client, KEY, cases)
    by_id = {r.id: r for r in results}
    assert by_id["nl-ltv"].passed and by_id["off-bread"].passed and by_id["too-long"].passed
    assert by_id["sixth-question"].passed and by_id["sixth-question"].code == "limit_reached"
    assert not by_id["fr-dsti"].passed                  # the fake model always cites the Dutch rule
    assert summary["must_refuse_failures"] == [] and summary["errors"] == []
    assert summary["answer_accuracy"] == 0.5 and summary["refusal_accuracy"] == 1.0
    assert "| `nl-ltv` |" in markdown(summary, results)


def test_a_wrong_key_is_an_error_not_a_pass(client):
    summary, results = evaluate(client, "wrong", [c for c in load_cases() if c["id"] == "off-bread"])
    assert not results[0].passed and results[0].error and summary["errors"] == ["off-bread"]
