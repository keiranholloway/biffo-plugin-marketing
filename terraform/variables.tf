variable "project_name" {
  description = "Biffo project name — passed through unchanged from the root config."
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev/staging/prod) — passed through unchanged from the root config."
  type        = string
}

variable "plugin_name" {
  description = "Plugin slug, matching biffo.plugin.json's `name` field and the services/<name>/ directory this plugin was installed into (ADR-0003 section 2). Used to namespace every resource this module creates."
  type        = string
}

variable "handler" {
  description = "Lambda handler entrypoint, e.g. `src.lambda.main.handler` per the plugin repo layout in ADR-0003 section 2."
  type        = string
}

variable "runtime" {
  type    = string
  default = "python3.13"
}

variable "memory_size" {
  type    = number
  default = 512
}

variable "timeout" {
  type    = number
  default = 30
}

variable "enable_vpc_access" {
  description = <<-EOT
    Attach this plugin's Lambda to a VPC. Leave false (the default) — per
    ADR-0002, plugins never access the database directly, only the Core API
    over HTTPS and EventBridge, so VPC attachment is normally unnecessary.
    In NAT-less networking configs (e.g. dev's enable_nat_gateway = false),
    attaching to the VPC would also cut off the outbound internet access
    this Lambda needs to reach the Core API's public endpoint. Only enable
    this if the plugin has a genuine, ADR-0002-compliant reason to reach a
    VPC-only resource (e.g. ElastiCache) — never to reach the database.

    Must be an explicit boolean, not inferred from whether vpc_id is set —
    vpc_id can be only known after apply (e.g. if it were ever passed from
    a VPC created in the same plan), and Terraform's count/for_each in the
    underlying compute module cannot depend on a not-yet-known value.
  EOT
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "VPC to attach this plugin's Lambda to. Only used when enable_vpc_access is true."
  type        = string
  default     = ""
}

variable "private_subnet_ids" {
  description = "Private subnets to place ENIs in. Only used when enable_vpc_access is true."
  type        = list(string)
  default     = []
}

variable "core_api_url" {
  description = "Core API base URL (module.api_gateway.api_endpoint from the root config). Injected as BIFFO_CORE_API_URL — the plugin SDK's BiffoAPIClient reads this env var by default (packages/python-sdk/src/biffo_plugin_sdk/client.py). This is how plugins read/write platform data per ADR-0002 — never a direct DB connection."
  type        = string
  default     = ""
}

variable "core_api_execution_arn" {
  description = <<-EOT
    The Core API Gateway's execution ARN (module.api_gateway.execution_arn from
    the root config, i.e. arn:aws:execute-api:<region>:<acct>:<api-id>). When
    set, this plugin's Lambda role is granted execute-api:Invoke scoped to that
    API's /api/v1/internal/* routes only — the plugin->Core auth mechanism from
    ADR-0009 (IAM SigV4), which the plugin SDK's SignedCoreClient speaks.

    Leave empty (the default) only if the plugin never calls the Core API. It
    is not enough on its own: the Core API independently re-checks the caller
    against BIFFO_SERVICE_PRINCIPAL_ARN_ALLOWLIST, so the plugin's role must
    also be allowlisted there — see this module's README for the exact glob and
    why it is a static string rather than this module's role_arn output.
  EOT
  type        = string
  default     = ""
}

variable "event_bus_name" {
  description = "Name of the shared EventBridge bus (module.events.event_bus_name from the root config). This module subscribes to it — it never creates its own bus, keeping every plugin's events on one platform-wide bus per ADR-0002."
  type        = string
}

variable "event_subscriptions" {
  description = "Events this plugin reacts to, mirroring biffo.plugin.json's `event_subscriptions` array (ADR-0003 section 2), e.g. [{ source = \"biffo.core\", detail_type = \"UserCreated\" }]. Leave empty if the plugin only calls the Core API and never reacts to events — no EventBridge rule is created in that case. Ignored when `subscribe_all` is true."
  type = list(object({
    source      = string
    detail_type = string
  }))
  default = []
}

variable "subscribe_all" {
  description = "Subscribe to every event on the bus (a generic forwarder). When true, `event_subscriptions` is ignored and the rule matches all events, so the plugin — not Terraform — decides what to act on and a new trigger needs no infra change (ADR-0010). Use for engines like the orchestrator that forward everything to the Core API for matching."
  type        = bool
  default     = false
}

variable "environment_variables" {
  description = "Additional environment variables for the plugin's Lambda, merged over BIFFO_CORE_API_URL / BIFFO_PLUGIN_NAME. Do not put database connection details here — plugins never receive them (ADR-0002)."
  type        = map(string)
  default     = {}
}

variable "sqs_kms_key_id" {
  description = "KMS key ID for the DLQ's SQS queue encryption (CKV_AWS_27). Leave empty for AWS-owned key."
  type        = string
  default     = ""
}

variable "cloudwatch_kms_key_id" {
  description = "KMS key ID for CloudWatch log group encryption (CKV_AWS_158). Leave empty for AWS-owned key."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
