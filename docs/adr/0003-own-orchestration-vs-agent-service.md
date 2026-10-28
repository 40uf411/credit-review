# 3. The assistant's orchestration: our own code, not Foundry Agent Service

**Status:** accepted.

## Context

The assistant answers questions on lending rules from five public sources, with every statement cited. Each turn
runs a fixed sequence: limits, Prompt Shields, a topic check, a question rewrite, retrieval, a cited answer, a
citation check and storage. Microsoft Foundry offers a managed alternative: **Foundry Agent Service**, which keeps
threads, calls tools (including Azure AI Search and Foundry IQ knowledge bases) and lets the model decide the steps.
Prompt Flow, the earlier low-code option, is being retired (20 April 2027) in favour of the Microsoft Agent Framework.

## Decision

Orchestrate in our own Python (`assistant/chat.py`), with the guards injected (`assistant/guards.py`), and use the
platform for what it does best: models, embeddings, Prompt Shields and AI Search.

**Why:**

- **The flow is fixed, not agentic.** Every turn must run every guard in a known order, cheap before paid, and stop at
  the first refusal. An agent that chooses its own steps can skip a step; a function cannot.
- **The guards must be testable without Azure.** Each guard and the whole turn have unit tests with fakes (fail
  closed, 6th message, a leaked canary, a source that tries to close its delimiters). With an agent service, most of
  that becomes an integration test against a hosted thread.
- **Citations are checked in code.** The answer is withheld unless every quote appears verbatim in a chunk that was
  retrieved. That needs the exact chunks the model saw, which our code holds.
- **Memory is ours to restrict.** Only the user's earlier *questions* feed the next turn, never model output, and a
  conversation is keyed by its user. A hosted thread keeps everything the model said.
- **Same patterns as the credit-review service:** the same `LLMClient` (cache, cost, tracing), deployment, tests and
  evaluation.

## Consequences

- We own the conversation store (Table Storage), the rewrite step and the prompt versions; about 300 lines.
- No built-in agent tracing UI. OpenTelemetry spans to Application Insights cover the model calls instead.
- **When Agent Service is the better choice:** open-ended tasks with many tools, where the model should plan (for
  example "draft the analyst's note, look up the borrower's other loans, then schedule a review"). The guards would
  then wrap the agent as pre- and post-processing, and Foundry evaluations would replace part of our harness.
