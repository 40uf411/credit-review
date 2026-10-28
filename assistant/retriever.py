"""Finding the passages an answer may use: Azure AI Search (keyword + vector + semantic re-ranking), or a small
in-memory retriever for tests and offline runs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol

from assistant.models import Chunk
from credit_review.llm import AZURE_SCOPE, azure_v1_url

FIELDS = ["id", "text", "source", "country", "language"]


class Retriever(Protocol):
    def search(self, query: str, k: int = 5, countries: Sequence[str] = ()) -> list[Chunk]: ...


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class AzureEmbedder:
    """Embeddings from the Foundry deployment, signed in with Entra ID."""

    def __init__(self, endpoint: str, deployment: str, client: Any = None, batch: int = 16):
        if client is None:
            import openai
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            client = openai.OpenAI(base_url=azure_v1_url(endpoint), max_retries=3,
                                   api_key=get_bearer_token_provider(DefaultAzureCredential(), AZURE_SCOPE))
        self.client, self.deployment, self.batch = client, deployment, batch

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            response = self.client.embeddings.create(model=self.deployment, input=list(texts[i:i + self.batch]))
            vectors += [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
        return vectors


def country_filter(countries: Sequence[str]) -> str | None:
    """EU texts apply everywhere, so a question about one country also searches the EU sources."""
    wanted = sorted({c.upper() for c in countries if re.fullmatch(r"[A-Za-z]{2}", c)} | ({"EU"} if countries else set()))
    return " or ".join(f"country eq '{c}'" for c in wanted) or None


class AzureSearchRetriever:
    def __init__(self, endpoint: str, index: str, embedder: Embedder, client: Any = None):
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.search.documents import SearchClient
            client = SearchClient(endpoint, index, credential=DefaultAzureCredential())
        self.client, self.embedder = client, embedder

    def search(self, query: str, k: int = 5, countries: Sequence[str] = ()) -> list[Chunk]:
        from azure.search.documents.models import VectorizedQuery
        vector = self.embedder.embed([query])[0]
        results = self.client.search(
            search_text=query, top=k, select=FIELDS, filter=country_filter(countries),
            vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=50, fields="embedding")],
            query_type="semantic", semantic_configuration_name="default")
        return [Chunk(id=r["id"], text=r["text"], source=r["source"], country=r["country"], language=r["language"])
                for r in results]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.casefold()) if len(w) > 2}


class LocalRetriever:
    """Word-overlap ranking over chunks held in memory. For tests and for running without Azure AI Search."""

    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)

    def search(self, query: str, k: int = 5, countries: Sequence[str] = ()) -> list[Chunk]:
        allowed = {c.upper() for c in countries} | {"EU"} if countries else None
        words = _words(query)
        scored = [(len(words & _words(c.text)), c) for c in self.chunks if allowed is None or c.country in allowed]
        return [c for score, c in sorted(scored, key=lambda s: -s[0]) if score > 0][:k]
