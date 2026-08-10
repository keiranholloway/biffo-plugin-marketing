#!/usr/bin/env node
/**
 * CI runner for the destructive-plan guard (issue #387).
 *
 * Reads a `terraform show -json` plan and refuses it when it would destroy
 * stateful infrastructure without an `Infra-Destroy:` trailer authorising it.
 *
 * Runs in the **Plan** job, before Apply, so a refusal costs nothing: no
 * infrastructure has been touched when it fires. Apply runs with
 * `--auto-approve`, so this is the only point at which a human decision can
 * still be required.
 *
 * No dependencies, so it runs on bare node without a pnpm install — see
 * destructive-plan.mjs for why.
 *
 * Usage: node check-destructive-plan.mjs <plan.json>
 */

import { execFileSync } from 'node:child_process'
import { appendFileSync, existsSync, readFileSync } from 'node:fs'
import { DESTROY_TRAILER, checkDestructivePlan } from './destructive-plan.mjs'

const BOLD = '[1m'
const DIM = '[2m'
const RED = '[31m'
const OFF = '[0m'

/**
 * Commit messages this deploy is carrying.
 *
 * A push build has no PR body, so the commit message is the only place an
 * author can speak to CI. `github.event.before` bounds it to what this push
 * actually added; on a first push or a force-push it is all-zeroes, and then
 * the tip commit alone is the honest answer rather than the entire history —
 * an `Infra-Destroy:` trailer from six months ago must not authorise today.
 */
function pushCommitMessages() {
  const before = process.env.GITHUB_EVENT_BEFORE
  const head = process.env.GITHUB_SHA || 'HEAD'
  const args =
    before && !/^0+$/.test(before)
      ? ['log', '--format=%B', `${before}..${head}`]
      : ['log', '--format=%B', '-1']
  try {
    return execFileSync('git', args, { encoding: 'utf8' })
  } catch {
    // A shallow clone may not contain `before`. Fall back to the tip rather
    // than treating an unreadable range as "no trailer", which would block a
    // legitimately authorised deploy.
    try {
      return execFileSync('git', ['log', '--format=%B', '-1'], { encoding: 'utf8' })
    } catch {
      return ''
    }
  }
}

function main() {
  const planPath = process.argv[2]
  if (!planPath) {
    console.error('Usage: check-destructive-plan.mjs <plan.json>')
    process.exit(2)
  }
  if (!existsSync(planPath)) {
    // Fail closed. A missing plan means the guard cannot see what is about to
    // be applied, which is not the same as there being nothing to see.
    console.error(`::error::Plan file ${planPath} not found — refusing to apply unexamined.`)
    process.exit(2)
  }

  let plan
  try {
    plan = JSON.parse(readFileSync(planPath, 'utf8'))
  } catch (err) {
    console.error(`::error::Could not parse ${planPath}: ${err.message}`)
    process.exit(2)
  }

  const result = checkDestructivePlan(plan, pushCommitMessages())

  if (result.destructive.length === 0) {
    console.log('✓ destructive-plan guard: no stateful resource is destroyed by this plan.')
    return
  }

  const lines = result.destructive.map(
    (c) => `    ${c.address} ${DIM}(${c.replacement ? 'replaced' : 'destroyed'})${OFF}`,
  )

  if (!result.blocked) {
    console.log(
      `⚠ destructive-plan guard: allowed by ${DESTROY_TRAILER}: ${result.acknowledgedReason}\n` +
        lines.join('\n'),
    )
    summarise(result.destructive, result.acknowledgedReason)
    return
  }

  console.error(`
${RED}${BOLD}✗ This plan destroys stateful infrastructure.${OFF}

${lines.join('\n')}

${BOLD}Why this is blocked${OFF}
  These resources hold data no re-apply can recreate — rows, users, objects.
  Everything else Terraform rebuilds from this repo; these it cannot, because
  nothing in this repo contains the data. Apply runs with --auto-approve, so
  without this check the destroy would happen with nobody having seen the plan.

  A ${BOLD}replacement${OFF} destroys the original just as surely as a delete, and most
  incidents of this kind are replacements: an attribute that forces one changed
  somewhere far from the resource itself.

${BOLD}If it is deliberate${OFF}
  Say so in the commit message and this passes:

    ${DIM}${DESTROY_TRAILER}: <what is destroyed, and why that is acceptable>${OFF}

  Take a backup first if the data matters — in dev and staging there may be no
  final snapshot to fall back on.
`)
  process.exit(1)
}

/** Record an authorised destroy in the job summary, so it is visible without
 * reading the log — the log being exactly what nobody reads. */
function summarise(destructive, reason) {
  const path = process.env.GITHUB_STEP_SUMMARY
  if (!path) return
  const rows = destructive.map(
    (c) => `- \`${c.address}\` (${c.replacement ? 'replaced' : 'destroyed'})`,
  )
  appendFileSync(
    path,
    `### ⚠️ Destructive plan authorised\n\n${destructive.length} stateful resource(s) will be destroyed:\n\n` +
      `${rows.join('\n')}\n\n**${DESTROY_TRAILER}:** ${reason || ''}\n`,
  )
}

main()
