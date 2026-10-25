"""Document type detection: deterministic keywords first, the model as fallback.

Keywords come from the policy (`document_types.*.detect`), so the bank's document
catalogue drives detection. Scans have no text, so they go to a vision model. The
result is only a proposal: the analyst confirms every type before a run.
"""

from __future__ import annotations

from typing import Any

from credit_review.llm import LLMClient, LLMError, LLMRequest
from credit_review.models import ClassificationProposal, LLMCallInfo
from credit_review.pdf import PdfDocument, normalise_for_search
from credit_review.policy import Policy

PROMPT_VERSION = "classify-v1"

SYSTEM = (
    "You classify documents from Belgian mortgage credit files for a credit analyst. "
    "Documents are mostly in Dutch, sometimes French or German. Choose exactly one type "
    "from the closed list. Base the choice only on what the document shows."
)


def keyword_scores(doc: PdfDocument, policy: Policy) -> dict[str, list[str]]:
    text = normalise_for_search(" ".join(line.text for line in doc.lines))
    scores: dict[str, list[str]] = {}
    for type_id, spec in policy.document_types.items():
        hits = [kw for kw in spec.detect if normalise_for_search(kw) in text]
        if hits:
            scores[type_id] = hits
    return scores


def _schema(policy: Policy) -> dict:
    return {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string", "enum": list(policy.document_types)},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "rationale": {"type": "string"},
        },
        "required": ["doc_type", "confidence", "rationale"],
        "additionalProperties": False,
    }


def _prompt(policy: Policy, doc: PdfDocument) -> str:
    lines = ["Document types:"]
    for type_id, spec in policy.document_types.items():
        note = f" — {spec.note}" if spec.note else ""
        lines.append(f"- {type_id}: {spec.label}{note}")
    if doc.has_text_layer:
        lines += ["", "First page of the document:", doc.first_page_text()]
    else:
        lines += ["", "The document is attached as page images."]
    lines += ["", "Return the type, your confidence, and a one-sentence rationale in English."]
    return "\n".join(lines)


def classify_document(doc: PdfDocument, policy: Policy, llm: LLMClient | None) -> ClassificationProposal:
    base: dict[str, Any] = dict(doc=doc.name, sha256=doc.sha256, has_text_layer=doc.has_text_layer)
    scores = keyword_scores(doc, policy) if doc.has_text_layer else {}
    ranked = sorted(scores.items(), key=lambda kv: len(kv[1]), reverse=True)
    if ranked and (len(ranked) == 1 or len(ranked[0][1]) > len(ranked[1][1])):
        best, hits = ranked[0]
        return ClassificationProposal(
            **base, proposed_type=best, method="keywords",
            confidence="high" if len(hits) >= 2 or len(ranked) == 1 else "medium",
            matched=hits, alternatives=[t for t, _ in ranked[1:]],
            rationale=f"Matched {', '.join(repr(h) for h in hits)}.")

    if llm is None:
        return ClassificationProposal(
            **base, proposed_type=None, method="none", confidence="none",
            alternatives=[t for t, _ in ranked],
            rationale="No text layer or no decisive keywords, and no model configured. Select the type manually.")

    req = LLMRequest(
        purpose="classify", system=SYSTEM, prompt=_prompt(policy, doc), schema=_schema(policy),
        schema_name="document_type",
        images=[] if doc.has_text_layer else doc.images_for_model()[:1],
        meta={"doc": doc.name, "sha256": doc.sha256, "prompt_version": PROMPT_VERSION},
    )
    try:
        res = llm.complete(req)
    except LLMError as exc:
        return ClassificationProposal(**base, proposed_type=None, method="none", confidence="none",
                                      error=str(exc), rationale="Model call failed. Select the type manually.")
    doc_type = res.data.get("doc_type")
    if doc_type not in policy.document_types:
        doc_type = None
    return ClassificationProposal(
        **base, proposed_type=doc_type, method="llm",
        confidence=res.data.get("confidence", "low") if doc_type else "none",
        rationale=str(res.data.get("rationale", "")), alternatives=[t for t, _ in ranked],
        llm=LLMCallInfo(provider=res.provider, model=res.model, mode=res.mode, cache_key=res.cache_key,
                        cached=res.cached, prompt_version=PROMPT_VERSION, usage=res.usage, seconds=res.seconds),
    )


def classify_documents(docs: list[PdfDocument], policy: Policy,
                       llm: LLMClient | None) -> list[ClassificationProposal]:
    return [classify_document(d, policy, llm) for d in docs]
