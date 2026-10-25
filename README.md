# Credit Review for Example Bank — prototype

[![CI](https://github.com/40uf411/credit-review/actions/workflows/ci.yml/badge.svg)](https://github.com/40uf411/credit-review/actions/workflows/ci.yml)
[![Container](https://github.com/40uf411/credit-review/actions/workflows/container.yml/badge.svg)](https://github.com/40uf411/credit-review/actions/workflows/container.yml)
[![CodeQL](https://github.com/40uf411/credit-review/actions/workflows/codeql.yml/badge.svg)](https://github.com/40uf411/credit-review/actions/workflows/codeql.yml)

Reads a mortgage dossier, checks it against Example Bank's credit policy, and reports every
finding with the evidence behind it. **AI reads, rules decide, analysts conclude.**

> **Sample data only.** The PDFs and reference labels in this repository are synthetic and
> contain realistic-looking identity and financial data. Never commit real borrower files,
> customer policy documents, model caches, generated assessments, or API credentials.

## Setup

```bash
uv sync                                                  # creates .venv from the lockfile (Python 3.12+)
source .venv/bin/activate                                # Windows: .venv\Scripts\activate
cp .env.example .env                                     # then choose a provider
```

Without uv: `python -m venv .venv && source .venv/bin/activate && pip install -e . pytest`.

**Anthropic API:** set `ANTHROPIC_API_KEY`. Default model `claude-sonnet-5-5`.

**LM Studio (fully local):** load a *vision-capable* model (the three payslips are scans),
set its context length to 16k or more, start the server (Developer → Start server, port
1234), and set `CREDIT_REVIEW_LLM_PROVIDER=lmstudio`. Leave `CREDIT_REVIEW_LLM_MODEL` empty to use the
first loaded model. Structured output (JSON Schema) is used when the server supports it,
otherwise the schema is put in the prompt.

## Run

Use the command-line tools to validate policies, extract documents and generate assessments:

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

Or in a container (documents mounted read-only, cached model results only):

```bash
docker build -t credit-review .
docker run --rm -v "$PWD/case_pack:/data:ro" -e CREDIT_REVIEW_LLM_OFFLINE=1 \
  credit-review credit-review /data/dossier --decision-date 2026-10-12 --out /tmp/out
```

Every model call is cached under `.credit_review_cache/`, keyed by provider, model, prompt,
schema and document. A second run is instant and identical. With `CREDIT_REVIEW_LLM_OFFLINE=1`
no model is ever called. Assessment results are written to `runs/latest/assessment.json`.
Use `--types path/to/types.json` with extraction or assessment to supply confirmed document
types as a JSON object mapping file names to policy document type IDs.

**Policy changes.** `policy/example_bank_v3.3.yaml` adds rule 5.3.e (renovation credits at
most 15 years) and tightens 5.4 (own funds 10% instead of 2%). The amended source text is in
`policy/example_bank_credit_policy_extract_v3.3.md`. Pass `--policy policy/example_bank_v3.3.yaml`
to the command-line tools to use it. In Python, `credit_review.pipeline.evaluate()` can
reuse an assessment's documents and extractions under another policy, and `compare()`
reports the changes. The validator rejects parameters or rule descriptions that disagree
with their cited policy text.

## Development and CI

```bash
uv run pytest --cov          # tests with coverage (gate: 80%)
uv run ruff check .          # lint
uv run pyright               # types
uv run pre-commit install    # the same checks before every commit
```

| Workflow | Runs on | What it enforces |
|---|---|---|
| `ci.yml` | push to `main`, PRs | Ruff, Pyright, tests on Python 3.12 and 3.13 with coverage, **every policy YAML validated against its source text**, copyleft-licence check, secret scan |
| `container.yml` | push to `main`, PRs | Image build, smoke test (CLI, policy validation, non-root user), Trivy image and Dockerfile scans (fixable HIGH/CRITICAL fail), publish to GHCR with SBOM and provenance on `main` |
| `codeql.yml` | push, PRs, weekly | CodeQL security analysis of the Python code |
| `dependency-review.yml` | PRs | Blocks new dependencies with high-severity vulnerabilities or copyleft licences |

Dependabot (`.github/dependabot.yml`) proposes weekly grouped updates for Python packages, workflow
actions and base images, and waits 7 days before adopting a new release. Actions and base images are
pinned to commit SHAs and digests. `CODEOWNERS` mirrors the ownership lanes in the policy YAML.
Decisions are recorded in `docs/adr/`.

## Layout

| Path | What |
|---|---|
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
| `.github/` | CI workflows, Dependabot, code owners, PR template |
| `Dockerfile` | Multi-stage image: locked dependencies, non-root, policy as configuration |
| `docs/adr/` | Architecture decision records |
