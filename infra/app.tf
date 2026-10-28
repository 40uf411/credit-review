# The credit-review service: an image registry, its storage, its identity and the container app.
#
# Who owns what:
#   Terraform  - the resources, the app's settings (env, scale, identity) and its roles.
#   deploy.yml - which image runs, the ingress port and traffic between revisions, and the API key secret.
# Those are in ignore_changes below, so an apply never rolls back a deployment or overwrites the secret.

locals {
  suffix = substr(sha1(var.subscription_id), 0, 6) # globally unique names, stable across reruns
}

# ------------------------------------------------------------------ registry

resource "azurerm_container_registry" "this" {
  name                = "acr${replace(var.name_prefix, "-", "")}${local.suffix}"
  resource_group_name = data.azurerm_resource_group.app.name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false # pushes and pulls use Entra ID (GitHub OIDC, the app's managed identity)
  tags                = var.tags
}

# ------------------------------------------------------------------ storage: uploads, results, model cache

resource "azurerm_storage_account" "app" {
  name                            = "st${substr(replace(var.name_prefix, "-", ""), 0, 8)}app${local.suffix}"
  resource_group_name             = data.azurerm_resource_group.app.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # Entra ID only: no connection string exists to leak
  default_to_oauth_authentication = true
  tags                            = var.tags

  blob_properties {
    delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "this" {
  for_each              = toset(["dossiers", "llm-cache"])
  name                  = each.key
  storage_account_id    = azurerm_storage_account.app.id
  container_access_type = "private"
}

# Uploaded dossiers are personal data: kept 30 days, then deleted. The model cache is kept (it is what makes a
# re-run free and reproducible) and holds extracted values only, under the same access controls.
resource "azurerm_storage_management_policy" "app" {
  storage_account_id = azurerm_storage_account.app.id

  rule {
    name    = "expire-dossiers"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["dossiers/"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.dossier_retention_days
      }
    }
  }
}

# ------------------------------------------------------------------ the app's identity and what it may do

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${var.name_prefix}-app"
  resource_group_name = data.azurerm_resource_group.app.name
  location            = var.location
  tags                = var.tags
}

locals {
  app_roles = {
    pull-images     = { scope = azurerm_container_registry.this.id, role = "AcrPull" }
    read-write-blob = { scope = azurerm_storage_account.app.id, role = "Storage Blob Data Contributor" }
    send-telemetry  = { scope = azurerm_application_insights.this.id, role = "Monitoring Metrics Publisher" }
    # Calling the models: in foundry.tf, with everyone else who may call them.
  }
}

resource "azurerm_role_assignment" "app" {
  for_each             = local.app_roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# GitHub Actions pushes the images it builds.
resource "azurerm_role_assignment" "github_acr_push" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_user_assigned_identity.github.principal_id
  principal_type       = "ServicePrincipal"
}

# ------------------------------------------------------------------ Container Apps

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.name_prefix}"
  resource_group_name        = data.azurerm_resource_group.app.name
  location                   = var.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = var.tags
}

resource "azurerm_container_app" "api" {
  name                         = "ca-credit-api"
  resource_group_name          = data.azurerm_resource_group.app.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Multiple" # deploy.yml tests a new revision on a label before it takes traffic
  max_inactive_revisions       = 5          # what a rollback can go back to
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # The real value is set by deploy.yml from the GitHub environment secret, so it never enters Terraform state.
  secret {
    name  = "api-key"
    value = "set-by-deploy"
  }

  ingress {
    external_enabled = true
    target_port      = 80 # the placeholder's port; deploy.yml sets 8000 with the first real image
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # One replica stays up: assessments run as background tasks in the replica that accepted them, so scaling
    # to zero mid-run would stop them (docs/adr/0002-container-apps.md).
    min_replicas = 1
    max_replicas = 2

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }

    container {
      name = "api"
      # A public placeholder until deploy.yml pushes the first image: the registry is empty on the first apply.
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "AZURE_CLIENT_ID" # which managed identity DefaultAzureCredential uses
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name  = "CREDIT_REVIEW_LLM_PROVIDER"
        value = "azure_openai"
      }
      env {
        name  = "CREDIT_REVIEW_LLM_BASE_URL"
        value = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/"
      }
      env {
        name  = "CREDIT_REVIEW_LLM_MODEL"
        value = var.llm_deployment
      }
      env {
        name  = "CREDIT_REVIEW_LLM_REASONING_EFFORT"
        value = var.reasoning_effort
      }
      env {
        name  = "CREDIT_REVIEW_STORAGE_URL"
        value = azurerm_storage_account.app.primary_blob_endpoint
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.this.connection_string
      }
      env {
        name        = "CREDIT_API_KEY"
        secret_name = "api-key"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      secret,
      ingress[0].target_port,
      ingress[0].traffic_weight,
      template[0].revision_suffix,
      template[0].container[0].image,
    ]
  }

  depends_on = [azurerm_role_assignment.app]
}
