# The resource group, the Foundry resource and its three model deployments were created by hand in the portal.
# These blocks adopt them into Terraform state without recreating anything. The first plan is run with
#   terraform plan -generate-config-out=foundry.tf
# which writes their configuration from what exists in Azure. From then on Terraform owns them, and the
# import blocks are no-ops (they can be removed once applied).

locals {
  rg_id      = "/subscriptions/${var.subscription_id}/resourceGroups/${var.app_resource_group_name}"
  foundry_id = "${local.rg_id}/providers/Microsoft.CognitiveServices/accounts/${var.foundry_account_name}"
}

import {
  to = azurerm_resource_group.app
  id = local.rg_id
}

import {
  to = azurerm_cognitive_account.foundry
  id = local.foundry_id
}

import {
  to = azurerm_cognitive_deployment.chat
  id = "${local.foundry_id}/deployments/${var.deployments.chat}"
}

import {
  to = azurerm_cognitive_deployment.mini
  id = "${local.foundry_id}/deployments/${var.deployments.mini}"
}

import {
  to = azurerm_cognitive_deployment.embeddings
  id = "${local.foundry_id}/deployments/${var.deployments.embeddings}"
}
