output "openai_endpoint" {
  description = "Set as the GitHub repository variable AZURE_OPENAI_ENDPOINT (and CREDIT_REVIEW_LLM_BASE_URL locally)."
  value       = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/"
}

output "deployments" {
  description = "Deployment names the application can call."
  value       = keys(azurerm_cognitive_deployment.model)
}

output "api_url" {
  description = "The service's public address (Swagger UI at /docs)."
  value       = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "github_variables" {
  description = "Repository variables deploy.yml needs (Settings → Secrets and variables → Actions → Variables)."
  value = {
    ACR_NAME           = azurerm_container_registry.this.name
    CONTAINER_APP      = azurerm_container_app.api.name
    APP_RESOURCE_GROUP = data.azurerm_resource_group.app.name
    API_URL            = "https://${azurerm_container_app.api.ingress[0].fqdn}"
    ASSISTANT_APP      = azurerm_container_app.assistant.name
    ASSISTANT_URL      = "https://${azurerm_container_app.assistant.ingress[0].fqdn}"
    ANALYST_APP        = azurerm_container_app.analyst.name
    ANALYST_URL        = "https://${azurerm_container_app.analyst.ingress[0].fqdn}"
    SEARCH_ENDPOINT    = "https://${azurerm_search_service.this.name}.search.windows.net"
    SHIELDS_ENDPOINT   = "https://${azurerm_cognitive_account.content_safety.custom_subdomain_name}.cognitiveservices.azure.com/"
  }
}
