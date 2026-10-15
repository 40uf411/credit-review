# Credit Review for Example Bank — prototype

Reads a mortgage dossier, checks it against Example Bank's credit policy, and shows every
finding with the evidence behind it. **AI reads, rules decide, analysts conclude.**

> **Sample data only.** The PDFs and reference labels in this repository are synthetic and
> contain realistic-looking identity and financial data. Never commit real borrower files,
> customer policy documents, model caches, generated assessments, or API credentials.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then choose a provider
```

**Anthropic API:** set `ANTHROPIC_API_KEY`. Default model `claude-sonnet-5-5`.

**LM Studio (fully local):** load a *vision-capable* model (the three payslips are scans),
set its context length to 16k or more, start the server (Developer → Start server, port
1234), and set `CREDIT_REVIEW_LLM_PROVIDER=lmstudio`. Leave `CREDIT_REVIEW_LLM_MODEL` empty to use the
first loaded model. Structured output (JSON Schema) is used when the server supports it,
otherwise the schema is put in the prompt.

## Run

**The page (what the analyst uses):**

```bash
streamlit run app.py
```

Choose the policy and the dossier folder in the sidebar, press *Detect document types*, check
the types, then *Run assessment*. Each finding opens to the policy text, the working and the
quotes, and any quote can be shown highlighted on its page. *Policy change* reruns the same
file under another policy version without reading the documents again. *Cached results only*
never calls a model: use it for the live demo after one full run.

**A policy change (Part B).** `policy/example_bank_v3.3.yaml` is the worked example: the risk
team adds a rule (5.3.e, renovation credits at most 15 years) and tightens one (5.4, own funds
10% instead of 2%), with the bank's amended text in `example_bank_credit_policy_extract_v3.3.md`.
Open *Policy change*, pick v3.3, and press *Show what this change does to the file*: on the
case dossier exactly two findings change (5.3.e new, not met; 5.4 from documents disagree to
not met, because both candidate totals now fail). No document is read again. The validator
refuses a half-done change: a parameter whose value is not in the article text it cites, or a
rule description still quoting the old number.

**The same steps from the command line:**

```bash
# 1. validate the policy configuration
python -m credit_review.policy policy/example_bank_v3.2.yaml

# 2. classify + extract the dossier (calls the model once; results are cached)
python -m credit_review.extract case_pack/dossier --out runs/latest

# 3. score the extraction against the hand-labelled reference
python -m credit_review.golden runs/latest/extraction.json golden/sample_dossier.yaml

# 4. full assessment: documents, facts, integrity checks, every policy rule
python -m credit_review.pipeline case_pack/dossier --decision-date 2026-10-12 --out runs/latest

# tests (no model or network needed)
python -m pytest -q
```

Every model call is cached under `.credit_review_cache/`, keyed by provider, model, prompt,
schema and document. A second run is instant and identical. With `CREDIT_REVIEW_LLM_OFFLINE=1`
no model is ever called — use that for the live demo.

## Layout

| Path | What |
|---|---|
| `app.py`, `.streamlit/` | The page: choose policy and dossier, confirm document types, run, read the findings |
| `policy/example_bank_v3.2.yaml` | The policy as configuration: documents, fields, parameters, integrity checks, rules |
| `policy/REVIEW.md` | The interpretation calls a person must confirm before the YAML is approved |
| `credit_review/policy.py` | Policy schema and validator (references, traceability to the policy text, guardrails) |
| `credit_review/pdf.py` | Text layer as visual lines with positions; quote location; page images |
| `credit_review/classify.py` | Document type: policy keywords first, model for scans |
| `credit_review/extract.py` | Policy-driven extraction; code re-parses values and locates every quote |
| `credit_review/llm.py` | Anthropic + OpenAI-compatible (LM Studio) behind one cached interface |
| `credit_review/golden.py` | Hand-labelled reference: offline replay and accuracy scoring |
| `credit_review/documents.py` | Document checklist: present, missing, duplicated (by content, e.g. the same month twice) |
| `credit_review/facts.py` | Observations pooled per field: agreement, precision differences, conflicts with evidence |
| `credit_review/derive.py` | Every computed number, with its working written out |
| `credit_review/integrity.py` | Check digits, encoded dates, arithmetic, impossible dates |
| `credit_review/engine.py` | Every rule: outcome, reason, evidence; conflicts evaluated per candidate value |
| `credit_review/judge.py` | Judgment checks: cited facts only, never 'pass' |
| `credit_review/pipeline.py` | The full run, and `compare()` for policy changes |
| `golden/` | Reference values for the case dossier |
