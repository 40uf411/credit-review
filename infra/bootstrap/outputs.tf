output "github_variables" {
  description = "Set these as repository variables (Settings > Secrets and variables > Actions > Variables). None is a secret."
  value = {
    AZURE_CLIENT_ID       = azurerm_user_assigned_identity.github.client_id
    AZURE_TENANT_ID       = data.azurerm_client_config.current.tenant_id
    AZURE_SUBSCRIPTION_ID = var.subscription_id
    TFSTATE_RG            = azurerm_resource_group.tfstate.name
    TFSTATE_ACCOUNT       = azurerm_storage_account.tfstate.name
  }
}

output "app_resource_group_name" {
  description = "Resource group the main configuration (infra/) deploys into."
  value       = azurerm_resource_group.app.name
}

output "backend_config" {
  description = "Contents of infra/backend.hcl for running the main configuration locally."
  value       = <<-EOT
    resource_group_name  = "${azurerm_resource_group.tfstate.name}"
    storage_account_name = "${azurerm_storage_account.tfstate.name}"
    container_name       = "${azurerm_storage_container.tfstate.name}"
    key                  = "dev.tfstate"
    use_azuread_auth     = true
  EOT
}
