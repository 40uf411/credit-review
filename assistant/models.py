"""Types shared by the assistant's parts: retrieved chunks, answers with citations, conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    source: str           # document id from the corpus manifest
    country: str          # EU | BE | FR | NL
    language: str         # en | fr | nl


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    quote: str            # verbatim text from the chunk


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass
class Conversation:
    id: str
    user_id: str          # the conversation is bound to the caller who created it
    turns: list[Turn] = field(default_factory=list)

    @property
    def user_messages(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")
