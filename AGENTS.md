# Rules of Engagement for AI Agents

These rules are **binding for every automated coding agent** working in this
repository — Claude Code, Codex, Cursor, or any other. They are tool-agnostic:
express everything as plain `git` / `gh` / `pnpm` / `uv` commands. Tool-specific
guidance belongs in that tool's own file (e.g. `CLAUDE.md`), which imports this
one rather than restating it.

This is a **Biffo satellite repo** — a repository in the estate that is not a
core project: a sibling app, a plugin, a shared package, a runner fleet, or a
design/data repo. It follows the same engagement rules as every Biffo repo; only
its product differs. `CLAUDE.md` says which one this is.

This file is distributed by the template and kept in step by
`scripts/shared-sync.sh` there (`shared-files.json`). Edit it **upstream in
`biffo-template`**, not here: a local edit is overwritten by the next sync, and
`AGENTS.md` silently drifting 68 lines behind is why that mechanism exists
(biffo-template#559, #1150).

## 1. One integration branch: `dev`

- **`dev` is the integration branch** — you branch from it and open every PR back
  into it; `gh repo view --json defaultBranchRef` confirms it. If it reports
  anything other than `dev`, flag it rather than working around it. (A sibling app
  also carries `staging` and a reserved `main`/production branch as promotion
  targets — production not built yet; a plugin repo has `dev` only. Either way you
  work off `dev`.)
- **All changes land via PR.** No direct commits to `dev`, no force-pushes to it.
  Branch protection stays on.
- Never leave the primary checkout parked on a feature branch or let it fall
  behind — keep it on `dev`, no more than a `git fetch` behind, and do the work
  in worktrees (§2).

## 2. Work in an isolated worktree

- **Always start from a freshly-fetched `dev`:** `git fetch origin`, then
  `git worktree add .worktrees/<name> -b <type>/<slug> origin/dev`. Never branch
  from a stale local ref.
- **Install dependencies in the new worktree before working** — `uv sync` and/or
  `pnpm install`, so the local gates (pre-push checks, lint-staged) run against
  fresh deps, not a stale `.venv`/`node_modules`.
- **One worktree per unit of work**, under the git-ignored `.worktrees/`.
- **Clean up when the PR merges:** `git worktree remove .worktrees/<name>`, then
  let the branch be deleted. Keep `git worktree list` short and every entry live.

### Check before you start, and push early

**Before starting work on an issue, run:**

```bash
sh scripts/biffo.sh claim <issue-number> [-R owner/repo]   # 0 free · 1 taken · 2 cannot tell
```

Several agent sessions run against this estate at once. The script asks four
questions, because the answer lives in more than one place: does the issue carry
the `in-progress` label, is there an **open PR** referencing it, is there a
**remote branch** naming it, and has a PR already merged that closes it. If it
reports free, it claims the issue for you.

**Exit 2 is "cannot tell" and is never "free".** An unreadable issue or an
unauthenticated `gh` must stop you, not wave you through.

**Why four signals rather than the label alone.** The label is a
hand-maintained second copy of something git already knows: a branch exists, a
PR exists. Those are automatic — you cannot do the work without creating them —
while the label is a separate action someone has to remember. On 2026-08-03 four
sessions collided in one morning, and **three of the four were "work exists,
label does not"**. Checking only the label would have caught one of four.

**Push your branch as soon as it exists.** A local worktree is invisible to
every other machine; the pushed branch is the only signal they can see. The
window between starting and pushing is where collisions actually happen — one of
that morning's issues went from branch to **merged in three minutes**.

**Release what you claim.** Remove the label when the PR merges, when you close
the issue, or when you stop — including when you stop because someone else got
there first. A claim you never release is worse than no claim, because the next
session believes it. Before skipping something because it is claimed, check how
old the claim is: no activity for over an hour probably means abandoned. Steal
it deliberately and say so in a comment; never steal a fresh one.

## 3. Commits

- **Conventional Commits** (`feat`, `fix`, `chore`, `docs`, `test`, `infra`,
  `security`, `refactor`, `perf`, `ci`), enforced by commitlint.
- Tag your commits with your agent's own `Co-Authored-By:` trailer.
- Keep commits and PRs **scoped to one concern**.

## 4. Pull requests

- Link the issue the PR resolves (`Closes #N`) and describe what changed and why.
- Mark a PR **ready** (not draft) when it is meant to merge.
- Behavior changes ship **with tests**. Don't reduce coverage to make CI pass.

## 5. Merging — never merge red

- Get CI green and confirm it: `gh pr checks <N>`. A green local run is not
  sufficient — verify the actual PR checks.
- **Wait with the CLI's `wait-for-checks`, not a hand-rolled loop:**

  ```bash
  sh scripts/biffo.sh wait-for-checks <N>   # 0 green · 1 failed · 2 cannot tell
  ```

  This repo no longer carries its own copy of the script. `scripts/biffo.sh`
  resolves the version-pinned Biffo CLI from `.biffo-shared-version` and runs
  the canonical copy that ships inside the package, so there is one script
  rather than one per repo (#1109).

  Do not write your own `until … grep -c pending … done`. That polls for the
  **absence** of pending checks, so the empty window right after
  `gh pr update-branch` — superseded runs dropped, new ones not yet registered —
  reads as "all green" and merges a PR whose CI has not started. The script
  waits on a **positive** signal instead, and its **exit 2 means "cannot tell",
  which is never a pass**.

- **Squash-merge, delete the branch, remove the worktree:**
  `gh pr merge <N> --squash --delete-branch`.

### If you were dispatched by another agent, you do NOT wait and you do NOT merge

Everything above assumes the session doing the work is also the session that
will merge it. **A delegated subagent is not that session.** If an orchestrator
spawned you to do one unit of work, your finished state is:

> branch pushed → PR opened → **report back immediately**, with the PR number
> and what you changed.

Do **not** run `wait-for-checks`. Do **not** merge. The orchestrator watches CI
and merges, and sends you back if CI fails on your change.

**Why this is a rule and not a preference.** A subagent has no way to sleep. Told
to wait for CI, it stops, wakes, re-reads the same pending status, and stops
again — and every wake is a full context reload that reports nothing new. Measured
on 2026-08-11: one dispatch tranche produced **25+ wake-ups** returning only
"still waiting"; a single agent burned ~145k tokens re-reading a queued deploy
before it was killed. The orchestrator does the same wait for nothing extra: it
is already awake, and one loop covers every PR in the tranche at once instead of
N agents each paying a context reload for the same minutes.

**The instruction is the cause, so the fix lives here.** Both of those incidents
happened in sessions whose dispatch briefs ended "get CI green, then squash-merge"
— written by an orchestrator that _had already recorded the lesson_ and then
followed §5 above, which says exactly that. §5 is right for the session that owns
the work and wrong for a delegate, and until now nothing said so.

**Keep a delegate alive only when it still has a DECISION to make** — a real test
failure to diagnose, a design question to answer. "Wait, then merge" is not a
decision.

Orchestrators: end every dispatch brief with the finished state above, and reach
for `gh pr merge --auto` when the merge needs no judgement — then still verify,
because `--auto` waits for ever on a check that cannot re-evaluate itself (a
`Release Guards` closing-keyword failure needs a re-run, not a wait).

## 6. Push honestly, and verify the remote has your commit

- **Push with the exit status visible:** `git push origin HEAD; echo $?`. A pipe
  (`git push | tail`) reports the pipe's status, not git's — a rejected push then
  reads as success and the commit is silently lost. Never trust a "pushed"
  message printed unconditionally after a pipe.
- **Confirm the remote actually has the commit before relying on it,** especially
  before merge: `git log origin/<branch> -1`. A green PR page is not proof your
  latest local commit reached it.

## 7. Verify post-merge

- A merge is not done when the PR closes. **Check the whole branch:**

  ```bash
  sh scripts/biffo.sh branch-health [-R owner/repo]   # 0 green · 1 red · 2 cannot tell
  ```

  It reports the latest run of **every** workflow on the integration branch, so
  the deploy cannot fall off the bottom of a short `gh run list` — and when
  something is red it names the **first** failing commit, not the newest.

- **When a check dies without a failure, adjudicate it before you re-run:**

  ```bash
  sh scripts/biffo.sh runner-drop-forensics --repo <owner/repo> --run <run-id>
  ```

  The self-hosted fleet runs on spot capacity, so a job can be killed mid-step
  and report `failure` with every remaining step at `conclusion: null` — which
  reads exactly like a real defect and sends you diagnosing your own change.
  This matches the run against the fleet's eviction record and tells you which
  it was. Of the 22 runner-killed jobs it was validated against, **17 were in
  satellites** — which is why it ships in the CLI rather than only upstream
  (#1240).

- **Check whether the branch was already failing before diagnosing your own
  change.** A red deploy has no audience: the author who broke it has moved on,
  and every later merge fails on damage it did not cause. On 2026-08-02 that
  cost 2h25m and four people each diagnosed their own innocent change.
- Exit 2 is "cannot tell" and is never a pass.

## 8. Creating this repo correctly (read once, at birth)

**No `biffo` command creates a standalone plugin repo.** `biffo plugin create`
scaffolds a plugin _into an existing checkout_; a repo like this one is made by
hand. So the governance that `biffo init` and `biffo sibling create` apply
automatically is **not applied here**, and has to be set deliberately.

That gap is not theoretical: both existing plugin repos ran with **no branch
protection at all**, and `biffo-plugin-ideation#54` was merged with both CI jobs
still in progress because nothing stopped it (biffo-template#714).

After creating the repo and pushing this skeleton:

```bash
# 1. Auto-merge must be ON, or `gh pr merge --auto` merges IMMEDIATELY
#    rather than queuing — see the warning below.
gh api -X PATCH repos/<org>/<repo> -f allow_auto_merge=true -f delete_branch_on_merge=true

# 2. Protect dev, deriving the required checks from what CI actually reports.
#    Run it once CI has gone green at least twice, so there is something to derive from.
biffo check branch-protection --repo <org>/<repo> --fix

# 3. Confirm it took.
biffo check branch-protection --repo <org>/<repo>
```

> **`--auto` is not a safety net on an unconfigured repo.** With
> `allow_auto_merge` disabled, `gh pr merge --squash --auto` does not queue — it
> merges _now_ if the PR is mergeable at that instant. On an unprotected branch
> that means merging with checks still running. Steps 1 and 2 together are what
> make the documented flow behave as documented; either alone is not enough.

## 9. CI runners — two steps this repo cannot do for itself

The workflows use `runs-on: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}`, so they
work anywhere by default and route to a self-hosted fleet when one exists. Two
things must be done **outside** this repo before its first run can succeed on a
fleet:

1. **Set the repo variable `RUNNER_LABEL`** to the fleet's label.
2. **Grant the runner GitHub App access to this repo.** The scale-up webhook only
   sees repos the App can see. Without the grant, jobs queue **indefinitely with
   no error** — and nothing in the UI distinguishes that from a slow runner. The
   first plugin repo to hit this sat queued for **1h 44m** before anyone noticed
   (`keiranholloway/biffo-runners#2`).

If a job is queued and nothing is happening, check the grant before anything
else. It is the failure that looks exactly like patience.

## 10. Security

- **Never commit secrets** (keys, tokens, credentials, `.env` values).
- **Never silently disable a security gate.** If one must be loosened, do it in
  the open and raise a tracking issue to restore it.
