"""Data contracts passed between pipeline stages. All JSON-serialisable."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    doc: str                                   # file name
    doc_type: str
    method: Literal["text", "vision"]
    quote: str
    page: int | None = None                    # 1-based
    lines: list[int] = Field(default_factory=list)
    bbox: list[float] | None = None            # x0, y0, x1, y1 in PDF points
    located: Literal["exact", "fuzzy", "image", "not_found", "none"]
    score: float | None = None


class Observation(BaseModel):
    """One value read from one document. Self-contained on purpose: after extraction,
    observations from every document are pooled per field, so each one carries its own
    field name, position and evidence (including which document it came from)."""
    field: str
    sub: str | None = None                     # sub-field of a list item or record
    item: int | None = None                    # index in a list field; None for scalars and records
    type: str
    raw: str                                   # as printed
    model_value: str | None = None             # as normalised by the model
    value: Any = None                          # canonical value (code-parsed where possible)
    parse: Literal["code", "model", "failed"]
    flags: list[str] = Field(default_factory=list)
    evidence: Evidence

    @property
    def path(self) -> str:
        return self.field + (f"[{self.item}]" if self.item is not None else "") + (f".{self.sub}" if self.sub else "")


class LLMCallInfo(BaseModel):
    provider: str
    model: str
    mode: str
    cache_key: str
    cached: bool
    prompt_version: str
    usage: dict[str, Any] = Field(default_factory=dict)
    seconds: float = 0.0
    cost_eur: float | None = None              # cost of the call when it was made; None = no price known


class DocumentExtraction(BaseModel):
    doc: str
    sha256: str                                # identity of the document; file names are not reliable
    doc_type: str
    method: Literal["text", "vision"]
    llm: LLMCallInfo | None = None
    requested: list[str] = Field(default_factory=list)       # fields the policy asked this document for
    observations: list[Observation] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)       # requested, but not in this document
    flags: list[str] = Field(default_factory=list)
    error: str | None = None


class ClassificationProposal(BaseModel):
    doc: str
    sha256: str
    has_text_layer: bool
    proposed_type: str | None
    method: Literal["keywords", "llm", "none"]
    confidence: Literal["high", "medium", "low", "none"]
    matched: list[str] = Field(default_factory=list)
    rationale: str = ""
    alternatives: list[str] = Field(default_factory=list)
    llm: LLMCallInfo | None = None
    error: str | None = None
