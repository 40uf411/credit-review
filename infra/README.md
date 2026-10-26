# Infrastructure (Terraform, azurerm 5.x)

Everything in Azure is created and owned by Terraform, except three things made by hand once: the
subscription's budget alert, the Foundry resource with its model deployments (adopted below with `import`),
and this bootstrap's own state file.

| Folder | What | State |
|---|---|---|
| `bootstrap/` | Remote-state storage, the GitHub identity (OIDC, no secret), its role assignments | Local, on the machine that ran it |
| `.` | The application: adopts the Foundry resource now; Container Apps, AI Search, storage and the rest follow | Remote, in the bootstrap's storage account |

## 1. Bootstrap (once, from your machine)

You need Owner on the subscription and `az login` done.

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars      # fill in: subscription, resource group, Foundry resource
terraform init
terraform apply
terraform output github_variables                 # set each one as a GitHub repository *variable*
terraform output -raw backend_config > ../backend.hcl
```

Commit the `.terraform.lock.hcl` that `init` creates (it pins the provider for every platform). Never commit
`terraform.tfstate` or `terraform.tfvars` (both gitignored).

Role assignments can take a few minutes to apply. If step 2 fails with 403 on the state storage, wait and retry.

## 2. Adopt the Foundry resource (once)

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars      # same values, plus the three deployment names
terraform init -backend-config=backend.hcl
terraform plan -generate-config-out=foundry.tf    # writes the configuration of what exists in Azure
terraform plan                                    # must show: 5 to import, 0 to add, 0 to change, 0 to destroy
terraform apply                                   # records the import in the remote state
```

Commit `foundry.tf` (it is tidied up afterwards) and the lock file.

## What signs in where

| Who | How | Allowed to |
|---|---|---|
| GitHub Actions | OIDC token for `repo:40uf411/credit-review` on PRs, `main`, environments `dev` and `prod` | State storage (blob data), the app resource group (Contributor, assign roles), call models |
| You | `az login` | State storage (blob data), call models |
| The application (later) | Its own managed identity | Only the data it uses |

No key or password exists anywhere: the state account has shared keys disabled, and the Foundry resource is
called with Entra ID tokens.
