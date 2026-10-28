# One page for traffic and model usage: Application Insights → Workbooks → "Credit review: traffic and models".
# Every query reads spans and logs the apps already send (credit_review/telemetry.py); none of them holds a
# document, a question or an answer.

locals {
  workbook_queries = [
    {
      title         = "Traffic per app (last 24 h)"
      query         = <<-KQL
        requests
        | summarize requests = count(), failed = countif(success == false), p95_ms = round(percentile(duration, 95))
            by app = cloud_RoleName
        | order by requests desc
      KQL
      visualization = "table"
    },
    {
      title         = "Requests per app, every 15 minutes"
      query         = <<-KQL
        requests
        | summarize requests = count() by app = cloud_RoleName, bin(timestamp, 15m)
      KQL
      visualization = "timechart"
    },
    {
      title         = "Model calls: tokens, prompt cache, response cache and cost"
      query         = <<-KQL
        dependencies
        | where name startswith "llm "
        | extend input = toint(customDimensions["gen_ai.usage.input_tokens"]),
                 prompt_cached = toint(customDimensions["gen_ai.usage.cache_read.input_tokens"]),
                 output = toint(customDimensions["gen_ai.usage.output_tokens"]),
                 cost = todouble(customDimensions["credit_review.cost"]),
                 answered_from_cache = tostring(customDimensions["credit_review.cached"]) == "true"
        | summarize calls = count(), from_response_cache = countif(answered_from_cache),
                    input_tokens = sum(input), prompt_cached_tokens = sum(prompt_cached), output_tokens = sum(output),
                    cost_usd = round(sum(cost), 4), p95_ms = round(percentile(duration, 95))
            by app = cloud_RoleName, call = name, model = tostring(customDimensions["gen_ai.request.model"])
        | extend prompt_cache_share = iff(input_tokens > 0, round(100.0 * prompt_cached_tokens / input_tokens, 1), 0.0)
        | order by cost_usd desc
      KQL
      visualization = "table"
    },
    {
      title         = "Tokens per hour"
      query         = <<-KQL
        dependencies
        | where name startswith "llm " and tostring(customDimensions["credit_review.cached"]) != "true"
        | summarize input = sum(toint(customDimensions["gen_ai.usage.input_tokens"])),
                    output = sum(toint(customDimensions["gen_ai.usage.output_tokens"])) by bin(timestamp, 1h)
      KQL
      visualization = "timechart"
    },
    {
      title         = "Assistant turns by outcome (ok, or the refusal code)"
      query         = <<-KQL
        dependencies
        | where name == "chat turn"
        | summarize turns = count() by outcome = tostring(customDimensions["assistant.code"])
        | order by turns desc
      KQL
      visualization = "piechart"
    },
    {
      title         = "Withheld answers: which check failed, on which chunk"
      query         = <<-KQL
        dependencies
        | where name == "chat turn" and isnotempty(customDimensions["assistant.withheld"])
        | project timestamp, detail = tostring(customDimensions["assistant.withheld"]),
                  prompt = tostring(customDimensions["assistant.prompt_version"])
        | order by timestamp desc
        | take 50
      KQL
      visualization = "table"
    },
  ]

  workbook = {
    version = "Notebook/1.0"
    items = concat(
      [{
        type    = 1
        name    = "intro"
        content = { json = "## Traffic and model usage\nRequests per app, model calls with tokens, prompt-cache share and cost, and the assistant's outcomes. Time range: last 24 hours." }
      }],
      [for i, q in local.workbook_queries : {
        type = 3
        name = "query-${i}"
        content = {
          version       = "KqlItem/1.0"
          title         = q.title
          query         = q.query
          size          = 0
          queryType     = 0
          resourceType  = "microsoft.insights/components"
          visualization = q.visualization
          timeContext   = { durationMs = 86400000 }
        }
      }],
      [{
        type = 3
        name = "foundry-requests"
        content = {
          version                 = "KqlItem/1.0"
          title                   = "Foundry and Prompt Shields requests by result (Azure's own logs)"
          query                   = "AzureDiagnostics\n| where ResourceProvider == \"MICROSOFT.COGNITIVESERVICES\"\n| summarize requests = count(), p95_ms = round(percentile(DurationMs, 95)) by resource = Resource, operation = OperationName, result = ResultSignature\n| order by requests desc"
          size                    = 0
          queryType               = 0
          resourceType            = "microsoft.operationalinsights/workspaces"
          crossComponentResources = [azurerm_log_analytics_workspace.this.id]
          visualization           = "table"
          timeContext             = { durationMs = 86400000 }
        }
      }],
    )
    fallbackResourceIds = [azurerm_application_insights.this.id]
  }
}

resource "azurerm_application_insights_workbook" "traffic" {
  name                = uuidv5("url", "https://github.com/40uf411/credit-review/workbooks/traffic") # stable id
  resource_group_name = data.azurerm_resource_group.app.name
  location            = var.location
  display_name        = "Credit review: traffic and models"
  source_id           = lower(azurerm_application_insights.this.id)
  category            = "workbook"
  data_json           = jsonencode(local.workbook)
  tags                = var.tags
}
