/**
 * Decide whether a Terraform plan destroys stateful infrastructure (issue #387).
 *
 * ## Why this exists
 *
 * `deploy-infra.yml` runs `terraform apply -auto-approve`. Nobody reads the
 * plan. A user-pool replacement or a database rebuild reads as an ordinary
 * `-/+ resource` line among dozens, and the deploy reports success either way.
 *
 * The exposure is not theoretical. Three attributes on `aws_db_instance` —
 * `identifier`, `db_name`, `username` — are derived from `project_name` and
 * force **replacement**, and in dev and staging `deletion_protection` is false.
 * So renaming the project, a plausible and innocuous-looking edit, plans a
 * destroy of the database, and CI applies it with nobody having seen the plan.
 *
 * A Cognito pool replacement of exactly this shape landed unattended during the
 * 0.50.0 upgrade. It was intended and documented; nothing about the mechanism
 * required it to be.
 *
 * ## Why plain .mjs rather than TypeScript
 *
 * This runs in the **Plan** job, which sets up Terraform and AWS credentials
 * and nothing else — no pnpm, no node_modules. Adding a dependency install to
 * three jobs to run one guard is a poor trade, so the guard has no
 * dependencies and runs on bare node.
 *
 * It is still tested: `cli/src/lib/destructive-plan.test.ts` imports this file
 * directly, the same arrangement `packaged-root-assets.mjs` already uses. The
 * logic has one home, not two.
 */

/**
 * Resource types whose destruction loses data that no re-apply can recreate.
 *
 * Deliberately a small, explicit list rather than a heuristic. A guard that
 * fires on everything gets its acknowledgement trailer added by reflex, and
 * then it protects nothing — so it fires only where a human really should stop.
 *
 * Everything absent from this list is configuration: Terraform rebuilds a
 * Lambda, a security group or an IAM role from this repo. Nothing in this repo
 * contains the rows.
 */
export const STATEFUL_RESOURCE_TYPES = [
  // The data itself.
  'aws_db_instance',
  'aws_rds_cluster',
  'aws_rds_cluster_instance',
  'aws_dynamodb_table',
  'aws_efs_file_system',
  'aws_elasticache_cluster',
  'aws_elasticache_replication_group',
  // Identities. Replacing a pool deletes every user and every `sub` with it,
  // orphaning anything keyed on cognito_sub (ADR-0012).
  'aws_cognito_user_pool',
  // Objects. A bucket destroy takes its contents; for state and media buckets
  // that is the entire point of the bucket.
  'aws_s3_bucket',
  // Delegation. A hosted zone's NS assignment is not in this repo — AWS
  // assigns it at creation and the registrar is told about it out of band.
  // Replacing the zone issues new nameservers, so recovery is a registrar
  // change plus propagation, not a re-apply. The zone's records ARE
  // Terraform-managed and would come back; the delegation is what would not.
  //
  // aws_acm_certificate was considered and rejected: a replacement re-issues
  // and DNS-validates automatically while the zone underneath it stands, so
  // it passes the same test that keeps this list small — Terraform rebuilds
  // it from this repo, unattended.
  'aws_route53_zone',
]

/**
 * Trailer, on its own line in a commit message, authorising this plan's
 * destructive changes. Mirrors `Core-Divergence:` — deliberate, and recorded in
 * history rather than argued about afterwards.
 */
export const DESTROY_TRAILER = 'Infra-Destroy'

/**
 * Extract the reason from an `Infra-Destroy:` trailer, or null.
 *
 * Anchored per line, and comment lines are dropped, for the same reason
 * `Core-Divergence:` is: the words appearing inside prose are not an
 * authorisation to delete a database.
 */
export function parseDestroyTrailer(commitMessage) {
  const body = String(commitMessage || '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('#'))
    .join('\n')
  const match = /^Infra-Destroy:[ \t]*(\S.*?)[ \t]*$/m.exec(body)
  return match ? match[1] : null
}

/**
 * Classify a `terraform show -json` plan.
 *
 * `replace` counts as destruction: Terraform encodes it as
 * `["delete","create"]` (or the reverse under create_before_destroy), and a
 * replacement destroys the original exactly as surely as a bare delete. Most
 * incidents in this class ARE replacements — nobody runs `terraform destroy` by
 * accident; they change an attribute that forces one, far from the resource.
 */
export function checkDestructivePlan(
  plan,
  commitMessage = '',
  statefulTypes = STATEFUL_RESOURCE_TYPES,
) {
  const types = new Set(statefulTypes)
  const destructive = []

  for (const change of (plan && plan.resource_changes) || []) {
    const actions = (change.change && change.change.actions) || []
    if (!actions.includes('delete')) continue
    if (!types.has(change.type)) continue
    destructive.push({
      address: change.address,
      type: change.type,
      replacement: actions.includes('create'),
    })
  }

  const acknowledgedReason = parseDestroyTrailer(commitMessage)
  return {
    destructive,
    acknowledgedReason,
    blocked: destructive.length > 0 && acknowledgedReason === null,
  }
}
