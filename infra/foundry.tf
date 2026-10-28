# Microsoft Foundry (Azure AI services) and its model deployments, in the EU.
# Entra ID only: local (key) authentication is disabled, so there is no key to leak or rotate.

data "azurerm_resource_group" "app" {
  name = "rg-${var.name_prefix}-dev" # created by infra/bootstrap
}

data "azurerm_user_assigned_identity" "github" {
  name                = "id-github-${var.name_prefix}" # created by infra/bootstrap
  resource_group_name = "rg-${var.name_prefix}-tfstate"
}

locals {
  # Custom subdomains are global; a stable suffix from the subscription keeps the name unique and reruns idempotent.
  foundry_name = "foundry-${var.name_prefix}-${substr(sha1(var.subscription_id), 0, 6)}"
}

resource "azurerm_cognitive_account" "foundry" {
  name                          = local.foundry_name
  resource_group_name           = data.azurerm_resource_group.app.name
  location                      = var.location
  kind                          = "AIServices"
  sku_name                      = "S0"
  custom_subdomain_name         = local.foundry_name
  local_auth_enabled            = false
  public_network_access_enabled = true # private endpoints are in the production delta (docs/adr)
  project_management_enabled    = true # a Foundry resource, so projects, evaluations and agents can be added
  tags                          = var.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_deployment" "model" {
  for_each             = var.deployments
  name                 = each.key
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  # Pinned: a model version changes answers, so upgrades go through the evaluation like any other change.
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = "OpenAI"
    name    = each.value.model
    version = each.value.version
  }

  sku {
    name     = each.value.sku
    capacity = each.value.capacity
  }
}

# Who may call the models: the service, the evaluation workflow, and people running evaluations locally.
locals {
  model_users = merge(
    { app = azurerm_user_assigned_identity.app.principal_id },
    { github = data.azurerm_user_assigned_identity.github.principal_id },
    { for i, id in var.model_user_object_ids : "user-${i}" => id },
  )
}

resource "azurerm_role_assignment" "model_user" {
  for_each             = local.model_users
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = each.value
}
