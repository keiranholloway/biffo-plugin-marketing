#!/usr/bin/env node
/**
 * Two guards over a PR's closing keywords, asking different questions — and,
 * since #1334, applied to every document GitHub actually honours, not just
 * the PR body.
 *
 * 1. Refuse `Closes #N` on a change whose behaviour only shows up once
 *    deployed — a path-scoped check, documented immediately below.
 * 2. Refuse a NEGATED closing keyword anywhere, on any path — see
 *    `negatedClosingReferences`. GitHub's linker has no concept of negation,
 *    so `Does not close #N` closes #N.
 *
 * ── Three documents, not one (#1334, #1362) ──────────────────────────────
 *
 * GitHub does not read only the PR body. A closing keyword in the PR body
 * shows up as a live "closes #N" link while the PR is open; a closing
 * keyword in a **commit message** is honoured too — and for a squash merge,
 * this repo's default constructs the squash commit's message from the
 * individual commits, not from the PR body. #1332 was opened with
 * `Closes #1331` and Release Guards correctly refused it (a workflow-only
 * change, deploy-only path). The PR body was corrected to `Refs #1331`, the
 * guard re-ran reading `PR_BODY`, and it passed — because the guard had only
 * ever read the body. The first commit's message still said `Closes #1331`,
 * that text reached the squash-merge commit unchanged, and #1331 closed the
 * instant the PR merged. The guard was right about what it read; GitHub read
 * something else.
 *
 * So every check in this file runs against **all** of: the PR body, the PR
 * title, and every commit's message (`messageHeadline` and `messageBody`
 * both — a keyword can sit in either). One finding in any one of them is
 * enough to trip the guard; see `documentsFor` and `assess`.
 *
 * ── 1. Closing keywords on deploy-only paths ─────────────────────────────
 *
 * GitHub closes an issue the moment a PR body carrying a closing keyword is
 * merged. For most changes that is right and convenient. For a change whose
 * correctness cannot be observed until it is running somewhere, it closes the
 * issue at the exact moment the least is known about it — the suite is green,
 * nothing has been deployed, and nobody has looked.
 *
 * This is not a theoretical tidiness rule. It has cost this estate repeatedly:
 *
 *   - #275: portal navigation landing on the raw RSC payload was diagnosed,
 *     "fixed", shipped with a drift guard and closed. On a wrong cause. It
 *     survived a teardown and redeploy before a human clicked the link.
 *   - tabsii-platform#429/#436: two independent ORM/DDL column mismatches,
 *     each green in a lane that builds its schema from the same models it is
 *     checking, each found by a 500 on a live click-through.
 *   - tabsii-platform#511 (2026-08-02): `Closes #511` auto-closed it on merge,
 *     ten minutes before the deploy that proved anything. The evidence had to
 *     be added afterwards, as a comment on an already-closed issue.
 *
 * The rule this encodes is AGENTS.md's, verbatim: *do not close an issue you
 * have not seen fixed*. Use `Refs #N`, verify against reality, then close by
 * hand with what you saw.
 *
 * ## What counts as "only shows up once deployed"
 *
 * A path list, deliberately short. Every entry is somewhere this estate has
 * actually been bitten, not everywhere a bug could hide:
 *
 *   - `infra/`, `modules/cloud/` — Terraform. `terraform validate` says the
 *     HCL parses, never that the deployed resource behaves.
 *   - `.github/workflows/` — a workflow is only really run by running it.
 *   - `db/imports/` — applied by the importer at deploy time, against a real
 *     database, in an order no unit test reproduces.
 *   - `apps/portal/` — auth flows, client-side routing and CDN behaviour, the
 *     exact trio behind #275, #1104 and #1106.
 *   - `apps/frontend/` — the same trio, under the name a SIBLING gives it.
 *     One list serves both flavours rather than a per-flavour copy: a sibling
 *     has no `apps/portal/` and this repo has no `apps/frontend/`, so each
 *     entry is simply inert where it does not apply. Two copies of this list
 *     would be two places for it to drift, which is the defect class this
 *     estate has paid for most often.
 *
 * Application code, the CLI, and `services/api/src/` are all absent on
 * purpose. A pure function with a failing-first test is genuinely proven by
 * that test, and a guard that fires on every PR teaches people to bypass it.
 *
 * ## The escape hatch, and why it is a trailer
 *
 * A `Verified-on-deploy:` trailer in the PR body allows the closing keyword.
 * It exists for the honest case — a fix already confirmed on a running
 * environment, being landed after the fact — and it asks for the evidence in
 * the same breath, so the claim lands in the PR body where a reviewer sees it
 * rather than in someone's memory.
 *
 * ## Reading the body live (#1174)
 *
 * In CI the body is read live via the GitHub API (`resolveBody`,
 * `fetchPrBodyViaGh`), not from `github.event.pull_request.body`. That value
 * is frozen at the moment the `pull_request` event fired, so both of this
 * guard's own documented remedies — edit the body to add `Refs #N`, or add a
 * `Verified-on-deploy:` line — were unable to ever turn the check green: an
 * edit does not re-trigger CI, and a re-run of the job replays the same
 * stale payload. Verified stale on #1172. See `resolveBody` for the fallback
 * to a direct `PR_BODY` (local runs and every test in this suite) and why an
 * unreadable live body fails the guard rather than passing it.
 */

/** Closing keywords GitHub actually acts on, per its own documentation. */
const CLOSING_KEYWORDS = [
  'close',
  'closes',
  'closed',
  'fix',
  'fixes',
  'fixed',
  'resolve',
  'resolves',
  'resolved',
]

export const VERIFIED_TRAILER = 'Verified-on-deploy:'

/** Paths whose behaviour a green suite does not evidence. See the module docstring. */
export const DEPLOY_ONLY_PREFIXES = [
  'infra/',
  'modules/cloud/',
  '.github/workflows/',
  'db/imports/',
  'apps/portal/',
  'apps/frontend/',
]

/** An issue reference GitHub linkifies: `#12` or `owner/repo#12`. */
const REFERENCE = '(?:[\\w.-]+/[\\w.-]+)?#\\d+'

/**
 * Blank out fenced code blocks and inline code spans, preserving line count.
 *
 * Not merely a courtesy: GitHub does not linkify `#12` inside backticks, so it
 * does not close anything there either. Matching there would make these guards
 * STRICTER than the behaviour they exist to model — and it is how the
 * deploy-path guard first failed its own PR, whose body necessarily quotes the
 * very pattern it forbids. The negation guard has the same problem in a
 * sharper form: its failure message, and any PR discussing it, must be able to
 * quote `does not close #N` without tripping it.
 *
 * Every non-newline character becomes a space rather than vanishing, so a
 * match's offset still maps to the line the author wrote — that is what lets
 * `negatedClosingReferences` name the offending line.
 */
export function stripCode(body) {
  const blank = (m) => m.replace(/[^\n]/g, ' ')
  return body.replace(/```[\s\S]*?```/g, blank).replace(/`[^`\n]*`/g, blank)
}

/**
 * The issue references a body would close on merge.
 *
 * Matches `Closes #12`, `fixes owner/repo#12` and the `Closes: #12` colon
 * form. Ignores keywords inside code — see `stripCode`.
 */
export function closingReferences(body) {
  if (!body) return []
  const withoutCode = stripCode(body)
  const pattern = new RegExp(`\\b(${CLOSING_KEYWORDS.join('|')})\\b:?\\s+(${REFERENCE})`, 'gi')
  return [...withoutCode.matchAll(pattern)].map((m) => m[2])
}

/**
 * ── 2. Negated closing keywords, on every path ───────────────────────────
 *
 * A sentence that says a PR does NOT close an issue still closes it. GitHub's
 * linker matches `close #N` and acts; it has no concept of the word before it.
 *
 * Four occurrences, three of them "fixed" by writing the rule down again:
 *
 *   - tabsii-platform#76 — the original.
 *   - tabsii-crm#133 — `tabsii-crm#141`'s body carried
 *     `## Scope note — this PR alone does not close #133`. Its squash commit
 *     carried only `Refs #133`. The issue closed on merge anyway. The lesson
 *     recorded then: *keeping a denial out of the commit is not sufficient —
 *     GitHub's linker reads the PR description text on its own.*
 *   - #1238 / #1021 (2026-08-03) — `- **Does not close #1021.**` in the body,
 *     `Refs #1021` in the commit, #1021 closed by the squash-merge.
 *
 * The recorded fix each time was a *practice*: "never write a closing keyword
 * in prose". Three occurrences produced a rule and no mechanism, and the
 * fourth was authored with that rule available. That is the argument for a
 * guard rather than another note (#1245).
 *
 * ## Why this fires on every path, unlike the check above
 *
 * The deploy-path check asks "is this closing an issue nothing has evidenced
 * yet?", so what the PR touches is the whole question. This one asks "does the
 * author's own prose contradict what GitHub is about to do?", which has
 * nothing to do with the diff. #1238 touched `scripts/` and `cli/` and the
 * deploy-path check correctly stayed silent while the issue closed anyway.
 *
 * ## Why the detection is safe to make blocking
 *
 * It is not inferring intent. It requires a negation *immediately* before a
 * closing keyword *and* a linkified issue reference — there is no reading of
 * `does not close #N` in which the author wants #N closed. Ordinary prose
 * survives it: `the fail-open the tool exists to close` has no negation and no
 * reference, and `this does not close it` has no `#N`, so GitHub would not
 * close anything there and neither does this fire.
 */
const NEGATIONS = [
  // `not` covers "does not", "will not", "should not", "is not", "did not".
  '\\bnot',
  '\\bnever',
  '\\bwithout',
  '\\bcannot',
  // The contracted forms, matched as a suffix so one alternative covers
  // don't / doesn't / didn't / won't / can't / isn't / shouldn't.
  "n['’]t",
]

/**
 * The negated closing references in a body, each with the line that carries
 * it — a guard that says only "no" gets worked around.
 *
 * Returns `[{ reference, line, lineNumber }]`, in body order.
 */
export function negatedClosingReferences(body) {
  if (!body) return []
  const text = stripCode(body)
  const authored = body.split('\n')
  const pattern = new RegExp(
    `(?:${NEGATIONS.join('|')})\\s+(?:${CLOSING_KEYWORDS.join('|')})\\b:?\\s+(${REFERENCE})`,
    'gi',
  )
  return [...text.matchAll(pattern)].map((m) => {
    // `stripCode` preserves newlines, so an offset into the blanked text still
    // maps to the line the author actually wrote.
    const lineNumber = text.slice(0, m.index).split('\n').length
    return {
      reference: m[1],
      lineNumber,
      line: (authored[lineNumber - 1] ?? m[0]).trim(),
    }
  })
}

/** Whether the author has claimed, in the body, to have verified this on a
 * deployed environment. Requires something after the colon: a bare trailer is
 * a box tick, not evidence. */
export function hasVerifiedTrailer(body) {
  if (!body) return false
  const line = body
    .split('\n')
    .find((l) => l.trim().toLowerCase().startsWith(VERIFIED_TRAILER.toLowerCase()))
  if (line === undefined) return false
  return line.slice(line.indexOf(':') + 1).trim().length > 0
}

/** The changed paths that fall under a deploy-only prefix. */
export function deployOnlyPaths(changedFiles) {
  return changedFiles.filter((f) => DEPLOY_ONLY_PREFIXES.some((p) => f.startsWith(p)))
}

/**
 * Every document GitHub honours a closing keyword in, tagged with a
 * human-readable source so a failure can say exactly where it found the
 * keyword (#1334: knowing only "the body passed" is what let the real bug
 * through — the body WAS clean, the commit message was not).
 *
 * `commits` is the shape `gh pr view --json commits` returns: an array of
 * `{ messageHeadline, messageBody }`. Both are scanned — a keyword can sit
 * in either, and #1334's own repro had it in the headline.
 */
export function documentsFor({ body, title, commits }) {
  const docs = [{ source: 'the PR body', text: body }]
  if (title) docs.push({ source: 'the PR title', text: title })
  const list = commits ?? []
  list.forEach((commit, i) => {
    const label = list.length === 1 ? 'the commit message' : `commit ${i + 1}`
    if (commit?.messageHeadline) {
      docs.push({ source: `${label} (subject)`, text: commit.messageHeadline })
    }
    if (commit?.messageBody) {
      docs.push({ source: `${label} (body)`, text: commit.messageBody })
    }
  })
  return docs
}

/**
 * The whole decision, pure so it is testable without a repo or a PR.
 *
 * Returns `{ ok }` on a pass, or a failure carrying `kind` plus exactly what
 * tripped it — a guard that says only "no" gets worked around.
 *
 * `title` and `commits` are optional so every existing body-only caller (and
 * test) keeps working unchanged — see `documentsFor`.
 *
 * The negation check runs FIRST and ignores `changedFiles` entirely. It is not
 * a special case of the deploy-path check: a `Verified-on-deploy:` trailer
 * cannot excuse it either, because the author is not claiming the issue is
 * verified, they are saying it is not being closed at all.
 */
export function assess({ body, title, commits, changedFiles }) {
  const docs = documentsFor({ body, title, commits })

  const negated = docs.flatMap((doc) =>
    negatedClosingReferences(doc.text).map((n) => ({ ...n, source: doc.source })),
  )
  if (negated.length > 0) return { ok: false, kind: 'negated-keyword', negated }

  const hits = docs
    .map((doc) => ({ source: doc.source, references: closingReferences(doc.text) }))
    .filter((h) => h.references.length > 0)
  if (hits.length === 0) return { ok: true, reason: 'no-closing-keyword' }

  const paths = deployOnlyPaths(changedFiles)
  if (paths.length === 0) return { ok: true, reason: 'no-deploy-only-paths' }

  if (hasVerifiedTrailer(body)) return { ok: true, reason: 'verified-trailer' }

  const references = [...new Set(hits.flatMap((h) => h.references))]
  return { ok: false, kind: 'deploy-only-path', references, paths, hits }
}

export function formatFailure(result) {
  return result.kind === 'negated-keyword'
    ? formatNegatedFailure(result)
    : formatDeployOnlyFailure(result)
}

function formatNegatedFailure({ negated }) {
  const refs = [...new Set(negated.map((n) => n.reference))]
  return [
    `This PR says it does NOT close ${refs.join(', ')}, and GitHub will`,
    `close ${refs.length === 1 ? 'it' : 'them'} anyway on merge. Its linker matches the keyword and the`,
    'issue reference; it has no concept of the word "not" in front of them.',
    '',
    ...negated.map((n) => `  ${n.source}, line ${n.lineNumber}: ${n.line}`),
    '',
    'This has now happened four times (tabsii-platform#76, tabsii-crm#133,',
    '#1021 via #1238 — see #1245). Keeping the denial out of the commit',
    'message is not enough: GitHub reads the PR description on its own — and',
    '(#1334) a commit message on its own, independent of the body.',
    '',
    'Rewrite the line so no closing keyword sits in front of the reference:',
    ...refs.map((r) => `  - \`Refs ${r}\`, or "leaves ${r} open"`),
    '',
    'If the offending text is in the PR body or title, edit it — this guard',
    'reads both live, so an edit alone turns the check green with no new',
    'commit (#1174, #1189). If it is in a COMMIT message, the commit itself',
    'must change (amend/reword and force-push) — the guard reads the commits',
    'live too, but the commit message that will actually reach the merge',
    'cannot be edited from the PR page.',
  ].join('\n')
}

function formatDeployOnlyFailure({ references, paths, hits }) {
  const shown = paths.slice(0, 10)
  const more = paths.length - shown.length
  return [
    `This PR would close ${references.join(', ')} on merge — found in:`,
    '',
    ...(hits ?? []).map((h) => `  - ${h.source}: ${h.references.join(', ')}`),
    '',
    'and it changes paths whose behaviour a green suite does not evidence:',
    '',
    ...shown.map((p) => `  - ${p}`),
    ...(more > 0 ? [`  …and ${more} more`] : []),
    '',
    'GitHub closes the issue the moment this merges — before it is deployed,',
    'and before anyone has seen it work. AGENTS.md: do not close an issue you',
    'have not seen fixed.',
    '',
    'Either:',
    '  - write `Refs #N` instead, deploy, verify by the route the reporter',
    '    used, then close the issue by hand with what you saw; or',
    `  - if you have ALREADY confirmed this on a running environment, add a`,
    `    \`${VERIFIED_TRAILER} <what you saw, and where>\` line to the PR body.`,
    '',
    // Both remedies are body edits, and a body edit does NOT re-trigger this
    // workflow -- `pull_request` uses the default types, which exclude
    // `edited`. The body IS read live (#1174/#1180), so a re-run genuinely
    // re-evaluates; without saying so, the obvious next move is to wait for a
    // re-check that never comes, or to push an empty commit to force one.
    // I did the latter on #1304 while this very message was on screen.
    //
    // If the keyword found above is in a COMMIT rather than the body/title,
    // a body edit does not touch it at all — the commit itself has to be
    // reworded (amend/rebase) and force-pushed, since that text is what
    // reaches the squash-merge commit GitHub actually reads (#1334).
    'If the match above is in the PR body or title, edit it, then RE-RUN this',
    'check — do not push an empty commit. Both are read live, so a re-run',
    'genuinely re-evaluates:',
    '',
    '    gh run rerun <run-id> --failed',
    '',
    'If the match is in a COMMIT message, editing the PR changes nothing:',
    'reword the commit (`git commit --amend` or an interactive rebase) and',
    'force-push the branch — the pushed commit message is what this guard,',
    'and GitHub itself, will read.',
    '',
    'The trailer must start the line: a `Verified-on-deploy:` inside backticks',
    'or a bullet is not a trailer and will not be seen.',
  ].join('\n')
}

/**
 * Fetch a PR's CURRENT body via the GitHub CLI. Broken out from
 * `resolveBody` so tests can inject a fake instead of shelling out to `gh`
 * (which needs a token and a network in real CI).
 */
export async function fetchPrBodyViaGh({ GH_TOKEN, PR_NUMBER, GH_REPO }) {
  const { execFileSync } = await import('node:child_process')
  return execFileSync(
    'gh',
    ['pr', 'view', String(PR_NUMBER), '--repo', GH_REPO, '--json', 'body', '--jq', '.body'],
    { encoding: 'utf8', env: { ...process.env, GH_TOKEN } },
  ).trim()
}

/**
 * Resolve the PR body to assess. Two paths, not interchangeable — see #1174.
 *
 *   - `PR_BODY` set (including deliberately empty): used as-is, no network
 *     involved. This is the local-run and test path — every existing test
 *     constructs a body this way, and it must keep working with no `gh` CLI
 *     and no token.
 *   - `PR_BODY` unset, `GH_TOKEN`/`PR_NUMBER`/`GH_REPO` all set: the CI path.
 *     `github.event.pull_request.body` is frozen at the moment the
 *     `pull_request` event fired, so neither editing the PR body nor
 *     re-running the job can ever pick up a later edit from that payload
 *     (verified stale on #1172). Fetching live makes the guard see the PR body
 *     as it is right now, including on a bare re-run with no new event.
 *
 * A failed live fetch is deliberately NOT treated as "no body" — that would
 * make an API outage, a missing token, or a permissions refusal silently pass
 * every PR, which is the exact `class:fail-open` shape #1174 is filed under.
 * It throws instead; the caller must fail the check, not swallow it.
 */
export async function resolveBody({ env = process.env, fetchLiveBody = fetchPrBodyViaGh } = {}) {
  if (env.PR_BODY !== undefined) return env.PR_BODY

  const { GH_TOKEN, PR_NUMBER, GH_REPO } = env
  const trio = [GH_TOKEN, PR_NUMBER, GH_REPO]
  if (trio.some(Boolean) && !trio.every(Boolean)) {
    // Only some of the three are set: a misconfigured workflow, not "not a
    // PR". Falling through to an empty body here would be the same fail-open
    // shape as swallowing a fetch error, just one step earlier.
    throw new Error(
      'GH_TOKEN, PR_NUMBER and GH_REPO must all be set together for the live PR-body fetch; got only some of them.',
    )
  }
  if (!trio.every(Boolean)) return ''

  try {
    return await fetchLiveBody({ GH_TOKEN, PR_NUMBER, GH_REPO })
  } catch (err) {
    throw new Error(
      `could not fetch the live body of PR #${PR_NUMBER} in ${GH_REPO}: ${err?.message ?? err}`,
    )
  }
}

/**
 * Fetch a PR's CURRENT title via the GitHub CLI. Same split as
 * `fetchPrBodyViaGh` so tests can inject a fake.
 */
export async function fetchPrTitleViaGh({ GH_TOKEN, PR_NUMBER, GH_REPO }) {
  const { execFileSync } = await import('node:child_process')
  return execFileSync(
    'gh',
    ['pr', 'view', String(PR_NUMBER), '--repo', GH_REPO, '--json', 'title', '--jq', '.title'],
    { encoding: 'utf8', env: { ...process.env, GH_TOKEN } },
  ).trim()
}

/**
 * Resolve the PR title to assess — the second of the three documents GitHub
 * honours (#1334). Same three-path shape as `resolveBody`, deliberately: a
 * frozen `github.event.pull_request.title` was #1187/#1189's bug for the
 * unrelated release-subject guard, and there is no reason to reintroduce it
 * here by copying the field instead of the pattern.
 *
 *   - `PR_TITLE` set (including deliberately empty): used as-is, no network.
 *   - `PR_TITLE` unset, `GH_TOKEN`/`PR_NUMBER`/`GH_REPO` all set: live fetch.
 *   - Neither: not a PR — empty title, nothing to scan.
 *
 * Fails CLOSED on a half-configured trio or a failed live fetch, same
 * reasoning as `resolveBody` — silently falling back to "no title" would be
 * the `class:fail-open` shape #1174 exists to prevent.
 */
export async function resolveTitle({ env = process.env, fetchLiveTitle = fetchPrTitleViaGh } = {}) {
  if (env.PR_TITLE !== undefined) return env.PR_TITLE

  const { GH_TOKEN, PR_NUMBER, GH_REPO } = env
  const trio = [GH_TOKEN, PR_NUMBER, GH_REPO]
  if (trio.some(Boolean) && !trio.every(Boolean)) {
    throw new Error(
      'GH_TOKEN, PR_NUMBER and GH_REPO must all be set together for the live PR-title fetch; got only some of them.',
    )
  }
  if (!trio.every(Boolean)) return ''

  try {
    return await fetchLiveTitle({ GH_TOKEN, PR_NUMBER, GH_REPO })
  } catch (err) {
    throw new Error(
      `could not fetch the live title of PR #${PR_NUMBER} in ${GH_REPO}: ${err?.message ?? err}`,
    )
  }
}

/**
 * Fetch a PR's commits via the GitHub CLI: `{ messageHeadline, messageBody }`
 * per commit, exactly the shape `gh pr view --json commits` returns. Broken
 * out so tests can inject a fake, same as the body/title fetchers.
 */
export async function fetchPrCommitsViaGh({ GH_TOKEN, PR_NUMBER, GH_REPO }) {
  const { execFileSync } = await import('node:child_process')
  const raw = execFileSync(
    'gh',
    ['pr', 'view', String(PR_NUMBER), '--repo', GH_REPO, '--json', 'commits', '--jq', '.commits'],
    { encoding: 'utf8', env: { ...process.env, GH_TOKEN } },
  ).trim()
  return raw ? JSON.parse(raw) : []
}

/**
 * Resolve the PR's commits to assess — the third document, and the one
 * #1334 is actually about: GitHub builds this repo's squash-merge commit
 * message from the individual commit messages, not from the PR body, so a
 * closing keyword left there survives a body edit that looks like a fix.
 *
 * Same three-path shape as `resolveBody`/`resolveTitle`:
 *
 *   - `PR_COMMITS` set (including `''`, read as no commits): a JSON array of
 *     `{ messageHeadline, messageBody }`, used as-is, no network — the
 *     local-run and test path.
 *   - `PR_COMMITS` unset, `GH_TOKEN`/`PR_NUMBER`/`GH_REPO` all set: live
 *     fetch, so a re-run sees the commits as they are right now (an amend +
 *     force-push), not as they were when the PR was opened.
 *   - Neither: not a PR — no commits to scan.
 *
 * Fails CLOSED on a half-configured trio or a failed fetch, same as the
 * other two resolvers and for the same reason: a silent empty-commits
 * fallback here is indistinguishable from "nothing to find" and would let
 * an API outage pass every PR — the exact shape #1174 is filed under.
 */
export async function resolveCommits({
  env = process.env,
  fetchLiveCommits = fetchPrCommitsViaGh,
} = {}) {
  if (env.PR_COMMITS !== undefined) return env.PR_COMMITS === '' ? [] : JSON.parse(env.PR_COMMITS)

  const { GH_TOKEN, PR_NUMBER, GH_REPO } = env
  const trio = [GH_TOKEN, PR_NUMBER, GH_REPO]
  if (trio.some(Boolean) && !trio.every(Boolean)) {
    throw new Error(
      'GH_TOKEN, PR_NUMBER and GH_REPO must all be set together for the live PR-commits fetch; got only some of them.',
    )
  }
  if (!trio.every(Boolean)) return []

  try {
    return await fetchLiveCommits({ GH_TOKEN, PR_NUMBER, GH_REPO })
  } catch (err) {
    throw new Error(
      `could not fetch the commits of PR #${PR_NUMBER} in ${GH_REPO}: ${err?.message ?? err}`,
    )
  }
}

// ── CLI ───────────────────────────────────────────────────────────────────
// Bare node, no install, matching practices-monotonic.mjs — so this runs in
// the Release Guards job without depending on the pnpm install step.
if (import.meta.url === `file://${process.argv[1]}`) {
  const { execSync } = await import('node:child_process')

  const base = process.env.GITHUB_BASE_REF

  if (!base) {
    console.log('✓ closing-keyword guard: skipped — no GITHUB_BASE_REF (not a pull request).')
    process.exit(0)
  }

  let body, title, commits
  try {
    // All three read live where a token is available (#1174, and #1334 for
    // commits specifically) — a re-run genuinely re-evaluates the PR/commits
    // as they are now, not as they were when the workflow event fired.
    body = await resolveBody()
    title = await resolveTitle()
    commits = await resolveCommits()
  } catch (err) {
    // Fail closed (#1174): an unreadable body/title/commits is an error,
    // never a silent "no closing keyword found".
    console.error(`✘ closing-keyword guard: ${err.message}`)
    process.exit(1)
  }

  let changedFiles = []
  try {
    changedFiles = execSync(`git diff --name-only origin/${base}...HEAD`, { encoding: 'utf8' })
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
  } catch (err) {
    // A guard that cannot see its input must say so rather than passing. The
    // estate's most repeated defect is a zero that means "could not look".
    console.error(`✘ closing-keyword guard: could not diff against origin/${base}.`)
    console.error(String(err?.stderr ?? err?.message ?? err))
    process.exit(1)
  }

  const result = assess({ body, title, commits, changedFiles })
  if (result.ok) {
    console.log(`✓ closing-keyword guard: ${result.reason}.`)
    process.exit(0)
  }

  console.error(formatFailure(result))
  process.exit(1)
}
