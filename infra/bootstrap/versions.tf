terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.11"
    }
  }

  # Local state on purpose: this configuration creates the remote state storage that every other
  # configuration uses. Its own state file stays on the machine that ran it (and is gitignored).
}

provider "azurerm" {
  features {}
  subscription_id     = var.subscription_id
  storage_use_azuread = true # the state account has shared keys disabled
}
