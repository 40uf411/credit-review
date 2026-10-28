# 4. Secrets: identities first, one GitHub environment secret, Key Vault in production

**Status:** accepted for the demo; Key Vault is the production change.

## Context

The services call Azure OpenAI, AI Search, Content Safety, Blob and Table Storage and Application Insights. CI signs
in to Azure to plan, apply and deploy. Every one of those could be a key or a connection string to store, rotate and
leak.

## Decision

**Remove the secrets rather than store them.**

| Who | Signs in with | Secret stored |
|---|---|---|
| GitHub Actions | OIDC token exchanged for a user-assigned managed identity (federated credentials, both subject formats) | none |
| The services | their user-assigned managed identity (`AZURE_CLIENT_ID`) | none |
| Azure resources | local or key authentication **disabled**: Foundry, Content Safety, AI Search, Storage (shared keys off), App Insights ingestion, ACR admin | none can exist |
| Callers of the APIs | `X-API-Key` | **one**: `CREDIT_API_KEY` |

The one real secret lives as a **GitHub environment secret** in `dev` (and later `prod`, which has a required
reviewer). `deploy.yml` copies it into a Container Apps secret on each rollout. Terraform creates the secret with a
placeholder and ignores its value, so the key **never enters Terraform state**.

Configuration that is not secret (endpoints, deployment names, the App Insights connection string, the IDs the
bootstrap prints) goes in repository variables and Container Apps environment variables.

## Consequences

- No rotation process for Azure credentials: tokens are short-lived and issued per call.
- Rotating the API key means updating one GitHub secret and re-running Deploy.
- **Production:** move `CREDIT_API_KEY` to **Key Vault**, referenced by the container app through its managed
  identity (Container Apps Key Vault references), with soft delete, purge protection and rotation alerts. Better
  still, replace the API key with Entra ID authentication for callers (ADR 0005).
