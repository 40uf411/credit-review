"""The assistant's topic: what it answers and what it refuses, loaded from topic.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

TOPIC_FILE = Path(__file__).with_name("topic.yaml")


@dataclass(frozen=True)
class Topic:
    name: str
    description: str
    in_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    countries: tuple[str, ...]


def load_topic(path: str | Path = TOPIC_FILE) -> Topic:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Topic(name=data["name"], description=" ".join(data["description"].split()),
                 in_scope=tuple(data["in_scope"]), out_of_scope=tuple(data["out_of_scope"]),
                 countries=tuple(data["countries"]))


@dataclass(frozen=True)
class Settings:
    """Where the assistant's Azure services are (environment variables, set by Terraform on the container app)."""
    search_endpoint: str
    search_index: str
    shields_endpoint: str
    table_endpoint: str
    embedding_deployment: str
    openai_endpoint: str

    @classmethod
    def from_env(cls) -> Settings:
        import os
        env = os.environ
        return cls(search_endpoint=env.get("ASSISTANT_SEARCH_ENDPOINT", ""),
                   search_index=env.get("ASSISTANT_SEARCH_INDEX", "regulations"),
                   shields_endpoint=env.get("ASSISTANT_SHIELDS_ENDPOINT", ""),
                   table_endpoint=env.get("ASSISTANT_TABLE_ENDPOINT", ""),
                   embedding_deployment=env.get("ASSISTANT_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"),
                   openai_endpoint=env.get("CREDIT_REVIEW_LLM_BASE_URL", ""))

    def missing(self) -> list[str]:
        names = {"ASSISTANT_SEARCH_ENDPOINT": self.search_endpoint, "ASSISTANT_SHIELDS_ENDPOINT": self.shields_endpoint,
                 "CREDIT_REVIEW_LLM_BASE_URL": self.openai_endpoint}
        return [k for k, v in names.items() if not v]
