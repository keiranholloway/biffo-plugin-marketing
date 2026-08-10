# Template Terraform module for a Biffo plugin (ADR-0003 chunk 12 / issues #25, #201).
#
# Copy this directory to modules/plugins/<name>/ inside a plugin repo (as
# terraform/, per ADR-0003 section 2's plugin repo layout) and adjust as
# needed. `biffo plugin install <name>@<minor>` copies that terraform/
# directory into the user's monorepo at modules/plugins/<name>/ and then
# generates the instantiating `module "plugin_<name>"` block — gated on
# `enabled_plugins` — into infra/environments/<env>/plugins.generated.tf. That
# file is CLI-owned and regenerated on every install/uninstall; main.tf is
# never edited. See infra/environments/dev/README.md's "Adding a plugin".
#
# This module deliberately wraps two existing modules rather than
# reimplementing Lambda/IAM/EventBridge from scratch:
#   - modules/cloud/aws/compute — the plugin's Lambda function, with the
#     same DLQ/logging/tracing/least-privilege IAM baseline every Biffo
#     function gets.
#   - modules/cloud/aws/events (via the shared event_bus_name passed in) —
#     this module adds only the subscription rule/target/permission a
#     plugin needs to react to events on the bus the root config already
#     owns. No new bus is created.
#
# What this module does NOT do, per ADR-0002 ("no DB clients outside
# services/api/", "microservices call the API via HTTP and react to
# EventBridge events"):
#   - It never creates a database, and never receives
#     db_credentials_secret_arn — that variable is wired to the Core API's
#     Lambda only (infra/environments/dev/main.tf's module "core_api" block).
#     A plugin that needs platform data calls the Core API over HTTPS
#     (BIFFO_CORE_API_URL, see variables.tf) using the plugin SDK's
#     BiffoAPIClient, exactly like any other API consumer.
#   - It never attaches to the VPC unless var.enable_vpc_access is set to
#     true — see variables.tf's enable_vpc_access description for why.
#
# Loose coupling: this module must not reference other plugin modules or
# their resources. Each plugin is instantiated independently by the root
# config; nothing here should assume any other plugin is installed.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

locals {
  name_prefix   = "${var.project_name}-${var.environment}"
  function_name = "${local.name_prefix}-plugin-${var.plugin_name}"
  # A rule is created when the plugin subscribes to specific events, or when it
  # is a generic forwarder that reacts to every event (subscribe_all).
  has_subscriptions = var.subscribe_all || length(var.event_subscriptions) > 0
  # Grant Core API access only when the root config told us which API to scope
  # it to. Empty (the default) => the plugin never calls Core, so no grant.
  grants_core_api_access = var.core_api_execution_arn != ""
}

# Compute — the plugin's Lambda function.
module "function" {
  source = "../../cloud/aws/compute"

  project_name       = var.project_name
  environment        = var.environment
  function_name      = "plugin-${var.plugin_name}"
  handler            = var.handler
  runtime            = var.runtime
  memory_size        = var.memory_size
  timeout            = var.timeout
  enable_vpc_access  = var.enable_vpc_access
  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids
  event_bus_name     = var.event_bus_name

  # No db_credentials_secret_arn — see the ADR-0002 note above.
  environment_variables = merge(
    {
      BIFFO_CORE_API_URL = var.core_api_url
      BIFFO_PLUGIN_NAME  = var.plugin_name
    },
    var.environment_variables,
  )

  sqs_kms_key_id        = var.sqs_kms_key_id
  cloudwatch_kms_key_id = var.cloudwatch_kms_key_id
  tags                  = var.tags
}

# Events — subscribe the plugin's Lambda to its declared event_subscriptions
# on the shared bus. Each subscription is matched as its own source +
# detail-type pair via `$or`, rather than independent `source`/`detail-type`
# arrays, to avoid EventBridge matching the cross product of unrelated
# source/detail-type combinations when a plugin subscribes to more than one
# event.
resource "aws_cloudwatch_event_rule" "subscription" {
  count          = local.has_subscriptions ? 1 : 0
  name           = "${local.function_name}-events"
  description    = "Routes subscribed events to the ${var.plugin_name} plugin"
  event_bus_name = var.event_bus_name

  # subscribe_all → match every event on the bus (a generic forwarder; the plugin
  # decides what to do, so new triggers need no Terraform change — ADR-0010). Else
  # a single subscription uses a flat pattern and two or more are OR-ed: EventBridge
  # rejects a `$or` with fewer than 2 elements ("There must have at least 2 Objects
  # in $or relationship"), so the single-subscription case must not use it.
  # jsonencode is applied inside each branch so the conditional's arms are all
  # strings — a `cond ? {a} : {b}` on differently-shaped objects is an "Inconsistent
  # conditional result types" error at apply (validate misses it).
  event_pattern = var.subscribe_all ? jsonencode({
    source = [{ prefix = "" }]
    }) : length(var.event_subscriptions) == 1 ? jsonencode({
    source        = [var.event_subscriptions[0].source]
    "detail-type" = [var.event_subscriptions[0].detail_type]
    }) : jsonencode({
    "$or" = [
      for s in var.event_subscriptions : {
        source        = [s.source]
        "detail-type" = [s.detail_type]
      }
    ]
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "subscription" {
  count          = local.has_subscriptions ? 1 : 0
  rule           = aws_cloudwatch_event_rule.subscription[0].name
  event_bus_name = var.event_bus_name
  target_id      = "${var.plugin_name}-lambda"
  arn            = module.function.function_arn
}

resource "aws_lambda_permission" "subscription" {
  count         = local.has_subscriptions ? 1 : 0
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.function.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.subscription[0].arn
}

# Core API access (ADR-0009) — the plugin->Core auth path.
#
# ADR-0002 forbids this Lambda from touching the database, so anything it needs
# from the platform it gets over HTTPS from the Core API. The Core API's
# internal routes (/api/v1/internal/*) are IAM-authorized, not Cognito-JWT, so
# the plugin authenticates by SigV4-signing with this Lambda role — see
# biffo_plugin_sdk.SignedCoreClient, which BiffoPluginBase uses by default. No
# bearer token, no shared secret, nothing to rotate.
#
# Scoped to the /api/v1/internal/* prefix on one API, never the whole API.
data "aws_iam_policy_document" "core_api" {
  count = local.grants_core_api_access ? 1 : 0

  statement {
    sid       = "InvokeCoreInternalApi"
    effect    = "Allow"
    actions   = ["execute-api:Invoke"]
    resources = ["${var.core_api_execution_arn}/*/*/api/v1/internal/*"]
  }
}

resource "aws_iam_role_policy" "core_api" {
  count = local.grants_core_api_access ? 1 : 0
  name  = "${local.function_name}-core-api"
  # compute exposes the role via its ARN; derive the role name (last ARN
  # segment) since aws_iam_role_policy wants the name, not the ARN.
  role   = element(split("/", module.function.role_arn), length(split("/", module.function.role_arn)) - 1)
  policy = data.aws_iam_policy_document.core_api[0].json
}
