output "openai_endpoint" {
  description = "Set as the GitHub repository variable AZURE_OPENAI_ENDPOINT (and CREDIT_REVIEW_LLM_BASE_URL locally)."
  value       = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/"
}

output "deployments" {
  description = "Deployment names the application can call."
  value       = keys(azurerm_cognitive_deployment.model)
}
