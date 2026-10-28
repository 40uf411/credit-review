# Infrastructure (Terraform, azurerm 5.x)

Everything in Azure is created and owned by Terraform, in **Sweden Central** (EU), except the subscription's budget
alert and this bootstrap's own state file.

| Folder | What | State |
|---|---|---|
| `bootstrap/` | The app resource group, remote-state storage, the GitHub identity (OIDC, no secret) and its roles | Local, on the machine that ran it |
| `.` | The application: Microsoft Foundry and its model deployments, Container Apps, the registry, storage, Log Analytics and Application Insights, a budget | Remote, in the bootstrap's storage account |

Commands are the same in PowerShell and bash.

## 1. Bootstrap (once, from your machine)

You need Owner on the subscription, and `az login` done.

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars      # fill in the subscription ID
terraform init
terraform apply
terraform output github_variables                 # set each one as a GitHub repository *variable* (not a secret)
terraform output -raw backend_config > ../backend.hcl
```

The GitHub identity trusts both forms of GitHub's OIDC subject: by name (`repo:owner/repo:...`) and with the
immutable owner and repository IDs (`repo:owner@ID/repo@ID:...`), which GitHub now issues by default. For another
repository, set `github_repository`, `github_owner_id` and `github_repository_id` (see `terraform.tfvars.example`).

Commit the `.terraform.lock.hcl` that `init` creates (it pins the provider for every platform). Never commit
`terraform.tfstate` or `terraform.tfvars` (both gitignored).

## 2. Foundry and the models

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars      # subscription ID, and your object ID:
                                                  #   az ad signed-in-user show --query id -o tsv
terraform init -backend-config=backend.hcl
terraform plan                                    # Foundry resource, the deployments, 2 role assignments
terraform apply
terraform output openai_endpoint                  # set as the GitHub variable AZURE_OPENAI_ENDPOINT
```

Deployments are defined in `variables.tf` (model, pinned version, SKU, capacity). gpt-5.5 is listed but commented
out until the subscription has quota for it; adding it is uncommenting four lines and `terraform apply`.

If the state storage answers 403 right after the bootstrap, role assignments are still spreading: wait a minute and
retry. If creating two deployments at once fails with 409 ("another operation is in progress"), run `terraform apply`
again; it continues where it stopped.

## 3. The service (Container Apps) and its pipelines

Once per subscription, register the resource providers (each takes a minute):

```bash
az provider register -n Microsoft.App
az provider register -n Microsoft.OperationalInsights
az provider register -n Microsoft.ContainerRegistry
az provider register -n Microsoft.Insights
```

Then:

```bash
cd infra
terraform apply                                   # registry, storage, monitoring, identity, Container Apps
terraform output github_variables                 # ACR_NAME, CONTAINER_APP, APP_RESOURCE_GROUP, API_URL
```

In GitHub:

- Set those four as repository **variables**. Also set `MODEL_USER_OBJECT_IDS` (the same JSON list as in your
  `terraform.tfvars`, e.g. `["<your object id>"]`) and optionally `BUDGET_EMAILS` (a JSON list), so that CI applies
  the same configuration you do.
- Create the **secret** `CREDIT_API_KEY` in the `dev` environment: any long random string, e.g. in PowerShell
  `-join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) })`. It is copied into the container app by
  `deploy.yml`, never into Terraform state.
- Run **Actions → Deploy**. The first run replaces the placeholder image with the service; open `API_URL/docs`.

From then on:

- A PR that touches `infra/` gets its plan as a comment, and merging applies it. A change to the app's settings
  (for example `reasoning_effort`) starts a deployment, because it creates a revision that must be tested and
  promoted.
- A merge that changes the code deploys it.

Terraform owns the resources and the app's settings. `deploy.yml` owns the running image, the ingress port, traffic
between revisions and the API key; those are in `ignore_changes`, so an apply never rolls back a deployment.

## 4. The assistant

`terraform apply` (or merging an `infra/` PR) adds Azure AI Search (Basic), Content Safety (Prompt Shields), a
`conversations` table and the `ca-assistant` container app. Then:

- Set the new `github_variables` as repository variables: `ASSISTANT_APP`, `ASSISTANT_URL`, `SEARCH_ENDPOINT`,
  `SHIELDS_ENDPOINT`.
- Run **Actions → Ingest corpus** (first with *dry run* to see which documents download, then for real). Pin the
  printed sha256 values in `assistant/corpus/manifest.yaml`.
- Run **Actions → Deploy**: it now rolls out both apps from one image and tests the assistant with one cited answer
  and one off-topic refusal.

## 5. The analyst page

`terraform apply` (or merging the `infra/` PR) adds the `ca-analyst` container app: the Streamlit page (`app.py`)
from the same image, with the same identity, model and model cache as the API. Then set the repository variables
`ANALYST_APP` and `ANALYST_URL` from `github_variables`, and run **Actions → Deploy**: it rolls out all three apps
and checks that the page is healthy and loads. The page asks for the API key before it shows anything.

## What signs in where

| Who | How | Allowed to |
|---|---|---|
| GitHub Actions | OIDC token for `repo:40uf411/credit-review` on PRs, `main`, environments `dev` and `prod` | State storage (blob data), read the state resource group, the app resource group (Contributor, assign roles), push images, call the models |
| You | `az login` | State storage (blob data), call the models |
| The service (`id-creditreview-app`) | Its user-assigned managed identity | Pull its image, read and write its storage account, call the models, send telemetry |

No key or password exists anywhere: the state account has shared keys disabled, and the Foundry resource has local
(key) authentication disabled, so every call carries an Entra ID token.
