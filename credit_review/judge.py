"""Judgment checks: the only place a model looks at a policy rule.

The model sees only the facts the policy lists for the check, each with an id. It
may raise concerns, and every concern must cite the ids it rests on; concerns
without valid citations are discarded and the analyst is told. It can answer
'concern' or 'no concern found' — never 'pass'. No concern found means
'analyst to confirm', because the absence of a concern the model noticed is not
evidence that the criterion is met.
"""

from __future__ import annotations

from typing import Any

from credit_review.facts import FactBook, display
from credit_review.llm import LLMClient, LLMError, LLMRequest
from credit_review.policy import Check

PROMPT_VERSION = "judge-v2"

SYSTEM = """You support a credit analyst at a Belgian bank who applies the bank's credit policy to a mortgage file.
You are given one judgment criterion from the policy and a numbered list of facts taken from the file's documents.

Rules:
1. Use only the facts given. Do not assume anything that is not in them.
2. Raise a concern for anything that, under this criterion, an experienced analyst would want explained or would weigh against the file.
3. Every concern must cite the fact ids it rests on (for example ["F3", "F7"]).
4. You do not decide the file and you do not approve anything. If you see nothing of concern, say "no_concern_found"; the analyst will still confirm.
5. Do not calculate. Quote the figures and computed values given in the facts exactly as written. If a point needs a number that is not given, say which number is missing instead of working it out.
6. Write in plain English for the analyst, briefly."""

SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["concern", "no_concern_found"]},
        "concerns": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "facts"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["outcome", "concerns", "summary"],
    "additionalProperties": False,
}


def render_facts(check: Check, book: FactBook) -> list[dict[str, str]]:
    """Each input as one or more numbered facts, with the documents they come from."""
    out: list[dict[str, str]] = []

    def add(name: str, text: str, docs: list[str]) -> None:
        out.append({"id": f"F{len(out) + 1}", "name": name, "text": text,
                    "docs": ", ".join(sorted(set(docs)))})

    for name in check.inputs or []:
        label = (book.policy.fields.get(name) or book.policy.derived.get(name))
        label = label.description.rstrip(".") if label else name
        if name in book.derived:
            d = book.derived[name]
            for c in d.candidates:
                add(name, f"{label}: {c.display} ({c.working})", ["computed"])
            continue
        fact = book.facts.get(name)
        if fact is None or not (fact.candidates or fact.records):
            add(name, f"{label}: not found in the file", [])
            continue
        if fact.cardinality == "one":
            if fact.conflict:
                for v in fact.candidates:
                    add(name, f"{label}: {v.display} — according to {', '.join(v.docs)} (documents disagree)", v.docs)
            else:
                add(name, f"{label}: {fact.candidates[0].display if fact.status != 'compatible' else display(fact.resolved[0], fact.type)}",
                    [d for v in fact.candidates for d in v.docs])
            continue
        for r in fact.records:
            parts = "; ".join(f"{sub.replace('_', ' ')} {v.display}" for sub, v in r.values.items())
            add(name, f"{label} — {parts}", [r.doc])
    for p in check.params:
        prm = book.policy.parameters[p]
        add(p, f"Policy parameter {p}: {prm.value} {prm.unit} — {prm.description}", ["policy"])
    return out


def make_judge(llm: LLMClient):
    def judge(check: Check, book: FactBook) -> dict[str, Any]:
        facts = render_facts(check, book)
        prompt = "\n".join([
            f"Criterion ({check.id}): {check.question}", "",
            "Facts:", *[f"{f['id']}. {f['text']}" + (f" [source: {f['docs']}]" if f['docs'] else "") for f in facts],
        ])
        req = LLMRequest(purpose="judge", system=SYSTEM, prompt=prompt, schema=SCHEMA, schema_name="judgment",
                         meta={"check": check.id, "prompt_version": PROMPT_VERSION})
        mapping = book.policy.judgment.outcome_mapping
        try:
            res = llm.complete(req)
        except LLMError as exc:
            return {"outcome": "analyst_review", "reason": f"Judgment check could not run: {exc}", "facts": facts}
        ids = {f["id"] for f in facts}
        kept, rejected = [], []
        for c in res.data.get("concerns", []):
            cited = [x for x in c.get("facts", []) if x in ids]
            (kept if cited else rejected).append({"text": c.get("text", ""), "facts": cited})
        model_outcome = res.data.get("outcome")
        if model_outcome == "concern" and not kept:
            model_outcome = "no_concern_found"           # a concern nobody can verify is not a finding
        outcome = mapping.get(model_outcome or "", "analyst_review")
        by_id = {f["id"]: f for f in facts}
        details = [f"Concern: {c['text']} [cites {', '.join(c['facts'])}: "
                   + "; ".join(by_id[x]['text'][:80] for x in c['facts']) + "]" for c in kept]
        details += [f"Discarded (no valid citation): {c['text']}" for c in rejected]
        reason = (res.data.get("summary") or "").strip() or ("Concerns raised." if kept else "No concern found.")
        if model_outcome == "no_concern_found":
            reason = f"No concern found by the model — analyst to confirm. {reason}".strip()
        return {"outcome": outcome, "reason": reason, "details": details, "concerns": kept,
                "rejected": rejected, "facts": facts, "model": res.model, "cached": res.cached,
                "prompt_version": PROMPT_VERSION}
    return judge
