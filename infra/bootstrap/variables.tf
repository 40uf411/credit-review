variable "subscription_id" {
  description = "Azure subscription that holds everything."
  type        = string
}

variable "location" {
  description = "Region for the state storage and the GitHub identity."
  type        = string
  default     = "swedencentral"
}

variable "github_repository" {
  description = "owner/repo whose workflows may sign in to Azure."
  type        = string
  default     = "40uf411/credit-review"
}

variable "app_resource_group_name" {
  description = "Existing resource group that holds the Foundry resource and, later, the application."
  type        = string
}

variable "foundry_account_name" {
  description = "Existing Foundry (Azure AI services) resource."
  type        = string
}

variable "name_prefix" {
  description = "Short prefix for resource names."
  type        = string
  default     = "creditreview"
}

variable "tags" {
  description = "Tags on every resource created here."
  type        = map(string)
  default = {
    project   = "credit-review"
    owner     = "40uf411"
    managedBy = "terraform-bootstrap"
  }
}
