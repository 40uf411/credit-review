"""The assistant's HTTP API with an in-memory store and fake dependencies."""

import pytest
from fastapi.testclient import TestClient

from assistant import api
from assistant.chat import Chat
from assistant.config import load_topic
from assistant.guards import MAX_MESSAGES, Guards
from assistant.memory import InMemoryStore
from assistant.retriever import LocalRetriever
from credit_review.llm import LLMClient, LLMConfig
from tests.assistant.fakes import CHUNKS, Model, Shields, cited

KEY = "k"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_API_KEY", KEY)
    llm = LLMClient(LLMConfig(provider="openai", model="mini", cache_dir=tmp_path), provider=Model(answer=cited()))
    store = InMemoryStore()
    chat = Chat(Guards(Shields(), llm, load_topic()), LocalRetriever(CHUNKS), llm, store)
    api.app.dependency_overrides = {api.get_store: lambda: store, api.get_chat: lambda: chat}
    with TestClient(api.app, headers={"X-API-Key": KEY, "X-User-Id": "alice"}) as c:
        yield c
    api.app.dependency_overrides = {}


def test_a_conversation_end_to_end(client):
    created = client.post("/conversations")
    assert created.status_code == 201 and created.json()["remaining_messages"] == MAX_MESSAGES
    cid = created.json()["id"]
    reply = client.post(f"/conversations/{cid}/messages", json={"text": "Maximale hypotheek waarde woning?"}).json()
    assert not reply["refused"] and reply["citations"][0]["chunk_id"] == "nl-thk-0003"
    assert reply["remaining_messages"] == MAX_MESSAGES - 1
    turns = client.get(f"/conversations/{cid}").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_another_users_conversation_is_not_found(client):
    cid = client.post("/conversations").json()["id"]
    assert client.get(f"/conversations/{cid}", headers={"X-User-Id": "mallory"}).status_code == 404
    assert client.post(f"/conversations/{cid}/messages", json={"text": "hi"},
                       headers={"X-User-Id": "mallory"}).status_code == 404


def test_the_key_and_the_user_are_required(client):
    assert client.post("/conversations", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/conversations", headers={"X-User-Id": "not valid/"}).status_code == 422


def test_an_oversized_body_is_rejected_before_any_guard(client):
    cid = client.post("/conversations").json()["id"]
    assert client.post(f"/conversations/{cid}/messages", json={"text": "x" * 5000}).status_code == 422


def test_health_and_readiness(client, monkeypatch):
    for name in ("ASSISTANT_SEARCH_ENDPOINT", "ASSISTANT_SHIELDS_ENDPOINT", "CREDIT_REVIEW_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 503
    for name in ("ASSISTANT_SEARCH_ENDPOINT", "ASSISTANT_SHIELDS_ENDPOINT", "CREDIT_REVIEW_LLM_BASE_URL"):
        monkeypatch.setenv(name, "https://x")
    assert client.get("/readyz").status_code == 200


def test_citations_name_their_document(client):
    cid = client.post("/conversations").json()["id"]
    citation = client.post(f"/conversations/{cid}/messages", json={"text": "Maximale hypotheek waarde woning?"}) \
        .json()["citations"][0]
    assert citation["source"] == "nl-thk" and citation["title"] == "Tijdelijke regeling hypothecair krediet"


@pytest.mark.parametrize(("path", "media"), [("/", "text/html"), ("/web/chat.js", "text/javascript"),
                                              ("/web/chat.css", "text/css")])
def test_the_chat_page_is_served_without_a_key_and_locked_down(client, path, media):
    r = client.get(path, headers={"X-API-Key": ""})
    assert r.status_code == 200 and r.headers["content-type"].startswith(media)
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_page_has_no_inline_script_and_only_listed_files_are_served(client):
    html = client.get("/").text
    assert '<script src="/web/chat.js"' in html and "<script>" not in html and "onclick" not in html
    assert ".innerHTML" not in client.get("/web/chat.js").text
    for path in ("/web/chat.html", "/web/..%2Fapi.py", "/web/../api.py", "/web/missing.js"):
        assert client.get(path).status_code == 404
