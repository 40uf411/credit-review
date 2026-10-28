# The analyst page (app.py, Streamlit) as a third Container App: the same image and identity as the API, started with
# APP_MODULE=analyst. It reads dossiers with the same model and the same Blob model cache as the API, and asks for the
# same API key before it shows anything (CREDIT_REVIEW_UI_HOSTED).

resource "azurerm_container_app" "analyst" {
  name                         = "ca-analyst"
  resource_group_name          = data.azurerm_resource_group.app.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Multiple" # same rollout as the other apps: tested on a label before it takes traffic
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
    # A Streamlit session lives in one replica's memory, over a websocket: exactly one replica, always warm.
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "analyst"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 1 # renders PDF pages and runs the pipeline in the page's process
      memory = "2Gi"
      env {
        name  = "APP_MODULE"
        value = "analyst"
      }
      env {
        name  = "CREDIT_REVIEW_UI_HOSTED" # key first; sample or upload only, never a server path
        value = "1"
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
        name  = "CREDIT_REVIEW_STORAGE_URL" # the model cache, shared with the API
        value = azurerm_storage_account.app.primary_blob_endpoint
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING" # traces, like the API's: one `assess` span per run
        value = azurerm_application_insights.this.connection_string
      }
      env {
        name  = "ASSISTANT_URL" # the page's assistant view calls the assistant's API, with the same key
        value = "https://${azurerm_container_app.assistant.ingress[0].fqdn}"
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
