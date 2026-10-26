data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "app" {
  name = var.app_resource_group_name
}

data "azurerm_cognitive_account" "foundry" {
  name                = var.foundry_account_name
  resource_group_name = var.app_resource_group_name
}

locals {
  # Storage account names are global; a stable suffix from the subscription keeps reruns idempotent.
  suffix = substr(sha1(var.subscription_id), 0, 6)

  # GitHub OIDC subjects this identity trusts. Anything else (another repo, a fork, another branch
  # outside an environment) cannot get a token for it.
  github_subjects = {
    pull-request = "repo:${var.github_repository}:pull_request"
    main         = "repo:${var.github_repository}:ref:refs/heads/main"
    env-dev      = "repo:${var.github_repository}:environment:dev"
    env-prod     = "repo:${var.github_repository}:environment:prod"
  }
}

# ------------------------------------------------------------------ remote state

resource "azurerm_resource_group" "tfstate" {
  name     = "rg-${var.name_prefix}-tfstate"
  location = var.location
  tags     = var.tags
}

resource "azurerm_storage_account" "tfstate" {
  name                            = "st${substr(var.name_prefix, 0, 8)}tf${local.suffix}"
  resource_group_name             = azurerm_resource_group.tfstate.name
  location                        = azurerm_resource_group.tfstate.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # Entra ID only: no account keys to leak
  default_to_oauth_authentication = true
  tags                            = var.tags

  blob_properties {
    versioning_enabled = true # every state write is a version: a bad apply can be rolled back

    delete_retention_policy {
      days = 14
    }

    container_delete_retention_policy {
      days = 14
    }
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

# ------------------------------------------------------------------ GitHub Actions identity (OIDC, no secret)

resource "azurerm_user_assigned_identity" "github" {
  name                = "id-github-${var.name_prefix}"
  resource_group_name = azurerm_resource_group.tfstate.name
  location            = azurerm_resource_group.tfstate.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "github" {
  for_each                  = local.github_subjects
  name                      = "github-${each.key}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = each.value
}

# ------------------------------------------------------------------ role assignments

locals {
  roles = {
    # GitHub: read and write Terraform state
    github-state = {
      scope     = azurerm_storage_account.tfstate.id
      role      = "Storage Blob Data Contributor"
      principal = azurerm_user_assigned_identity.github.principal_id
      type      = "ServicePrincipal"
    }
    # GitHub: create and change the application's resources
    github-app = {
      scope     = data.azurerm_resource_group.app.id
      role      = "Contributor"
      principal = azurerm_user_assigned_identity.github.principal_id
      type      = "ServicePrincipal"
    }
    # GitHub: give the application's managed identity its roles. In production this assignment carries an
    # ABAC condition that limits which roles it may hand out.
    github-rbac = {
      scope     = data.azurerm_resource_group.app.id
      role      = "Role Based Access Control Administrator"
      principal = azurerm_user_assigned_identity.github.principal_id
      type      = "ServicePrincipal"
    }
    # GitHub: call the models (the evaluation workflow)
    github-models = {
      scope     = data.azurerm_cognitive_account.foundry.id
      role      = "Cognitive Services OpenAI User"
      principal = azurerm_user_assigned_identity.github.principal_id
      type      = "ServicePrincipal"
    }
    # You: run Terraform against the remote state from your machine
    me-state = {
      scope     = azurerm_storage_account.tfstate.id
      role      = "Storage Blob Data Contributor"
      principal = data.azurerm_client_config.current.object_id
      type      = null
    }
    # You: call the models from your machine (local evaluation runs)
    me-models = {
      scope     = data.azurerm_cognitive_account.foundry.id
      role      = "Cognitive Services OpenAI User"
      principal = data.azurerm_client_config.current.object_id
      type      = null
    }
  }
}

resource "azurerm_role_assignment" "this" {
  for_each             = local.roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = each.value.principal
  principal_type       = each.value.type
}
