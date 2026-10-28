# A spending alert on everything in the application's resource group (the subscription-wide one is set by hand).

resource "azurerm_consumption_budget_resource_group" "app" {
  count             = length(var.budget_emails) > 0 ? 1 : 0
  name              = "budget-${var.name_prefix}"
  resource_group_id = data.azurerm_resource_group.app.id
  amount            = var.budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  dynamic "notification" {
    for_each = [50, 90]
    content {
      enabled        = true
      threshold      = notification.value
      operator       = "GreaterThanOrEqualTo"
      threshold_type = "Actual"
      contact_emails = var.budget_emails
    }
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Forecasted"
    contact_emails = var.budget_emails
  }
}
