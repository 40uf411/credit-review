# How AI was used

Kept as I go. What I delegated, what I decided, and what I caught.

| Step | Delegated to AI | Decided / verified by me |
|---|---|---|
| Reading the pack | Text extraction, reading the scanned Dutch payslips, first list of findings | Re-checked each finding against the documents |
| Architecture | Drafting options | AI reads / rules decide / analyst concludes; policy as configuration; what to cut |
| Policy → YAML | Drafted every article with its verbatim source sentence | The 12 interpretation calls in `policy/REVIEW.md` |
| Validator | Code and tests | That every rule must trace to policy text verbatim, and a model may never "pass" a judgment rule |
| Extraction | Code, prompts, schema, tests | Code re-parses every value and locates every quote; no number is taken from the model unchecked |
| Reference labels | Hand-labelling the dossier (186 values) | Spot-check before using it to score models |

First real run (Claude Sonnet 5.5): 183/186 reference values (98.4%). No amount, date or identifier
misread. The 3 differences were my design errors, not the model's: a yes/no "is it signed?" field
(absence is the finding — now a presence check), one field spanning two printed values (cadastral
data — now split), and a reference label that contradicted its own field hint.

Caught along the way:
- Large schemas were refused by strict structured output; my fallback (a forced tool call) is not
  supported on current models. My fake client allowed it, so tests passed. Fixed: per-schema fallback
  to prompted JSON with a structural check and one repair round; the fake now behaves like the API.
- The guardrail flagged 4 June-payslip values whose quote held only the label, not the value. Values
  were right; evidence was weak. Tightened the quote rule (prompt v2).
- Windows' built-in unzip mangled the en-dash in two file names. Documents are now identified by
  sha256, never by name.
- I asked why each observation repeats its field name: the nested output was redundant. Flattened to
  self-contained observations plus a not-found list, ready to be pooled per field across documents.
- The validator's first run found a YAML bug in the AI draft: unquoted commas silently split
  descriptions into bogus keys. A schema with `extra="forbid"` turned a silent error into a loud one.
- Scanned payslip pages rendered at 150 dpi were ~4300×6100 px, too large for the API. Now
  capped at 1800 px JPEG. Tests with a replayed model would never have shown this.
- The employer enterprise number on all payslips fails its check digit (found by hand while drafting).
- The engine passed every test on its first run, which made me check the tests. One assertion was
  written so it could not fail. Fixed, it exposed a real bug: with the BECRIS extract removed, 4.2
  (arrears) and 4.3 (all credits declared) still passed — "0 of 0 credits" read as "all declared".
  Absence of evidence was treated as evidence of absence. Now "no document of that type" and "the
  document lists nothing" are different values everywhere; nothing that depends on a missing document
  can pass, and the reason names the missing document. Pinned by a test.
- First real judgment run: every concern cited valid facts; the 7.1 judge connected the pay jump,
  the advance, the household conflicts and the three new mortgages on its own. Two arithmetic slips
  ("23% lower" — it is 18.9%; "€914" — it is €911.95): harmless because no deciding number comes
  from the model, and the judge prompt now forbids calculating (judge-v2). One reasoning error was
  mine: I had translated "gezamenlijk belastbaar inkomen" as "joint taxable income", and the model
  reasoned from that label that the household's income was split. Renamed and described correctly.

