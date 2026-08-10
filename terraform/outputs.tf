output "function_arn" {
  description = "Lambda function ARN — aggregate this in the root config's plugin outputs."
  value       = module.function.function_arn
}

output "function_name" {
  value = module.function.function_name
}

# The plugin Lambda's execution role ARN. Useful for auditing, but note this is
# deliberately NOT how the role reaches the Core API's
# BIFFO_SERVICE_PRINCIPAL_ARN_ALLOWLIST — wiring this output into the core_api
# module would create the dependency cycle core_api -> api_gateway -> plugin ->
# core_api (issue #201). Allowlist the predictable assumed-role glob instead;
# see README.md.
output "role_arn" {
  value = module.function.role_arn
}

output "role_name" {
  description = "The plugin Lambda's execution role NAME — the value to interpolate into the Core API's BIFFO_SERVICE_PRINCIPAL_ARN_ALLOWLIST glob (arn:aws:sts::<acct>:assumed-role/<role-name>/*)."
  value       = element(split("/", module.function.role_arn), length(split("/", module.function.role_arn)) - 1)
}

output "dlq_arn" {
  description = "Dead letter queue ARN for failed invocations (both direct and EventBridge-triggered)."
  value       = module.function.dlq_arn
}

output "event_rule_arn" {
  description = "EventBridge rule ARN, or null when the plugin declares no event_subscriptions."
  value       = local.has_subscriptions ? aws_cloudwatch_event_rule.subscription[0].arn : null
}
