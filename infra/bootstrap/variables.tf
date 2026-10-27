variable "subscription_id" {
  description = "Azure subscription that holds everything."
  type        = string
}

variable "location" {
  description = "Region for everything: state, identity and the application (EU, for data residency)."
  type        = string
  default     = "swedencentral"
}

variable "github_repository" {
  description = "owner/repo whose workflows may sign in to Azure."
  type        = string
  default     = "40uf411/credit-review"
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
