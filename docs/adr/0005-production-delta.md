# 5. From this demo to production

**Status:** proposed: the changes a client deployment needs, in the order we would make them.

The demo environment (`dev`) shows the shape: infrastructure as code, keyless identities, a gated rollout, an
evaluation harness. This is what it leaves out on purpose, and what replaces it.

| Area | In the demo | In production | Why it waits |
|---|---|---|---|
| **Caller identity** | `X-API-Key` plus `X-User-Id` from a trusted client | Entra ID: Container Apps built-in auth or API Management validating tokens; the user id comes from the token, roles from groups (analyst, reviewer, admin) | needs the client's tenant and app registrations |
| **Background work** | FastAPI background tasks; a stopped replica gives `stale`, recovered by re-posting (ADR 0002) | Service Bus queue + Container Apps **job** scaled by KEDA on queue length, dead-letter queue, retries with backoff | one queue per workload type to design with the client |
| **Network** | public endpoints, Entra-only auth | VNet-integrated environment, **private endpoints** for Foundry, Search, Storage, Content Safety, ACR; public access off | Premium ACR and private DNS cost more than the demo budget |
| **AI gateway** | services call Foundry directly | **API Management** in front of the models: per-team token quotas, load balancing across regions or a provisioned deployment, semantic caching, central logging | worth it once several apps share the models |
| **Model capacity** | Data Zone Standard, 100K TPM (pay as you go) | Provisioned throughput (PTU) for the steady load, spill-over to Standard; a fallback deployment | needs measured volumes |
| **Secrets** | one GitHub environment secret | Key Vault references (ADR 0004) | only one secret exists today |
| **Rate limits** | in memory, per replica | in API Management (per subscription and per user), or Redis | needs the gateway |
| **Memory** | Table Storage, 24h expiry checked on read | the same, plus a scheduled purge of expired rows; Cosmos DB if conversations need search or analytics | volumes are tiny |
| **Data protection** | uploads expire after 30 days; LRS storage | retention agreed with the client's DPO; ZRS or GRS; customer-managed keys if required; a DPIA | client policy |
| **Groundedness** | quotes checked verbatim against retrieved chunks | plus Azure AI Content Safety **groundedness detection** on the answer text, and Foundry evaluations (groundedness, relevance) on a schedule | the verbatim check already blocks invented citations |
| **Environments** | `dev` only | `prod` from the same image digest, promoted after `dev` passes; the `prod` GitHub environment already requires a reviewer | — |
| **Observability** | traces in App Insights | dashboards and alerts on refusal rates, 5xx, latency p95, cost per day; an on-call runbook | needs owners |
| **Regulation** | — | EU AI Act: creditworthiness assessment of natural persons is **high-risk** (Annex III); obligations apply from 2 December 2027: risk management, logging, human oversight, technical documentation. The design already keeps the decision with the analyst ("AI reads, rules decide, analysts conclude") | — |
| **PDF licence** | PyMuPDF (AGPL) | commercial licence or a replacement (ADR 0001) | — |
