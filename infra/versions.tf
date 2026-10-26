terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.11"
    }
  }

  # State lives in the storage account created by infra/bootstrap. The values come from backend.hcl
  # locally (printed by the bootstrap) and from repository variables in CI. Entra ID auth: no keys.
  backend "azurerm" {}
}

provider "azurerm" {
  features {}
  subscription_id     = var.subscription_id
  storage_use_azuread = true
}
