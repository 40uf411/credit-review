"""Fakes for the assistant's dependencies: a model that answers by purpose, and Prompt Shields."""

from collections.abc import Callable, Sequence
from typing import Any

from assistant.models import Chunk
from assistant.shields import ShieldResult
from credit_review.llm import LLMRequest

CHUNKS = [
    Chunk("nl-thk-0003", "Artikel 3. Het hypothecair krediet bedraagt ten hoogste 100 procent van de waarde van de woning.",
          "nl-thk", "NL", "nl"),
    Chunk("fr-hcsf-0001", "Le taux d'effort des emprunteurs ne doit pas excéder 35 % des revenus nets, assurance comprise.",
          "fr-hcsf-credit", "FR", "fr"),
    Chunk("eu-mcd-0018", "Member States shall ensure that the creditor makes a thorough assessment of the consumer's "
                         "creditworthiness before concluding a credit agreement.", "eu-mcd", "EU", "en"),
]


class Model:
    """Answers each request purpose with a handler; records every request it saw."""
    name = "fake"

    def __init__(self, **handlers: Callable[[Any], dict] | dict | Exception):
        self.handlers = {"topic": {"in_scope": True, "reason": "lending"},
                         "rewrite": lambda req: {"question": req.prompt.split("<latest>\n")[1].split("\n</latest>")[0],
                                                 "countries": []},
                         **handlers}
        self.requests: list[Any] = []

    def complete(self, req: LLMRequest, model: str, max_tokens: int) -> tuple[dict, dict, str, str]:
        self.requests.append(req)
        handler = self.handlers[req.purpose]
        data = handler(req) if callable(handler) else handler
        if isinstance(data, Exception):
            raise data
        assert isinstance(data, dict)
        return data, {"input_tokens": 10, "output_tokens": 5}, "json_schema", ""

    def purposes(self) -> list[str]:
        return [r.purpose for r in self.requests]


def cited(chunk_id: str = "nl-thk-0003", quote: str = "ten hoogste 100 procent van de waarde van de woning",
          text: str = "In the Netherlands the mortgage may not exceed 100% of the home's value.") -> dict:
    return {"answer": text, "citations": [{"chunk_id": chunk_id, "quote": quote}]}


class Shields:
    def __init__(self, attack: bool = False, flagged: Sequence[str] = ()):
        self.attack, self.flagged, self.calls = attack, set(flagged), 0

    def analyze(self, user_prompt: str, documents: Sequence[str] = ()) -> ShieldResult:
        self.calls += 1
        return ShieldResult(user_attack=self.attack,
                            document_attacks=tuple(any(f in d for f in self.flagged) for d in documents))
