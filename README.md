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

**Azure OpenAI (Microsoft Foundry):** set `CREDIT_REVIEW_LLM_PROVIDER=azure_openai`, the resource endpoint
in `CREDIT_REVIEW_LLM_BASE_URL` and the deployment name in `CREDIT_REVIEW_LLM_MODEL`. There is no key: it signs in
with Microsoft Entra ID (`az login` locally, a managed identity in Azure), and needs the *Cognitive Services OpenAI
User* role on the resource.

**LM Studio (fully local):** load a *vision-capable* model (the three payslips are scans),
set its context length to 16k or more, start the server (Developer → Start server, port
1234), and set `CREDIT_REVIEW_LLM_PROVIDER=lmstudio`. Leave `CREDIT_REVIEW_LLM_MODEL` empty to use the
first loaded model. Structured output (JSON Schema) is used when the server supports it,
otherwise the schema is put in the prompt.

## Run

**The analyst page:**

```bash
uv sync --extra ui
uv run streamlit run app.py
```

Pick the policy and the dossier (the sample, or upload PDFs), press *Detect document types*, check the types, then
*Run assessment*. Each finding opens to the policy text, the working and the quotes, and any quote can be shown
highlighted on its page. *Policy change* reruns the same file under another policy version without reading the
documents again: under v3.3, exactly two articles change (5.3 and 5.4, both to *not met*). *Cached results only* never
calls a model. With `CREDIT_REVIEW_STORAGE_URL` set, the page shares the API's model cache in Blob, so a dossier the
API has read costs nothing here. Hosted (`CREDIT_REVIEW_UI_HOSTED=1`) the page asks for the API key first, accepts
only the sample or an upload, and shows text from documents and models escaped.

**The command-line tools** validate policies, extract documents and generate assessments:

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
docker run --rm -p 8000:8000 -e CREDIT_API_KEY=dev credit-review     # the HTTP service; Swagger at /docs
```

**Comparing models.** `python -m credit_review.model_eval run --fresh` scores the
configured model on the reference set (text documents and scans separately), with cost per dossier and latency.
Each run is saved as `runs/eval/<model>[-<effort>].json`, and
`python -m credit_review.model_eval table "runs/eval/*.json"` compares them (the pattern is expanded by the tool,
so it works in PowerShell too). Prices per model are in
`config/model_prices.yaml`; a model without a price is reported as "n/a", never as free. For reasoning models,
`--reasoning-effort low|medium|high` compares the same model at different effort. The `eval.yml` workflow does the
same for Azure deployments in CI, with entries like `gpt-5.4-mini:low,gpt-5.4-mini:medium`.

**Model comparison results** (sample dossier, 186 hand-labelled values, Azure OpenAI in the EU data zone, prompt
extract-v2). Each configuration was run twice:

| Model | Accuracy | Text documents | Scans | Cost per dossier | Latency p50 / p95 |
|---|---|---|---|---|---|
| gpt-5.4-mini @ medium | 99.5% / 97.9% (185, 182) | 99.1% / 96.5% | 100% / 100% | 0.125 USD | 8.8 s / 19.7 s |
| gpt-5.4-mini @ low | 98.4% / 96.8% (183, 180) | — / 97.4% | 100% / 95.7% | 0.072 USD | 5.7 s / 9.4 s |
| claude-sonnet-5-5 (reference, Anthropic API) | 100% (186) | 100% | 100% | n/a | 7.9 s / 14.6 s |

Cost and latency are from the second runs (prices were recorded after the first). Two runs of the same configuration
differ by up to 3 values, which is as large as the difference between low and
medium effort. The service therefore uses **low**: about 60% of the cost and half the tail latency, for an accuracy
difference that one run cannot distinguish from noise, and every value is shown to the analyst with its evidence.
Effort is a Terraform variable (`infra/variables.tf`), so revisiting the choice is a one-line PR with a plan.
gpt-5.5 was the intended "full model" side; its quota was requested and denied, so it is not in the table.

Every model call is cached under `.credit_review_cache/`, keyed by provider, model, prompt,
schema and document. A second run is instant and identical. With `CREDIT_REVIEW_LLM_OFFLINE=1`
no model is ever called. Assessment results are written to `runs/latest/assessment.json`.
Use `--types path/to/types.json` with extraction or assessment to supply confirmed document
types as a JSON object mapping file names to policy document type IDs.

**Policy changes.** `python -m credit_review.impact policy/example_bank_v3.2.yaml policy/example_bank_v3.3.yaml`
reports which checks change outcome on the sample dossier (no model call). CI posts the same report on every policy
pull request. `policy/example_bank_v3.3.yaml` adds rule 5.3.e (renovation credits at
most 15 years) and tightens 5.4 (own funds 10% instead of 2%). The amended source text is in
`policy/example_bank_credit_policy_extract_v3.3.md`. Pass `--policy policy/example_bank_v3.3.yaml`
to the command-line tools to use it. In Python, `credit_review.pipeline.evaluate()` can
reuse an assessment's documents and extractions under another policy, and `compare()`
reports the changes. The validator rejects parameters or rule descriptions that disagree
with their cited policy text.

## Running in Azure

```
GitHub ── infra.yml: PR → Terraform plan as a PR comment; main → apply
       └─ deploy.yml: build → scan → push to ACR → new revision with no traffic → smoke + end-to-end test → promote
Azure (Sweden Central, everything Entra ID; no keys exist):
  Container Apps: credit-api (FastAPI)  ── managed identity ──▶ Foundry (gpt-5.4-mini, EU data zone)
                                                          ├──▶ Blob Storage: uploads, status, results, model cache
                                                          └──▶ Application Insights (traces: tokens, cost, cache hits)
```

```bash
curl -H "X-API-Key: $KEY" -F "files=@case_pack/dossier/payslip_may2026.pdf" ... \
     -F decision_date=2026-10-12 $API_URL/dossiers             # 202 {"id": ..., "status_url": ...}
curl -H "X-API-Key: $KEY" $API_URL/dossiers/<id>               # pending → running → done
curl -H "X-API-Key: $KEY" $API_URL/dossiers/<id>/assessment    # the same JSON the CLI writes
```

The assessment runs as a background task in the service; why that is acceptable here, and what replaces it in
production, is in `docs/adr/0002-container-apps.md`. Uploads expire after 30 days. Setup steps are in
`infra/README.md`.

## Regulation assistant

A second service in the same image answers questions on mortgage-lending rules in the EU, Belgium, France and the
Netherlands, from five public sources (`assistant/corpus/manifest.yaml`), with a citation for every statement.

```
message ─▶ limits (5 questions, 1,000 chars, rate) ─▶ Prompt Shields ─▶ topic check (mini model)
        ─▶ rewrite into a standalone question (earlier *questions* only) ─▶ Azure AI Search (keyword + vector +
           semantic re-ranking, filtered by country) ─▶ answer from the sources only (sources spotlighted as data,
           a per-call canary in the prompt) ─▶ every quote checked against its retrieved chunk ─▶ Table Storage
```

| Control | Where | Fails |
|---|---|---|
| At most 5 questions per conversation, 1,000 characters, per-user rate limit | `assistant/guards.py` | closed, before any paid call |
| Direct prompt attacks (jailbreak) | Prompt Shields on the message | closed: no verdict is a refusal |
| Indirect attacks planted in sources | Prompt Shields document scan at ingestion; flagged chunks are never indexed | closed |
| Off-topic use | one structured model call; anything but an explicit yes is a refusal | closed |
| Instructions inside sources | sources delimited and declared data; delimiters cannot be closed from inside | — |
| Ungrounded or invented answers | each citation must quote ≥ 20 characters verbatim from a chunk that was retrieved | answer withheld |
| System-prompt leaks | a random canary per call; seen in the answer means withheld | answer withheld |
| Another user's conversation | conversations are keyed by user (Table partition); a foreign id is "not found" | — |
| Memory poisoning | only the user's own earlier questions feed the next turn, never model output | — |

Refusals are fixed messages that do not say which rule matched; logs carry the guard code, never the user's text.
Ingestion (`ingest.yml`) pins each source by sha256: a document that changes stops the run instead of silently
changing the answers.

**Chat page:** open `$ASSISTANT_URL/` and paste the API key; it stays in that browser tab (sessionStorage) and goes
out only as the `X-API-Key` header. The page loads nothing from other sites, and renders every answer and quote as
plain text, so text inside a source or an answer cannot run as markup or script in the browser. Or call the API:

```bash
curl -H "X-API-Key: $KEY" -H "X-User-Id: alice" -X POST $ASSISTANT_URL/conversations          # {"id": ...}
curl -H "X-API-Key: $KEY" -H "X-User-Id: alice" -H "Content-Type: application/json" \
     -d '{"text": "What is the maximum loan-to-value in the Netherlands?"}' $ASSISTANT_URL/conversations/<id>/messages
```

## Cost

| What | Cost | How it is known |
|---|---|---|
| Reading one dossier (10 documents, gpt-5.4-mini, low effort) | about **0.07 USD** the first time | measured: `model_eval` with token prices from `config/model_prices.yaml` |
| Reading the same dossier again | **0** | every model reply is cached in Blob, keyed by its inputs; the deploy test shows it on every run |
| One assistant question | 3 model calls (topic, rewrite, answer) + 1 embedding + 1 Prompt Shields call; the topic and rewrite calls are cached | the `llm` spans in Application Insights carry tokens and cost per call |
| Idle environment | dominated by **AI Search Basic** (a fixed hourly price) and the two warm replicas (`min_replicas = 1`); the registry, storage, Log Analytics (capped at 0.5 GB/day) and Content Safety are cents | estimate from the Azure pricing pages; the actual figure is in Cost Management, with a resource-group budget alert at 50% and 90% |

Two levers bring idle cost close to zero after the demo: `min_replicas = 0` (accepting cold starts, and moving the
assessment to a queue, ADR 0002) and `terraform destroy`; the search index is rebuilt from the manifest in minutes.

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
| `policy-impact.yml` | PRs touching `policy/`, manual | Validates the changed policy, runs the sample dossier under the policy before and after, and posts the checks whose outcome changes as one PR comment |
| `infra.yml` | PRs and `main` touching `infra/` | `terraform fmt` and `validate`, Trivy configuration scan; the plan posted on the PR; apply on `main` (environment `dev`) |
| `deploy.yml` | `main` (app changes), manual | Build, Trivy (fixable HIGH/CRITICAL fail), push to ACR, a new revision tested on a `staging` label (health, auth, the sample dossier end to end), then 100% of traffic; automatic rollback on failure |
| `eval-assistant.yml` | manual | Runs `assistant/evals/cases.yaml` against the deployed assistant: answer accuracy, false refusals, refusal accuracy, latency; fails if an attack or limit breach is not refused |
| `ingest.yml` | changes to the corpus, manual | Builds the assistant's search index: download, hash check, chunk, Prompt Shields scan, embed |
| `eval.yml` | manual | Scores Azure OpenAI deployments on the reference set and posts a comparison (OIDC sign-in, no stored key) |
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
| `credit_review/llm.py` | Anthropic, OpenAI-compatible (LM Studio) and Azure OpenAI behind one cached interface |
| `credit_review/prices.py`, `config/model_prices.yaml` | Cost of each model call from its token usage |
| `assistant/` | The regulation assistant: guards, retrieval, memory, chat, ingestion, API |
| `credit_review/api.py` | The HTTP service: upload, background assessment, status, result |
| `credit_review/storage.py` | Local folder or Azure Blob, behind one interface (uploads, results, the model cache) |
| `credit_review/telemetry.py` | Traces to Application Insights, signed in with the managed identity |
| `infra/` | Terraform: bootstrap (state, GitHub OIDC identity), Foundry, Container Apps, registry, storage, monitoring, budget |
| `credit_review/model_eval.py` | Score a model on the reference set; compare models |
| `credit_review/golden.py` | Hand-labelled reference: offline replay and accuracy scoring |
| `credit_review/documents.py` | Document checklist: present, missing, duplicated (by content, e.g. the same month twice) |
| `credit_review/facts.py` | Observations pooled per field: agreement, precision differences, conflicts with evidence |
| `credit_review/derive.py` | Every computed number, with its working written out |
| `credit_review/integrity.py` | Check digits, encoded dates, arithmetic, impossible dates |
| `credit_review/engine.py` | Every rule: outcome, reason, evidence; conflicts evaluated per candidate value |
| `credit_review/judge.py` | Judgment checks: cited facts only, never 'pass' |
| `credit_review/pipeline.py` | The full run, and `compare()` for policy changes |
| `credit_review/impact.py` | What a policy change does to the sample dossier: the report CI posts on policy PRs |
| `golden/` | Reference values for the case dossier |
| `.github/` | CI workflows, Dependabot, code owners, PR template |
| `Dockerfile` | Multi-stage image: locked dependencies, non-root, policy as configuration |
| `docs/adr/` | Architecture decision records |
