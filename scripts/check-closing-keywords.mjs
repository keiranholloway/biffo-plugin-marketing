#!/usr/bin/env node
/**
 * Three guards over a PR's closing keywords, asking different questions —
 * and, since #1334, applied to every document GitHub actually honours, not
 * just the PR body.
 *
 * 1. Refuse `Closes #N` on a change whose behaviour only shows up once
 *    deployed — a path-scoped check, documented immediately below.
 * 2. Refuse a NEGATED closing keyword anywhere, on any path — see
 *    `negatedClosingReferences`. GitHub's linker has no concept of negation,
 *    so `Does not close #N` closes #N.
 * 3. Refuse a mismatch between GitHub's OWN `closingIssuesReferences` and
 *    what this file's lexical scan calls "deliberate" — see
 *    `deliberateClosingReferences` and the "3. Ground truth" section below
 *    (#1686). This is the one that reconciles the guard's model against the
 *    thing that actually acts, rather than trying to out-regex it. Extended
 *    in "3b" below (#1732) to a document `closingIssuesReferences` itself
 *    cannot see.
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
 *
 * ── 3. Ground truth: reconciling against `closingIssuesReferences` (#1686) ──
 *
 * Checks 1 and 2 above both infer intent from a regex over prose — and a
 * regex over prose can only ever be a MODEL of what GitHub's own linker does,
 * never the thing itself. PR #1680's body read (in full context) "This is
 * the one-word fix #1664 asked for" — ordinary mid-sentence prose, not a
 * deliberate `Closes #N` trailer — alongside its own explicit `Refs #1664`
 * elsewhere in the same body. GitHub's `closingIssuesReferences` nonetheless
 * read `totalCount: 1 -> #1664` while the PR was in that state: the lexical
 * shape GitHub's linker looks for does not care about sentence position, and
 * this file's `closingReferences` (check 1's hit detector) doesn't either —
 * so `assess` correctly recorded a hit, but `changedFiles` for that PR were
 * `cli/src/lib/pg-test-db-reaper.test.ts` and `scripts/pg-test-db.sh` — no
 * `DEPLOY_ONLY_PREFIXES` entry — so check 1 returned
 * `{ ok: true, reason: 'no-deploy-only-paths' }`. Release Guards reported
 * SUCCESS. Only a human rewording the body before merge kept #1664 open.
 *
 * The deploy-only-path scoping is not wrong and is NOT removed here: a
 * genuinely deliberate `Closes #N` on a path whose correctness a green suite
 * already proves is exactly the case it exists to let through. What was
 * wrong is narrower — a hit was silently PASSED whenever the paths were
 * ordinary, with nothing checking whether GitHub was actually about to act on
 * it. `deliberateClosingReferences` narrows check 1's hit detector to
 * keyword+reference pairs that read as a genuine directive — at the start of
 * the document, a line, or a sentence, optionally after a list/heading/bold
 * marker — as opposed to buried mid-sentence. If GitHub's own
 * `closingIssuesReferences` is non-empty and NOTHING in the PR's documents
 * carries a deliberate closing keyword, that is a closing-keyword hit GitHub
 * will act on that this file cannot explain as intentional — fail regardless
 * of path, because the path-scoped hazard this file was built to catch is a
 * SUBSET of "GitHub is about to close something nobody asked for", not a
 * replacement for it.
 *
 * This also happens to close a gap #1686 flagged but explicitly did NOT ask
 * to be fixed here: a closing shape GitHub's linker recognises that this
 * file's own regex does not (e.g. a reference before its keyword) would
 * previously have returned `no-closing-keyword` — hits.length === 0 — with
 * nothing to catch it. Asking GitHub directly, rather than trying to widen
 * the regex to match its exact recognition rules, structurally covers that
 * case too: `deliberateClosingReferences` would find nothing "deliberate"
 * either, and the ground-truth check would still fire. This is a consequence
 * of the design, not a claim that the widened-adjacency shape was reproduced
 * — it was not, deliberately (see #1686's own "UNCONFIRMED SECONDARY CLAIM").
 *
 * Level of fix: 3 (fail closed), not 1 or 2. The invalid state cannot be made
 * unrepresentable, because the closing keyword lives in prose an author
 * legitimately writes and there is no way to derive intent from it with
 * certainty — `deliberateClosingReferences` is a heuristic, not a parser of
 * meaning. What IS achievable, and what this does, is refuse to let our own
 * heuristic's blind spot silently diverge from GitHub's actual behaviour: the
 * two are reconciled every time, and a mismatch fails rather than passing.
 *
 * ── 3b. Ground truth's own blind spot: it never reads a commit (#1732) ───
 *
 * Section 3 above reconciles this file's lexical model against
 * `closingIssuesReferences` — but that field is itself only a MODEL of one
 * of the three documents GitHub honours: it is GitHub's ground truth for the
 * PR BODY, computed by the same markdown-aware linker that renders the PR
 * page, and it structurally cannot see a commit message at all. This repo's
 * squash-merge strategy is `squash_merge_commit_message = COMMIT_MESSAGES`
 * (confirmed via `gh api repos/{owner}/{repo}` → `squash_merge_commit_title`/
 * `_message`), so the actual merge commit GitHub creates is composed from the
 * branch's own commit messages, verbatim — not from the PR body at all. A
 * closing-keyword hit that lives only in a commit message is therefore
 * something GitHub WILL act on that section 3's check is a structural no-op
 * for, not a considered "safe": `closingIssuesReferences.length > 0` is
 * simply never true for it, no matter how dangerous the commit text is.
 *
 * Real instance: merging PR #1730 (this very guard's own #1686 fix)
 * spuriously closed unrelated issue #1664. Its body quoted the historical
 * bug it was fixing — "the one-word fix #1664 asked for" — inside a markdown
 * code span, so GitHub's PR-body linker correctly ignored it and
 * `closingIssuesReferences` read `[]`, exactly what section 3 checks and
 * exactly what let it through. The identical phrase reached the real squash
 * commit unchanged, in the PR's own commit message — WITHOUT a code span
 * there, because a git commit message has no markdown semantics at all: a
 * backtick in one is two literal characters, not a code-span delimiter, and
 * GitHub's push-based "closes on merge to the default branch" keyword scan
 * is a completely different mechanism from the PR-body linker, with no
 * concept of markdown to respect. #1664 closed one second after merge.
 *
 * This is also why `stripCode` cannot simply be applied to a commit-message
 * document the way it is to the body/title: doing so would make this file's
 * OWN lexical scan (`closingReferences`, `deliberateClosingReferences`,
 * `negatedClosingReferences` — checks 1 and 2 above, not just this one) less
 * sensitive than GitHub's real behaviour for that document, the same
 * "guard reads a different document from the one that acts" shape #1362
 * names, just one level further in: the guard was reading the RIGHT document
 * (#1334 already fixed that) but modelling it with the WRONG renderer's
 * rules. Every function above therefore takes a `{ code: false }` option
 * (see `documentsFor`'s `kind` tag and `assess`'s `rawScan`) that skips
 * `stripCode` for a `'commit'` document — never for `'body'`/`'title'`,
 * where a code span is genuine, GitHub-honoured protection.
 *
 * With that in place, `assess` runs a second, independent ground-truth
 * reconciliation scoped to commit documents alone, using the commit text's
 * own (now un-stripped) lexical hit as the ground truth `closingIssuesReferences`
 * can never supply: on an otherwise-safe (non-deploy-only) path, if a commit
 * document carries a hit and nothing anywhere reads as deliberate, fail —
 * `kind: 'commit-ground-truth-mismatch'`. A hit on a genuinely deploy-only
 * path is still caught by check 1 regardless, exactly as before; this only
 * closes the gap check 1 always had by design (ordinary paths pass) and
 * section 3 could not close for this one document (ground truth never
 * arrives). See `scripts/check-closing-keywords-ground-truth.test.sh` for
 * the fail-first reproduction of PR #1730's exact real shape, plus the
 * corpus cases either side of it.
 *
 * Level of fix: still 3 (fail closed), same reasoning as section 3 — a
 * commit message is prose an author legitimately writes, so intent cannot be
 * derived with certainty here either. Not made MORE strict than GitHub's own
 * closing behaviour: GitHub will act on a commit-message hit regardless of
 * position or backticks, and this check only refuses the ones this file
 * cannot explain as intentional, using the identical `deliberateClosingReferences`
 * heuristic and its identical, already-accepted trade-off (see that
 * function's docstring for two real, intentional, mid-line-parenthetical
 * closes this heuristic already did not recognise before this change,
 * unrelated to commits — this does not introduce a new blind spot, it
 * extends an existing, documented one to a new document).
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
 * The whitespace allowed between a closing keyword (plus its optional `:`)
 * and the issue reference it governs, in `closingReferences`,
 * `deliberateClosingReferences` and `negatedClosingReferences` alike — all
 * three end their pattern with the identical `\b:?\s+(REFERENCE)` tail, and
 * all three had the identical bug.
 *
 * A bare `\s+` matches across an ARBITRARY run of blank lines, so a
 * markdown heading ending in a closing keyword matched against a `#N` that
 * starts a completely unrelated paragraph two lines later — nothing in the
 * character class stopped it from spanning the blank line between them.
 * Real instance, PR #1789's own body:
 *
 *   ## What this fixes
 *
 *   #1717 fixed one of two acceptance routes ...
 *
 * "fixes" is the last word of the heading; "#1717" is the first token of
 * the next paragraph's own, unrelated sentence. `closingReferences`
 * nonetheless reported `#1717` as a hit — `\s+` matched the `"\n\n"`
 * between them — and that false hit reached the `deploy-only-path` branch
 * of `assess`, which (unlike the two ground-truth branches) has no
 * reconciliation step and fails on whatever `closingReferences` reports
 * verbatim. Confirmed against GitHub's own ground truth for that PR
 * (`gh pr view 1789 --json closingIssuesReferences`): only `#1718` — the
 * genuine `Closes #1718` trailer — is ever returned; #1717 never was.
 *
 * A single line break is still allowed: nothing in this repo's corpus
 * needs it, but there is no evidence GitHub REQUIRES same-line adjacency
 * either, and the conservative/fail-closed choice for a guard whose job is
 * "don't miss a real close" is to keep allowing one newline and exclude
 * only a genuine blank line (two or more).
 */
const KEYWORD_REFERENCE_GAP = '(?:[ \\t]+|[ \\t]*\\n[ \\t]*)'

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
 * form. Ignores keywords inside code — see `stripCode` — UNLESS `{ code:
 * false }` is passed, which skips that blanking entirely.
 *
 * `code` must be `false` for a COMMIT MESSAGE document (#1732): a git commit
 * message has no markdown semantics, so a backtick there is two literal
 * characters, not a code-span delimiter, and GitHub's push-based "closes on
 * merge to the default branch" keyword scan reads it exactly that way — it
 * is not the same renderer as the PR body/title, which genuinely are
 * markdown and where `stripCode` correctly models GitHub's own linker. See
 * `assess`'s `rawForDoc` and the module docstring's "3b" section.
 */
export function closingReferences(body, { code = true } = {}) {
  if (!body) return []
  const withoutCode = code ? stripCode(body) : body
  const pattern = new RegExp(
    `\\b(${CLOSING_KEYWORDS.join('|')})\\b:?${KEYWORD_REFERENCE_GAP}(${REFERENCE})`,
    'gi',
  )
  return [...withoutCode.matchAll(pattern)].map((m) => m[2])
}

/**
 * Markdown decoration a clause may legitimately start with before the
 * keyword itself: a list marker (`-`, `*`, `1.`, `1)`), heading hashes, or
 * bold (`**`). Real shapes from this repo's own history: `- tabsii-
 * platform#511, today: \`Closes #511\`` (list item; the keyword itself was
 * inside backticks there and so already blanked by `stripCode`, but plain
 * `- Closes #42` is the same shape without the backticks) and `**Fixes
 * #10**` (bold trailer).
 */
const CLAUSE_DECORATION = '(?:[-*•]\\s+|\\d+[.)]\\s+|#{1,6}\\s+|\\*{1,2})*'

/**
 * The closing-keyword references that read as a DELIBERATE directive rather
 * than incidental prose — the keyword+reference sits at the start of the
 * document, a line, or a sentence (optionally after `CLAUSE_DECORATION`),
 * rather than buried mid-sentence.
 *
 * Real corpus evidence for both shapes, from this repo's own commit history
 * (`git log --all --format='%B'`):
 *
 *   - Deliberate — hundreds of `Closes #1234` lines used as commit-message
 *     trailers, plus `warnings on both commands. Closes #201.` (a trailer
 *     sentence following prose on the SAME physical line, which is why this
 *     splits on sentence-ending punctuation too, not only on newlines).
 *   - NOT deliberate — `This is the one-word fix #1664 asked for` (PR
 *     #1680's real, pre-reword text — the shape #1686 is filed over): `fix`
 *     is a real closing keyword immediately followed by a real reference,
 *     but it is the predicate of an ordinary sentence, not a directive.
 *     Likewise `That closes #422 by construction rather than policing it`
 *     and `` `--fix` exists to close #714 and #715 `` (both real lines from
 *     this repo's own history) — mid-sentence, not clause-initial.
 *
 * This is intentionally a narrower, less permissive detector than
 * `closingReferences` — it exists only to ask "does this file have a
 * confident READING of author intent", not to replace the lexical scan
 * `closingReferences` still does for checks 1 and 2 above.
 *
 * Known residual gap, accepted rather than solved: a trailer that starts
 * mid-line without sentence-ending punctuation before it reads as
 * not-deliberate. That is the conservative direction — it can make the
 * ground-truth check (below) ask for a clarifying reword it didn't strictly
 * need, never the reverse. Two real instances, both intentional and both
 * missed by this heuristic because a parenthesis is not a boundary this
 * function looks for: `chore(core): mark services/pr-signer/ template-owned
 * (closes #243/#548-shaped gap) (#581)` and `feat(cli): publish the CLI to
 * npm as versioned `biffo` (closes #259) (#300)` (both real commit subjects,
 * `git log --all --format='%B'`). Widening the boundary set to also start a
 * clause after `(` was considered and rejected here: it would not even have
 * caught either example (neither open-paren sits at a position this
 * function currently recognises as a clause start), and it is exactly the
 * kind of heuristic change #1628 warns against making without a full case
 * matrix — see AGENTS.md. The remedy is the same as always: a `Closes #N` on
 * its own line or sentence.
 *
 * `code`, same contract as `closingReferences` (#1732): pass `{ code: false
 * }` for a commit-message document, since backticks are not markdown there
 * and must not be treated as protection.
 */
export function deliberateClosingReferences(text, { code = true } = {}) {
  if (!text) return []
  const stripped = code ? stripCode(text) : text
  const starts = new Set([0])
  const boundary = /\n|[.!?]\s+/g
  let m
  while ((m = boundary.exec(stripped))) starts.add(m.index + m[0].length)

  const pattern = new RegExp(
    `^${CLAUSE_DECORATION}\\s*(${CLOSING_KEYWORDS.join('|')})\\b:?${KEYWORD_REFERENCE_GAP}(${REFERENCE})`,
    'i',
  )
  const found = []
  for (const start of starts) {
    const mm = stripped.slice(start).match(pattern)
    if (mm) found.push(mm[2])
  }
  return [...new Set(found)]
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
 * Returns `[{ reference, line, lineNumber }]`, in body order. `code`, same
 * contract as `closingReferences` — pass `{ code: false }` for a commit
 * message (#1732), since backticks do not protect text there.
 */
export function negatedClosingReferences(body, { code = true } = {}) {
  if (!body) return []
  const text = code ? stripCode(body) : body
  const authored = body.split('\n')
  const pattern = new RegExp(
    `(?:${NEGATIONS.join('|')})\\s+(?:${CLOSING_KEYWORDS.join('|')})\\b:?${KEYWORD_REFERENCE_GAP}(${REFERENCE})`,
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
 *
 * Each doc also carries `kind` — `'body'`, `'title'`, or `'commit'` (#1732).
 * The PR body and title are genuinely markdown, rendered by GitHub's own PR
 * page, so a code span in either is real protection. A commit message is
 * neither: it has no markdown semantics for GitHub's push-based "closes on
 * merge to the default branch" keyword scan, so `assess` must scan `'commit'`
 * documents with `{ code: false }` — see that function and the module
 * docstring's "3b" section.
 */
export function documentsFor({ body, title, commits }) {
  const docs = [{ source: 'the PR body', text: body, kind: 'body' }]
  if (title) docs.push({ source: 'the PR title', text: title, kind: 'title' })
  const list = commits ?? []
  list.forEach((commit, i) => {
    const label = list.length === 1 ? 'the commit message' : `commit ${i + 1}`
    if (commit?.messageHeadline) {
      docs.push({ source: `${label} (subject)`, text: commit.messageHeadline, kind: 'commit' })
    }
    if (commit?.messageBody) {
      docs.push({ source: `${label} (body)`, text: commit.messageBody, kind: 'commit' })
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
 *
 * The ground-truth check (#1686) runs SECOND, before the deploy-path check,
 * and also ignores `changedFiles`: it is not asking "is this a hazard here",
 * it is asking "is GitHub about to do something this file cannot explain as
 * intentional" — see the module docstring's "3. Ground truth" section.
 * `closingIssuesReferences` defaults to `[]` so every existing body-only
 * caller (and every existing test) keeps working unchanged, the same reason
 * `title`/`commits` are optional — see `documentsFor`.
 */
// A document is markdown, and therefore genuinely protected by a code span,
// only if GitHub's OWN renderer treats it that way. The PR body and title
// are; a commit message is not — see `documentsFor` and the module
// docstring's "3b" section (#1732). `closingReferences`, `deliberateClosingReferences`
// and `negatedClosingReferences` all take `{ code: false }` to mean "scan
// this raw, backticks are literal characters here".
const rawScan = (doc) => ({ code: doc.kind !== 'commit' })

export function assess({ body, title, commits, changedFiles, closingIssuesReferences = [] }) {
  const docs = documentsFor({ body, title, commits })

  const negated = docs.flatMap((doc) =>
    negatedClosingReferences(doc.text, rawScan(doc)).map((n) => ({ ...n, source: doc.source })),
  )
  if (negated.length > 0) return { ok: false, kind: 'negated-keyword', negated }

  // Whether ANY document reads as a deliberate closing directive — shared
  // between the two ground-truth checks below, since both ask the identical
  // question ("is this hit something the author actually meant"), just
  // triggered by two different sources of ground truth.
  const deliberate = docs.some(
    (doc) => deliberateClosingReferences(doc.text, rawScan(doc)).length > 0,
  )

  if (closingIssuesReferences.length > 0 && !deliberate) {
    return { ok: false, kind: 'ground-truth-mismatch', closingIssuesReferences }
  }

  const hits = docs
    .map((doc) => ({
      source: doc.source,
      isCommit: doc.kind === 'commit',
      references: closingReferences(doc.text, rawScan(doc)),
    }))
    .filter((h) => h.references.length > 0)
  if (hits.length === 0) return { ok: true, reason: 'no-closing-keyword' }

  const paths = deployOnlyPaths(changedFiles)
  if (paths.length === 0) {
    // ── 3b. Ground truth, extended to the document GitHub actually squashes
    // (#1732) ──────────────────────────────────────────────────────────────
    //
    // `closingIssuesReferences` is GitHub's OWN ground truth for what the PR
    // BODY will close — but it structurally cannot see a commit message, and
    // this repo's squash-merge composes the real merge commit from commit
    // messages verbatim (`squash_merge_commit_message = COMMIT_MESSAGES`).
    // A closing-keyword hit that lives only in a commit message is therefore
    // something GitHub WILL act on that `closingIssuesReferences` can never
    // confirm OR deny — the check above is a structural no-op for it, not a
    // considered "safe". Real instance: PR #1730's body quoted the phrase
    // "the one-word fix #1664 asked for" inside a markdown code span, so
    // GitHub's PR-body linker correctly ignored it (closingIssuesReferences
    // read `[]`) — but the identical phrase reached the actual squash commit
    // verbatim from the branch's own commit message, WITHOUT a code span
    // (a git commit message has no markdown semantics: a backtick there is
    // two literal characters, not a code-span delimiter), and closed #1664
    // one second after merge.
    //
    // So a commit-only hit gets the same reconciliation the body already
    // gets from `closingIssuesReferences`, using the commit text itself as
    // the ground truth `closingIssuesReferences` cannot supply: if a commit
    // document carries a hit and nothing anywhere reads as deliberate, fail
    // — regardless of path, and regardless of what `closingIssuesReferences`
    // said, since it was never asked about this document.
    //
    // Known residual gap, same shape and same acceptance as
    // `deliberateClosingReferences`'s own docstring: a deliberate close
    // written as a mid-line parenthetical (`(closes #NNN)`) is not
    // recognised as deliberate either, so it would ask for a reword it did
    // not strictly need. Conservative direction only — see that docstring.
    const commitHits = hits.filter((h) => h.isCommit)
    if (commitHits.length > 0 && !deliberate) {
      return { ok: false, kind: 'commit-ground-truth-mismatch', hits: commitHits }
    }
    return { ok: true, reason: 'no-deploy-only-paths' }
  }

  if (hasVerifiedTrailer(body)) return { ok: true, reason: 'verified-trailer' }

  const references = [...new Set(hits.flatMap((h) => h.references))]
  return { ok: false, kind: 'deploy-only-path', references, paths, hits }
}

export function formatFailure(result) {
  if (result.kind === 'negated-keyword') return formatNegatedFailure(result)
  if (result.kind === 'ground-truth-mismatch') return formatGroundTruthFailure(result)
  if (result.kind === 'commit-ground-truth-mismatch') return formatCommitGroundTruthFailure(result)
  return formatDeployOnlyFailure(result)
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

function formatGroundTruthFailure({ closingIssuesReferences }) {
  const refs = closingIssuesReferences.map((r) =>
    r?.number !== undefined ? `#${r.number}` : (r?.url ?? JSON.stringify(r)),
  )
  return [
    `GitHub's own closingIssuesReferences says this PR will close ${refs.join(', ')} on`,
    'merge — but nothing in the PR body, title or commit messages reads as a',
    'DELIBERATE closing directive (a keyword+reference at the start of the',
    "document, a line, or a sentence). GitHub's linker does not care about",
    'paths or sentence position; it only needs the lexical shape, wherever it',
    'sits.',
    '',
    'This is #1686: PR #1680\'s body read "This is the one-word fix #1664',
    'asked for" — ordinary prose, not a directive — alongside its own',
    'explicit `Refs #1664` elsewhere in the same body. closingIssuesReferences',
    'nonetheless read #1664 while the PR was in that state, and Release Guards',
    'reported SUCCESS: the deploy-only-path check only fires on a hazardous',
    'PATH, and this PR touched none. Only a human rewording the body before',
    'merge kept #1664 open.',
    '',
    'Either:',
    '  - this close is NOT intended: reword the offending line so the keyword',
    '    and reference are not adjacent (e.g. "the fix requested in #1664"',
    '    rather than "fix #1664"), or move the reference into a `Refs #N`',
    '    line; or',
    '  - this close IS intended: make it a deliberate directive — its own',
    '    line, its own sentence, or after a list/heading/bold marker, e.g.',
    '    `Closes #1664` — so this file, and anyone reading the PR, can tell',
    '    the difference.',
    '',
    'Re-run after editing — the body, title and commits are all read live, so',
    'a re-run genuinely re-evaluates them (do not push an empty commit):',
    '',
    '    gh run rerun <run-id> --failed',
  ].join('\n')
}

function formatCommitGroundTruthFailure({ hits }) {
  const refs = [...new Set(hits.flatMap((h) => h.references))]
  return [
    `A COMMIT message would close ${refs.join(', ')} on merge — found in:`,
    '',
    ...hits.map((h) => `  - ${h.source}: ${h.references.join(', ')}`),
    '',
    "GitHub's own `closingIssuesReferences` cannot see this: that field",
    'reflects only the PR body as GitHub itself parses it, and this repo',
    "builds the real squash-merge commit from the branch's own commit",
    'messages verbatim (squash_merge_commit_message = COMMIT_MESSAGES) — a',
    'separate mechanism GitHub applies to that text with no markdown',
    'awareness at all: a backtick in a commit message is a literal',
    'character, not a code-span delimiter, so it does NOT protect a',
    'closing keyword there the way it would in the PR body.',
    '',
    'This is #1732: PR #1730\'s body quoted "the one-word fix #1664 asked',
    'for" inside a markdown code span, so closingIssuesReferences correctly',
    'read [] — but the identical phrase, without a code span, was already',
    "sitting in the branch's own commit message, and closed #1664 one",
    'second after merge.',
    '',
    'Nothing in the PR body, title or commit messages reads as a DELIBERATE',
    'closing directive (a keyword+reference at the start of the document, a',
    'line, or a sentence). Either:',
    '  - this close is NOT intended: reword the COMMIT (`git commit --amend`',
    '    or an interactive rebase) so the keyword and reference are not',
    '    adjacent, or move the reference into its own `Refs #N` line, and',
    '    force-push; or',
    '  - this close IS intended: make it a deliberate directive in the',
    '    COMMIT — its own line, its own sentence, e.g. `Closes #1664` — so',
    '    this file, and anyone reading `git log`, can tell the difference.',
    '',
    'Editing the PR body does NOT fix this: the commit message is what',
    'reaches the squash-merge commit GitHub actually reads, independent of',
    'anything in the PR description. Re-run after amending and force-pushing',
    '— commits are read live, so a re-run genuinely re-evaluates:',
    '',
    '    gh run rerun <run-id> --failed',
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

/**
 * Fetch a PR's `closingIssuesReferences` via the GitHub CLI — GitHub's own
 * ground truth for which issues this PR will close on merge (#1686). Same
 * split as the other fetchers so tests can inject a fake. Each element is
 * `{ id, number, repository: {...}, url }` (confirmed live against PR #1417,
 * which genuinely closes an issue, and against #1730/tabsii-crm#379 below).
 *
 * This calls `gh api graphql` with an explicit query, NOT `gh pr view --json
 * closingIssuesReferences`. That shorthand only works if the installed `gh`
 * binary's OWN hardcoded `--json` field allowlist happens to include the
 * field — `closingIssuesReferences` was added to that allowlist partway
 * through gh's release history, so it is a property of the CLI binary, not
 * of the GitHub API. tabsii-crm#379 failed identically on two attempts of
 * the same commit with `Unknown JSON field: "closingIssuesReferences"` —
 * this repo's own `gh` (2.96.0) lists the field, but tabsii-crm's Release
 * Guards runs on ITS OWN self-hosted runner fleet (`vars.RUNNER_LABEL:
 * tabsii`), whose baked-in `gh` binary predates it. That is a real, and
 * recurring, source of drift: every satellite's runner image can lag behind
 * whatever `gh` happens to be on the machine this file was last tested on,
 * and `check-closing-keywords.mjs` is distributed VERBATIM (`shared-files.json`
 * `files`) to every one of them — so pinning to a newer allowlisted field is
 * a bug this file WILL hit again on the next satellite with an older image,
 * not a one-off.
 *
 * `gh api graphql` has no such allowlist: it sends the query text through
 * to GitHub's GraphQL endpoint verbatim, and has done so since `gh api` was
 * introduced, long before `closingIssuesReferences` reached `pr view --json`.
 * Asking for a field GitHub's schema does not have is still a real failure —
 * it always was, and always will be, GitHub's error rather than the local
 * binary's — but a locally-out-of-date `gh` can no longer manufacture a
 * false one. This removes the CLI-version dependency instead of chasing it
 * runner image by runner image.
 */
export async function fetchPrClosingIssuesReferencesViaGh({ GH_TOKEN, PR_NUMBER, GH_REPO }) {
  const { execFileSync } = await import('node:child_process')
  const [owner, repo] = GH_REPO.split('/')
  const query = `
    query($owner: String!, $repo: String!, $num: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $num) {
          closingIssuesReferences(first: 50) {
            nodes { id number url repository { nameWithOwner } }
          }
        }
      }
    }
  `
  const raw = execFileSync(
    'gh',
    [
      'api',
      'graphql',
      '-f',
      `query=${query}`,
      '-f',
      `owner=${owner}`,
      '-f',
      `repo=${repo}`,
      '-F',
      `num=${PR_NUMBER}`,
      '--jq',
      '.data.repository.pullRequest.closingIssuesReferences.nodes',
    ],
    { encoding: 'utf8', env: { ...process.env, GH_TOKEN } },
  ).trim()
  return raw ? JSON.parse(raw) : []
}

/**
 * Resolve the PR's `closingIssuesReferences` to assess — the ground-truth
 * check's own input, and the reason it needs no new CI wiring: it reads via
 * the same `GH_TOKEN`/`PR_NUMBER`/`GH_REPO` trio `resolveTitle` and
 * `resolveCommits` already use, already present wherever this script runs
 * as a PR check.
 *
 * Same three-path shape as the other resolvers:
 *
 *   - `PR_CLOSING_ISSUES` set (including `''`, read as none): a JSON array,
 *     used as-is, no network — the local-run and test path.
 *   - `PR_CLOSING_ISSUES` unset, `GH_TOKEN`/`PR_NUMBER`/`GH_REPO` all set:
 *     live fetch, so a re-run sees the current linkage, not the one at the
 *     moment the workflow event fired (the exact staleness #1174 fixed for
 *     the body).
 *   - Neither: not a PR — nothing to reconcile against.
 *
 * Fails CLOSED on a half-configured trio or a failed fetch, same as the
 * other resolvers: a silent empty-array fallback here would make an API
 * outage read as "GitHub confirms nothing closes", which is the opposite of
 * cautious for a check whose whole job is to catch what OUR OWN scan missed.
 */
export async function resolveClosingIssuesReferences({
  env = process.env,
  fetchLiveClosingIssuesReferences = fetchPrClosingIssuesReferencesViaGh,
} = {}) {
  if (env.PR_CLOSING_ISSUES !== undefined) {
    return env.PR_CLOSING_ISSUES === '' ? [] : JSON.parse(env.PR_CLOSING_ISSUES)
  }

  const { GH_TOKEN, PR_NUMBER, GH_REPO } = env
  const trio = [GH_TOKEN, PR_NUMBER, GH_REPO]
  if (trio.some(Boolean) && !trio.every(Boolean)) {
    throw new Error(
      'GH_TOKEN, PR_NUMBER and GH_REPO must all be set together for the live closing-issues fetch; got only some of them.',
    )
  }
  if (!trio.every(Boolean)) return []

  try {
    return await fetchLiveClosingIssuesReferences({ GH_TOKEN, PR_NUMBER, GH_REPO })
  } catch (err) {
    throw new Error(
      `could not fetch the closing-issues references of PR #${PR_NUMBER} in ${GH_REPO}: ${err?.message ?? err}`,
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

  let body, title, commits, closingIssuesReferences
  try {
    // All four read live where a token is available (#1174, #1334 for
    // commits, #1686 for closingIssuesReferences) — a re-run genuinely
    // re-evaluates the PR/commits/linkage as they are now, not as they were
    // when the workflow event fired.
    body = await resolveBody()
    title = await resolveTitle()
    commits = await resolveCommits()
    closingIssuesReferences = await resolveClosingIssuesReferences()
  } catch (err) {
    // Fail closed (#1174): an unreadable body/title/commits/linkage is an
    // error, never a silent "no closing keyword found".
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

  const result = assess({ body, title, commits, changedFiles, closingIssuesReferences })
  if (result.ok) {
    console.log(`✓ closing-keyword guard: ${result.reason}.`)
    process.exit(0)
  }

  console.error(formatFailure(result))
  process.exit(1)
}
