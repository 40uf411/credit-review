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
