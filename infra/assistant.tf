# The regulation assistant: search index, Prompt Shields, conversation memory, and its container app.
# It runs the same image as the credit-review API with another command, under the same managed identity.

locals {
  assistant_index = "regulations"
}

# ------------------------------------------------------------------ retrieval: Azure AI Search

resource "azurerm_search_service" "this" {
  name                         = "srch-${var.name_prefix}-${local.suffix}"
  resource_group_name          = data.azurerm_resource_group.app.name
  location                     = var.location
  sku                          = "basic" # the smallest tier with the semantic ranker and a 99.9% SLA option
  semantic_search_sku          = "free"  # 1,000 semantic queries a month
  local_authentication_enabled = false   # Entra ID only: no admin or query keys
  replica_count                = 1
  partition_count              = 1
  tags                         = var.tags
}

# ------------------------------------------------------------------ Prompt Shields: Azure AI Content Safety

resource "azurerm_cognitive_account" "content_safety" {
  name                  = "cs-${var.name_prefix}-${local.suffix}"
  resource_group_name   = data.azurerm_resource_group.app.name
  location              = var.location
  kind                  = "ContentSafety"
  sku_name              = "S0"
  custom_subdomain_name = "cs-${var.name_prefix}-${local.suffix}"
  local_auth_enabled    = false # Entra ID only
  tags                  = var.tags
}

# ------------------------------------------------------------------ memory: a table in the existing storage account

resource "azurerm_storage_table" "conversations" {
  name               = "conversations"
  storage_account_id = azurerm_storage_account.app.id
}

# ------------------------------------------------------------------ who may do what

locals {
  assistant_roles = {
    # the service: read the index, ask Prompt Shields, keep conversations
    app-search-read   = { scope = azurerm_search_service.this.id, role = "Search Index Data Reader", principal = azurerm_user_assigned_identity.app.principal_id }
    app-shields       = { scope = azurerm_cognitive_account.content_safety.id, role = "Cognitive Services User", principal = azurerm_user_assigned_identity.app.principal_id }
    app-conversations = { scope = azurerm_storage_account.app.id, role = "Storage Table Data Contributor", principal = azurerm_user_assigned_identity.app.principal_id }
    # the ingestion workflow: create the index, write documents, scan them with Prompt Shields
    github-search-service = { scope = azurerm_search_service.this.id, role = "Search Service Contributor", principal = data.azurerm_user_assigned_identity.github.principal_id }
    github-search-data    = { scope = azurerm_search_service.this.id, role = "Search Index Data Contributor", principal = data.azurerm_user_assigned_identity.github.principal_id }
    github-shields        = { scope = azurerm_cognitive_account.content_safety.id, role = "Cognitive Services User", principal = data.azurerm_user_assigned_identity.github.principal_id }
  }
}

resource "azurerm_role_assignment" "assistant" {
  for_each             = local.assistant_roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = each.value.principal
  principal_type       = "ServicePrincipal"
}

# ------------------------------------------------------------------ the assistant's container app

resource "azurerm_container_app" "assistant" {
  name                         = "ca-assistant"
  resource_group_name          = data.azurerm_resource_group.app.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Multiple" # same rollout as the API: tested on a label before it takes traffic
  max_inactive_revisions       = 5
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  secret {
    name  = "api-key"
    value = "set-by-deploy" # deploy.yml sets the real value from the GitHub environment secret
  }

  ingress {
    external_enabled = true
    target_port      = 80 # the placeholder's port; deploy.yml sets 8000
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # Conversations live in Table Storage, so any replica can answer; one stays warm for the demo.
    min_replicas = 1
    max_replicas = 2

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }

    container {
      name   = "assistant"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"
      env {
        name  = "APP_MODULE" # the image serves credit_review.api:app unless told otherwise
        value = "assistant.api:app"
      }
      env {
        name  = "AZURE_CLIENT_ID"
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
        name  = "ASSISTANT_EMBEDDING_DEPLOYMENT"
        value = var.embedding_deployment
      }
      env {
        name  = "ASSISTANT_SEARCH_ENDPOINT"
        value = "https://${azurerm_search_service.this.name}.search.windows.net"
      }
      env {
        name  = "ASSISTANT_SEARCH_INDEX"
        value = local.assistant_index
      }
      env {
        name  = "ASSISTANT_SHIELDS_ENDPOINT"
        value = "https://${azurerm_cognitive_account.content_safety.custom_subdomain_name}.cognitiveservices.azure.com/"
      }
      env {
        name  = "ASSISTANT_TABLE_ENDPOINT"
        value = azurerm_storage_account.app.primary_table_endpoint
      }
      env {
        name  = "CREDIT_REVIEW_STORAGE_URL" # the model cache, shared with the API
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

  depends_on = [azurerm_role_assignment.app, azurerm_role_assignment.assistant]
}
