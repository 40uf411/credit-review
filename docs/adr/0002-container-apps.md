# 2. Hosting: Azure Container Apps, with the assessment as a background task

**Status:** accepted for the demo environment (`dev`). The production changes are listed at the end.

## Context

The service accepts a dossier (10 PDFs, a few MB), reads it with a model (about 10 calls and 30–60 s when nothing is
cached), applies the policy, and returns an assessment. Traffic is low and bursty: a few dossiers per analyst per day.
It must run in the EU, sign in to everything with Entra ID, and cost close to nothing when idle.

## Decision

**Azure Container Apps** (consumption plan) in Sweden Central, defined in `infra/app.tf`:

- **Over App Service:** revisions with traffic splitting and labels give a zero-downtime rollout. A new revision is
  tested on its own URL before it takes traffic (`deploy.yml`), with no slot pricing tier needed. Scale rules are KEDA,
  so the production change below is a configuration change, not a new platform.
- **Over AKS:** one service does not justify a cluster to run, patch and pay for.
- **Over Functions:** the run is long, CPU-bound on PDF parsing, and needs the same image the CLI and CI use.

**The assessment runs as a FastAPI background task** in the replica that accepted the upload:

- `POST /dossiers` stores the files in Blob, writes the status `pending`, answers 202, and starts the task.
- Status and results live in Blob, never in memory, so any replica can answer `GET`.
- The dossier id is content-addressed (documents, policy, dates, types). Posting the same dossier again returns the
  stored result, and every model reply is cached in Blob, so a re-run costs nothing.

## The limits we accept, and why they are safe here

| Risk | What happens | Why it is acceptable for the demo |
|---|---|---|
| The replica stops mid-run (scale-in, new revision, crash) | The status stops moving; after 15 minutes `GET` reports `stale` | Posting again resumes: the model calls already made come from the cache, so the retry is fast and free |
| A burst of uploads | Runs share one replica's CPU | `min_replicas = 1`, `max_replicas = 2`; demo traffic is a few dossiers |
| No back-pressure | Uploads are accepted however many are running | Same; the HTTP scale rule adds a replica at 20 concurrent requests |

`min_replicas = 1` keeps one replica warm, which removes the cold start and means background tasks are not stopped by
scale-to-zero. It costs about €1–2 over the demo days, within the free monthly grant.

## Accepted findings, not in this demo

The infrastructure scan (`infra.yml`, Trivy) reports these. They are deliberate for a demo and are the first changes
for production:

- **Public endpoints** on Storage, the registry, Foundry and the app. Production uses private endpoints and a
  VNet-integrated Container Apps environment.
- **Locally redundant storage** (LRS). Production uses ZRS.
- **AI Search in a second EU region** (France Central): Sweden Central had no Search capacity for new services. The
  index holds public legal texts and every call stays in the EU; production keeps Search next to the app (capacity
  reserved in advance) to avoid the cross-region hop.
- **An API key** for callers. Production uses Entra ID authentication (Container Apps built-in auth, or API
  Management in front), with roles per analyst group.

## Production design

- **A queue instead of background tasks:** Service Bus, a Container Apps **job** scaled by KEDA on queue length,
  and a dead-letter queue for dossiers that fail repeatedly. The API only enqueues. Retries, back-pressure and
  scale-to-zero then come from the platform.
- **Workload profiles** with dedicated CPU if PDF parsing becomes the bottleneck.
- **Separate `prod` environment** (the GitHub `prod` environment, with a required reviewer, already exists in the
  OIDC trust), promoted from the same image digest that passed in `dev`.
