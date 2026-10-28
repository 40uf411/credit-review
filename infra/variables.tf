variable "subscription_id" {
  description = "Azure subscription that holds everything."
  type        = string
}

variable "location" {
  description = "Region of the application (EU, for data residency)."
  type        = string
  default     = "swedencentral"
}

variable "name_prefix" {
  description = "Short prefix for resource names; must match the bootstrap's."
  type        = string
  default     = "creditreview"
}

variable "deployments" {
  description = <<-EOT
    Model deployments on the Foundry resource, keyed by deployment name (the name the application calls).
    sku: DataZoneStandard keeps processing in the EU data zone; Standard keeps it in the region.
    capacity: thousands of tokens per minute, within the subscription's quota for that model, SKU and region.
  EOT
  type = map(object({
    model    = string
    version  = string
    sku      = string
    capacity = number
  }))
  default = {
    "gpt-5.4-mini" = {
      model = "gpt-5.4-mini", version = "2026-03-17", sku = "DataZoneStandard", capacity = 100
    }
    "text-embedding-3-large" = {
      model = "text-embedding-3-large", version = "1", sku = "Standard", capacity = 50
    }
    # Quota requested and denied (DataZoneStandard and GlobalStandard, Oct 2026). Kept for when it is granted.
    # "gpt-5.5" = {
    #   model = "gpt-5.5", version = "2026-04-24", sku = "DataZoneStandard", capacity = 50
    # }
  }
}

variable "model_user_object_ids" {
  description = "Entra object IDs of people who may call the models directly (e.g. you, for local evaluation runs)."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags on every resource Terraform creates."
  type        = map(string)
  default = {
    project   = "credit-review"
    owner     = "40uf411"
    env       = "dev"
    managedBy = "terraform"
  }
}

variable "llm_deployment" {
  description = "The deployment the service calls (a key of var.deployments)."
  type        = string
  default     = "gpt-5.4-mini"

  validation {
    condition     = contains(keys(var.deployments), var.llm_deployment)
    error_message = "llm_deployment must be one of the deployments in var.deployments."
  }
}

variable "reasoning_effort" {
  description = <<-EOT
    Reasoning effort the service sends. Measured on the reference set (README, "Model comparison"): low reads as
    accurately as medium within run-to-run noise, at about half the cost and half the p95 latency.
  EOT
  type        = string
  default     = "low"

  validation {
    condition     = contains(["minimal", "low", "medium", "high"], var.reasoning_effort)
    error_message = "reasoning_effort must be minimal, low, medium or high."
  }
}

variable "dossier_retention_days" {
  description = "Uploaded dossiers and their results are deleted this many days after the last change."
  type        = number
  default     = 30
}

variable "budget_emails" {
  description = "Who gets the resource group's budget alerts; no budget is created when empty."
  type        = list(string)
  default     = []
}

variable "budget_amount" {
  description = "Monthly budget for the application's resource group, in the billing currency."
  type        = number
  default     = 30
}

variable "budget_start_date" {
  description = "First day of the budget (the first of a month, RFC 3339)."
  type        = string
  default     = "2026-10-01T00:00:00Z"
}
