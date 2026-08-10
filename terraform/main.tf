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

  # Where this deployment's public base URL is read from.
  #
  # DERIVED, not passed in, and that is the point. `biffo plugin install`
  # writes plugins.generated.tf from a fixed argument list — project_name,
  # environment, plugin_name, handler, event_bus_name, core_api_url,
  # core_api_execution_arn, tags — and that file is regenerated in full on the
  # next install, so an installed plugin has NO channel for instance-specific
  # configuration. (Core plugins escape this only because they are wired by
  # hand in plugins.core.tf; that is how agent-runtime gets its OpenRouter
  # key.) Filed upstream as keiranholloway/biffo-template#1456.
  #
  # A conventional path built from values the module already receives needs no
  # such channel: Terraform grants read access to it, and an operator sets the
  # value once with `aws ssm put-parameter`. Same shape as agent-runtime's
  # credential parameters, minus the plumbing this module cannot have.
  public_base_url_parameter = "/${var.project_name}/${var.environment}/${var.plugin_name}/public-base-url"

  ssm_parameter_prefix = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter"
}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

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
      # The parameter NAME, not the value. Terraform never reads the value, so
      # it never lands in state — state is not a secret store, and a base URL
      # that changes should not need an apply to take effect.
      BIFFO_PUBLIC_BASE_URL_PARAMETER = local.public_base_url_parameter
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


# ---------------------------------------------------------------------------
# Public base URL — read at runtime from one SSM parameter.
#
# `ssm:GetParameter` on exactly one path, and `kms:Decrypt` conditioned on the
# call going via SSM, so the grant cannot be used for anything but fetching
# this parameter. Copied deliberately from agent-runtime's credential grant,
# including the ViaService condition.
#
# The parameter does not have to exist for this to apply. A deployment where
# nobody has set it yet simply has a plugin that refuses to mint links and says
# why (503), which is the honest behaviour — a link minted against a missing
# base URL would be published as a relative path.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "public_base_url" {
  statement {
    sid       = "ReadPublicBaseUrlParameter"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["${local.ssm_parameter_prefix}${local.public_base_url_parameter}"]
  }

  statement {
    sid       = "DecryptPublicBaseUrlParameter"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "public_base_url" {
  name   = "${local.function_name}-public-base-url"
  role   = element(split("/", module.function.role_arn), length(split("/", module.function.role_arn)) - 1)
  policy = data.aws_iam_policy_document.public_base_url.json
}
