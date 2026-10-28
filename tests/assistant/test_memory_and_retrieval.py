"""Conversation memory (bound to its user, expiring) and retrieval (country filter)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from assistant.memory import TTL, InMemoryStore, InvalidUser, TableStore
from assistant.models import Turn
from assistant.retriever import AzureSearchRetriever, LocalRetriever, country_filter
from tests.assistant.fakes import CHUNKS


class FakeTable:
    def __init__(self):
        self.rows = {}

    def create_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        from azure.core.exceptions import ResourceNotFoundError
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError("not found")
        return self.rows[(partition_key, row_key)]

    def update_entity(self, entity, mode):
        assert mode == "merge"
        self.rows[(entity["PartitionKey"], entity["RowKey"])].update(entity)


@pytest.mark.parametrize("make", [InMemoryStore, lambda: TableStore("https://x.table.core.windows.net", client=FakeTable())])
def test_a_conversation_belongs_to_its_user(make):
    store = make()
    conversation = store.create("alice")
    conversation.turns.append(Turn("user", "What is the LTV limit?"))
    store.save(conversation)
    assert store.get(conversation.id, "alice").turns == [Turn("user", "What is the LTV limit?")]
    assert store.get(conversation.id, "mallory") is None          # someone else's id: not found
    assert store.get("0" * 24, "alice") is None


def test_conversations_expire():
    table = FakeTable()
    store = TableStore("https://x.table.core.windows.net", client=table)
    conversation = store.create("alice")
    table.rows[("alice", conversation.id)]["created_at"] = (datetime.now(UTC) - TTL - timedelta(minutes=1)).isoformat()
    assert store.get(conversation.id, "alice") is None


@pytest.mark.parametrize("user", ["", "a/b", "x" * 129, "name with spaces", "a#b"])
def test_user_ids_are_checked(user):
    with pytest.raises(InvalidUser):
        InMemoryStore().create(user)


def test_turns_are_stored_as_json_text():
    table = FakeTable()
    store = TableStore("https://x.table.core.windows.net", client=table)
    conversation = store.create("alice")
    conversation.turns += [Turn("user", "Taux d'effort maximal ?"), Turn("assistant", "35 %")]
    store.save(conversation)
    assert json.loads(table.rows[("alice", conversation.id)]["turns"])[0]["text"] == "Taux d'effort maximal ?"


def test_country_filter_always_includes_the_eu_texts():
    assert country_filter([]) is None
    assert country_filter(["NL"]) == "country eq 'EU' or country eq 'NL'"
    assert country_filter(["nl", "FR", "x' or 1 eq 1"]) == "country eq 'EU' or country eq 'FR' or country eq 'NL'"


def test_local_retriever_ranks_and_filters():
    retriever = LocalRetriever(CHUNKS)
    assert retriever.search("taux d'effort revenus assurance")[0].id == "fr-hcsf-0001"
    assert {c.country for c in retriever.search("creditworthiness hypothecair taux", countries=["NL"])} <= {"NL", "EU"}
    assert retriever.search("zzz qqq") == []


def test_azure_search_sends_a_hybrid_semantic_query():
    seen = {}

    class Client:
        def search(self, **kwargs):
            seen.update(kwargs)
            c = CHUNKS[0]
            return [{"id": c.id, "text": c.text, "source": c.source, "country": c.country, "language": c.language}]

    class Embedder:
        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    chunks = AzureSearchRetriever("https://s.search.windows.net", "regulations", Embedder(), client=Client()) \
        .search("LTV in NL", k=3, countries=["NL"])
    assert chunks[0].id == "nl-thk-0003"
    assert seen["query_type"] == "semantic" and seen["top"] == 3 and seen["search_text"] == "LTV in NL"
    assert seen["filter"] == "country eq 'EU' or country eq 'NL'" and seen["vector_queries"][0].vector == [0.1, 0.2]
