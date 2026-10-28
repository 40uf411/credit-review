# Logs and traces. Capped, so a noisy loop cannot run up a bill.

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${var.name_prefix}"
  resource_group_name = data.azurerm_resource_group.app.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.5
  tags                = var.tags
}

resource "azurerm_application_insights" "this" {
  name                = "appi-${var.name_prefix}"
  resource_group_name = data.azurerm_resource_group.app.name
  location            = var.location
  workspace_id        = azurerm_log_analytics_workspace.this.id
  application_type    = "web"
  # Entra ID only: telemetry is accepted from identities holding Monitoring Metrics Publisher, so the
  # connection string (which is not a secret) cannot be used by anyone else to write into it.
  local_authentication_enabled = false
  tags                         = var.tags
}
