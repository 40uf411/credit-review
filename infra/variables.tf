variable "subscription_id" {
  description = "Azure subscription that holds everything."
  type        = string
}

variable "app_resource_group_name" {
  description = "Resource group of the application (it already holds the Foundry resource)."
  type        = string
}

variable "foundry_account_name" {
  description = "The Foundry (Azure AI services) resource created in the portal."
  type        = string
}

variable "deployments" {
  description = "Names of the model deployments created in the portal."
  type = object({
    chat       = string # vision-capable chat model: reads the documents
    mini       = string # its mini variant: comparison, topic guard, question rewriting
    embeddings = string # text-embedding-3-small: the assistant's index
  })
}

variable "tags" {
  description = "Tags on every resource Terraform creates or adopts."
  type        = map(string)
  default = {
    project   = "credit-review"
    owner     = "40uf411"
    env       = "dev"
    managedBy = "terraform"
  }
}
