"""Storage backends, and the model cache going through them."""

import pytest

from credit_review.llm import LLMClient, LLMConfig, LLMRequest
from credit_review.storage import BlobStore, LocalStore, store_from_env


def test_local_store_round_trip(tmp_path):
    store = LocalStore(tmp_path)
    assert store.get("a/b.json") is None
    store.put("a/b.json", b"1")
    store.put("a/c/d.json", b"2")
    assert store.get("a/b.json") == b"1"
    assert store.list("a") == ["a/b.json", "a/c/d.json"] and store.list("nothing") == []


@pytest.mark.parametrize("key", ["", "../x", "a/../../x", "/abs", "a//b", "a\\b"])
def test_keys_cannot_escape_the_store(tmp_path, key):
    with pytest.raises(ValueError):
        LocalStore(tmp_path).put(key, b"x")


class FakeContainer:
    def __init__(self):
        self.blobs = {}

    def download_blob(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        if name not in self.blobs:
            raise ResourceNotFoundError("not found")
        data = self.blobs[name]
        return type("Downloader", (), {"readall": lambda self: data})()

    def upload_blob(self, name, data, overwrite):
        assert overwrite
        self.blobs[name] = data

    def list_blobs(self, name_starts_with):
        return [type("Blob", (), {"name": n})() for n in self.blobs if n.startswith(name_starts_with)]


def test_blob_store_round_trip():
    store = BlobStore("https://acct.blob.core.windows.net/", "dossiers", container_client=FakeContainer())
    assert store.get("x/status.json") is None
    store.put("x/status.json", b"{}")
    assert store.get("x/status.json") == b"{}" and store.list("x/") == ["x/status.json"]


def test_blob_is_used_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDIT_REVIEW_STORAGE_URL", raising=False)
    assert isinstance(store_from_env("dossiers", tmp_path), LocalStore)


class Echo:
    name = "echo"
    calls = 0

    def complete(self, req, model, max_tokens):
        Echo.calls += 1
        return {"ok": True}, {"input_tokens": 1, "output_tokens": 1}, "json_schema", "{}"


def test_model_replies_are_cached_in_the_given_store(tmp_path):
    store = BlobStore("https://acct.blob.core.windows.net", "llm-cache", container_client=FakeContainer())
    client = LLMClient(LLMConfig(model="m", cache_dir=tmp_path), provider=Echo(), store=store)
    req = LLMRequest(purpose="extract", system="s", prompt="p", schema={"type": "object"}, schema_name="x")
    first, second = client.complete(req), client.complete(req)
    assert Echo.calls == 1 and not first.cached and second.cached
    assert [n.split("/")[0] for n in store.client.blobs] == ["llm"]
    assert not any(tmp_path.iterdir())            # nothing written to the local folder
