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
    # Waiting for quota (DataZoneStandard in Sweden Central is 0 today). Uncomment once it is granted.
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
