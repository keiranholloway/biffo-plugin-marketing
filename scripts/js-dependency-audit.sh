#!/bin/sh
#
# JS dependency audit that fails the build on a real high/critical advisory but
# NOT on a broken audit registry (#591).
#
# `pnpm audit --audit-level=high` exits non-zero identically whether it found a
# vulnerability OR simply couldn't parse the registry's response — and the npm
# audit endpoint intermittently returns a non-JSON/gzip body pnpm chokes on
# ("Unexpected token, is not valid JSON"), which then fails the required JS check
# on every open PR at once, blocking the whole merge queue for an infrastructure
# hiccup that has nothing to do with the PR.
#
# `pnpm audit --json` disambiguates: a real run yields `.metadata.vulnerabilities`
# (counts by severity); a registry/parse failure yields `.error`. So we fail only
# on a genuine high+critical finding, retry a transient registry error a few
# times, and treat a persistent one as INCONCLUSIVE (warn, don't block) rather
# than as a vulnerability. "Couldn't run the check" and "the check found a
# problem" must not be the same signal on a required gate.
#
# ## Skeleton lockfiles are audited too (#644)
#
# `_skeletons/**/` are scaffolding trees: `biffo sibling create` / `plugin
# create` copy them verbatim into a brand-new repo. They sit OUTSIDE the pnpm
# workspace, so `pnpm install` never installs them and a workspace-scoped audit
# never sees them — which meant every sibling scaffolded from this template was
# born with four high-severity advisories nobody could see. Dependabot found
# them; the required JS gate could not, by construction.
#
# That is the fail-open shape this file already exists to fight, one level up:
# the gate was green because it was not looking, and "we checked and it is
# clean" read identically to "we never checked".
#
# ## Discovery, not a fixed path list (#1270)
#
# A fixed sweep of "workspace root + `_skeletons/**`" is itself the same shape
# of gap, one level further down: it reports clean on any pnpm-lock.yaml
# outside that pair, and an INSTANCE grows trees this repo never has —
# vendored plugin frontends under `services/*/web/`, a second app under
# `web-admin/`, or any other nested pnpm project. Found live:
# `services/ideation/web/pnpm-lock.yaml` sat on two advisory-range packages
# (one high) while this exact gate stayed green, because it never looked
# there. Ten lockfiles in that repo; a fixed sweep covered nine.
#
# So every pnpm-lock.yaml under the repo is DISCOVERED by walking from the git
# root, rather than assumed from a hardcoded list — see "Discover the trees"
# below. Nothing here still means the estate's layouts differ; discovery
# covers a repo the day it grows a new one, instead of the day someone
# remembers to add a path.
#
# ## This file is distributed verbatim (#743)
#
# Both skeletons and every existing sibling/plugin repo hold a BYTE-IDENTICAL
# copy, listed in `shared-files.json` and pushed by `scripts/shared-sync.sh`.
# Until then the hardening above lived in this repo only, while both skeletons
# shipped the raw `pnpm audit --audit-level=high` that #591 was filed about — so
# every satellite was born with the original defect and reddened its required JS
# check on any registry hiccup.
#
# It therefore takes its WORKSPACE tree from the CURRENT WORKING DIRECTORY —
# the CI job's `working-directory` — rather than assuming a repo layout: `.` is
# the root workspace here, `apps/frontend` in a sibling. Keep it layout-agnostic,
# or the copies stop being interchangeable and `shared-sync.sh --check` starts
# reporting drift that is really divergence.
#
# ## Pre-existing vs introduced (#2040)
#
# Every check above answers "can this audit be trusted" (network flake vs a
# real finding). None of them answer WHEN the vulnerable version got there —
# so a genuine high/critical advisory published overnight against a package
# already sitting on `dev`, untouched by this diff, used to read identically
# to a PR that actually introduced the vulnerable version. #1880 (2026-09-04)
# tried to paper over exactly that by moving this whole check to a
# `continue-on-error`, non-required job — which stopped a real per-PR finding
# from blocking too, and produced ten separate biffo-fleet tickets and $86.72
# of dispatch spend for two routine advisories that no PR under test could
# have fixed (#2040's own cost accounting). `py-dependency-audit.sh` closed
# this same gap for Python in #1673; this is the JS side of the same fix.
#
# The fix compares the ADVISORY IDS found against the SAME tree's pnpm audit
# run against the PR's BASE branch, not against the diff itself: a PR can
# leave pnpm-lock.yaml untouched while a sibling change moves a transitive
# version, or the base branch may already carry the flagged version. An
# advisory ID present in both base and head is pre-existing and does not
# block; an advisory ID present only in head is introduced by this diff (a
# new dependency, or an upgrade/downgrade into a vulnerable version) and
# blocks exactly as before. This mirrors the issue's own "run against base
# and head, diff the advisory IDs, fail on head-only" design rather than
# hand-parsing pnpm-lock.yaml's YAML — `pnpm audit` already works against a
# bare copy of just the lockfile with no install (verified directly against
# this repo's own pnpm 9.15.9), so the base side is a second real audit
# call, not a reimplementation of what `pnpm audit` already does.
#
# `GITHUB_BASE_REF` is the base branch name and is set ONLY for a
# `pull_request`/`pull_request_target` event — empty on `push`,
# `workflow_dispatch` and `merge_group`. That is deliberate, not a gap:
# outside a PR there is no "diff" to attribute a finding to, so every finding
# blocks exactly as before (the correct behaviour for `dev` itself, and for
# the scheduled base-branch scan added alongside this file in #2040, which
# wants every current finding reported, not just new ones).
#
# `origin/$GITHUB_BASE_REF` must already be a resolvable local ref for the
# comparison to run at all — the `js` job's checkout uses `fetch-depth: 0`
# precisely so it is (see that job's own comment). If it is NOT resolvable
# (a shallow checkout, or a distributed copy of this script running
# somewhere that checkout is missing), comparison fails CLOSED: every
# finding blocks, same as before this fix, rather than silently waving a
# real regression through because the check could not be performed.
#
# POSIX sh (the CI step runs `sh scripts/...`, i.e. dash) — no `pipefail`.
set -u

# jq is the parser this whole distinction rests on. Without it EVERY response
# reads as unparseable, so the script reports INCONCLUSIVE on every invocation
# and the gate goes green while scanning nothing — the exact fail-open shape it
# exists to prevent, and precisely what the dash `echo` defect did until #717. A
# missing jq is a deterministic environment defect, not a transient registry
# hiccup, so it fails loudly instead of degrading into a permanent pass.
if ! command -v jq >/dev/null 2>&1; then
  echo "::error::jq is not installed on this runner. The dependency audit cannot tell a clean run from an unparseable one without it, and would report INCONCLUSIVE forever — a gate that is green because it never ran. Install jq."
  exit 1
fi

# `timeout` bounds each `pnpm audit` call (#1878). Without it, a registry that
# TCP-hangs rather than erroring makes a single attempt block for however long
# pnpm's own internal retry/timeout budget takes — observed at ~4 minutes per
# attempt against a healthy-run baseline of ~2 seconds for all discovered
# trees combined. Multiplied by this script's own 3-attempt retry loop across
# every discovered lockfile tree, that reaches ~36 minutes worst case, blowing
# through the CI job's 20-minute cap and getting the job CANCELLED — a worse
# outcome than this script's own designed INCONCLUSIVE-and-block (exit 2),
# because a cancelled job prints no actionable error. Missing `timeout` is a
# deterministic environment defect like missing jq above, not a transient
# hiccup, so it fails loudly rather than silently reverting to unbounded waits.
if ! command -v timeout >/dev/null 2>&1; then
  echo "::error::timeout (coreutils) is not installed on this runner. The dependency audit cannot bound a hung registry call without it. Install coreutils."
  exit 1
fi

# Generous relative to a healthy run (~2s for every tree combined) but short
# enough that 3 attempts × every discovered tree stays well inside the job's
# budget. Overridable for local debugging against a known-slow network.
AUDIT_TIMEOUT_SECS="${AUDIT_TIMEOUT_SECS:-20}"

attempts=3
inconclusive=0
failed=0

# Audit one directory. Returns 0 if clean or inconclusive, 1 on a real finding,
# AND writes a one-word verdict (`ok` / `fail` / `inconclusive`) to `$4`.
#
# The verdict file exists because this function is invoked backgrounded (`&`,
# see the audit loop below) so the four (or however many) trees run in
# parallel rather than paying their registry round-trip one after another
# (#1874: 4 trees serially cost 15m21s in a real CI run and blew the job's
# 20-minute cap). A backgrounded call is a forked subshell — every variable it
# touches, including `inconclusive`/`failed` below, is a copy in that child
# process and vanishes when it exits. The return code has the same problem: a
# background job's exit status is only visible via `wait "$pid"`, one pid at a
# time, which is no simpler than a file and less robust (a killed/never-run
# job leaves nothing to wait on). So each invocation reports for itself, in
# writing, and the parent tallies the results after `wait` once every
# invocation has finished.
#
# `$1` is the directory, `$2` a human label, `$3` extra pnpm flags, `$4` the
# result file this invocation must write its verdict to, `$5` this tree's
# pnpm-lock.yaml path relative to the repo root — used to look up the SAME
# lockfile's advisories on the base branch (#2040) — never empty, every
# caller passes one.
#
# Returns (on stdout) the set of advisory IDs recorded against the SAME
# lockfile path on the PR's base branch, one per line — any severity, not
# just high/critical, because presence is all classification needs. Prints
# nothing and returns non-zero when a comparison genuinely cannot be made
# (network/parse failure, a temp-dir failure) — the caller then treats every
# finding as introduced, the same fail-closed default #1673 established for
# Python. An empty base lockfile (the path did not exist on the base branch
# at all — a brand-new lockfile or a brand-new vendored tree this diff
# itself introduced) is NOT a failure: it prints nothing and returns 0,
# because an empty set is the correct answer — there is nothing to be
# pre-existing against, so every finding in a tree the base never had is
# introduced by definition.
_base_advisory_ids() {
  lock_rel="$1"

  base_content="$(git show "${BASE_REMOTE_REF}:${lock_rel}" 2>/dev/null)"
  if [ -z "$base_content" ]; then
    return 0
  fi

  workdir=$(mktemp -d "${TMPDIR:-/tmp}/js-dependency-audit-base.XXXXXX") || return 1
  printf '%s' "$base_content" >"$workdir/pnpm-lock.yaml"

  for attempt in $(seq 1 "$attempts"); do
    # shellcheck disable=SC2086
    base_out="$(cd "$workdir" && timeout "$AUDIT_TIMEOUT_SECS" pnpm audit --json --ignore-workspace 2>/dev/null)"
    base_status=$?
    if [ "$base_status" -eq 124 ]; then
      [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 2))"
      continue
    fi
    if printf '%s' "$base_out" | jq -e '.metadata.vulnerabilities' >/dev/null 2>&1; then
      printf '%s' "$base_out" | jq -r '.advisories[]? | (.github_advisory_id // (.id|tostring))' 2>/dev/null
      rm -rf "$workdir"
      return 0
    fi
    [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 2))"
  done

  rm -rf "$workdir"
  return 1
}

audit_dir() {
  dir="$1"
  label="$2"
  extra="$3"
  resultfile="$4"
  lock_rel="$5"

  for attempt in $(seq 1 "$attempts"); do
    # printf, never echo: the CI step runs `sh scripts/...` i.e. dash, whose
    # `echo` interprets backslash escapes. Advisory payloads contain them, so
    # `echo "$out" | jq` mangles the JSON and jq rejects it — the run then reads
    # as "the registry returned junk" and reports INCONCLUSIVE. That is this
    # gate failing open inside the very fix that exists to stop it failing open
    # (#591): green, every time, while scanning nothing.
    #
    # The flag differs by tree, and both directions matter:
    #   - skeletons need --ignore-workspace, or pnpm walks up, finds this repo's
    #     workspace root and audits THAT instead — reporting a clean result for
    #     a tree it never looked at, which is the failure this step closes.
    #   - the workspace must NOT have it, or pnpm has no project to audit and
    #     every run reports INCONCLUSIVE. That fails open: the gate goes green
    #     forever while scanning nothing. (Caught by running this script before
    #     trusting it — the workspace audit had silently stopped working.)
    # shellcheck disable=SC2086
    out="$(cd "$dir" 2>/dev/null && timeout "$AUDIT_TIMEOUT_SECS" pnpm audit --json $extra 2>/dev/null)"
    audit_status=$?
    # Stamped the instant the registry answered, not when the run started.
    # `pnpm audit` asks the LIVE registry, so its verdict is a function of what
    # had been ingested at this moment — two runs of the same tree minutes apart
    # can legitimately disagree (#1269). Without this stamp a green is not
    # falsifiable, and a red appearing hours after a merge reads as "someone
    # broke dev" when nothing in the tree moved.
    seen_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # `timeout` exits 124 when it had to kill the process rather than the
    # process exiting on its own. Handled before the jq parse below: a killed
    # `pnpm audit` produces empty/partial output that would already fall
    # through to "could not run", but naming the timeout explicitly here keeps
    # that failure distinguishable from a genuine registry parse error rather
    # than silently blank, and — same contract as any other "could not run" —
    # it is never treated as a clean/success result.
    if [ "$audit_status" -eq 124 ]; then
      echo "${label}: attempt ${attempt}/${attempts} could not run: timed out after ${AUDIT_TIMEOUT_SECS}s waiting on the registry"
      [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 3))"
      continue
    fi

    if printf '%s' "$out" | jq -e '.metadata.vulnerabilities' >/dev/null 2>&1; then
      # Tally from the advisories that REMAIN in the output when present:
      # pnpm applies `auditConfig.ignoreGhsas` to `.advisories` but leaves
      # `.metadata.vulnerabilities` counting the ignored ones, so trusting the
      # metadata reports suppressed (unfixable) advisories as blocking.
      # Fall back to the metadata when no advisories object is emitted.
      _cnt() { printf '%s' "$out" | jq --arg s "$1" 'if (.advisories|type)=="object" then [.advisories[]|select(.severity==$s)]|length else (.metadata.vulnerabilities[$s] // 0) end'; }
      high="$(_cnt high)"
      crit="$(_cnt critical)"
      mod="$(_cnt moderate)"
      low="$(_cnt low)"
      total="$(printf '%s' "$out" | jq '.metadata.totalDependencies // 0')"
      if [ "$((high + crit))" -gt 0 ]; then
        # Classify each qualifying (high/critical) advisory against the base
        # branch's SAME lockfile before deciding to block (#2040). Written to
        # a regular file, not a pipe, and read with `while read ... done <
        # file` rather than `| while read`, for the same reason
        # py-dependency-audit.sh's findings loop does: a pipeline runs the
        # loop in a subshell, and a subshell's counter updates vanish the
        # instant it exits.
        findings_file="$(mktemp)"
        printf '%s' "$out" | jq -r '.advisories[]? | select(.severity=="high" or .severity=="critical") | "\(.github_advisory_id // (.id|tostring))\t\(.severity)\t\(.module_name)"' >"$findings_file"

        base_ids_file=""
        base_available=0
        if [ "$COMPARE_MODE" -eq 1 ]; then
          candidate_ids="$(mktemp)"
          if _base_advisory_ids "$lock_rel" >"$candidate_ids" 2>/dev/null; then
            base_ids_file="$candidate_ids"
            base_available=1
          else
            rm -f "$candidate_ids"
          fi
        fi

        introduced_count=0
        preexisting_count=0
        while IFS="$(printf '\t')" read -r f_id f_sev f_mod; do
          [ -z "$f_id" ] && continue
          if [ "$base_available" -eq 1 ] && grep -qxF "$f_id" "$base_ids_file" 2>/dev/null; then
            preexisting_count=$((preexisting_count + 1))
            echo "::warning::${label}: ${f_mod} advisory ${f_id} (${f_sev}) is already present in ${BASE_REMOTE_REF}'s lockfile — pre-existing, not introduced by this diff (#2040)."
          else
            introduced_count=$((introduced_count + 1))
            echo "::error::${label}: ${f_mod} advisory ${f_id} (${f_sev}) — new to this tree, or a version this diff introduced/upgraded (or a base-branch comparison was not possible)."
          fi
        done <"$findings_file"
        rm -f "$findings_file"
        [ -n "$base_ids_file" ] && rm -f "$base_ids_file"

        printf '%s' "$out" | jq '.advisories // .metadata.vulnerabilities' 2>/dev/null | head -c 4000

        if [ "$introduced_count" -gt 0 ]; then
          echo "::error::${label}: ${introduced_count} critical/high advisory(ies) introduced or upgraded by this diff across ${total} package(s) (${preexisting_count} more pre-existing, not counted against it); registry answered ${seen_at}."
          echo "fail" >"$resultfile"
          return 1
        fi

        echo "${label}: ${preexisting_count} critical/high advisory(ies) found, all pre-existing on ${BASE_REMOTE_REF:-the base branch} and unrelated to this diff — not blocking (#2040); registry answered ${seen_at}."
        echo "ok" >"$resultfile"
        return 0
      fi
      # A bare "no advisories" is not falsifiable. State the population, the
      # severities that did NOT block, and when the registry was asked, so a
      # reader can tell a clean tree from a tree nobody looked at properly.
      echo "${label}: 0 critical, 0 high across ${total} package(s) (${mod} moderate, ${low} low — reported, not blocking); registry answered ${seen_at}."
      echo "ok" >"$resultfile"
      return 0
    fi

    msg="$(printf '%s' "$out" | jq -r '.error.message // "no parseable audit output"' 2>/dev/null | head -c 200)"
    echo "${label}: attempt ${attempt}/${attempts} could not run: ${msg}"
    [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 3))"
  done

  echo "::error::${label}: audit could not run after ${attempts} attempts (the registry returned a non-JSON/error response). Advisory scanning was NOT performed for this tree, so this is INCONCLUSIVE and BLOCKS — a gate that cannot see its input must not report clean (#1269, #591)."
  echo "inconclusive" >"$resultfile"
  return 0
}

# ## Discover the trees (#1270)
#
# Walk from the git root — not the CWD this script happens to run from — so a
# vendored tree anywhere in the repo is found regardless of where the CI job's
# `working-directory` points. Excludes `node_modules` (installed, not source),
# `.git`, and `.worktrees` (other agents' in-progress checkouts, git-ignored
# and not part of this run).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  echo "::error::js-dependency-audit: not inside a git repository ('git rev-parse --show-toplevel' failed). Discovery cannot walk a tree it cannot find." >&2
  exit 2
fi

WORKSPACE_ABS=$(pwd -P)

# Decide once, for the whole run, whether a base-branch comparison is even
# possible (#2040 — see the "Pre-existing vs introduced" docstring above
# `set -u`). `GITHUB_BASE_REF` is only ever set by a
# `pull_request`/`pull_request_target` event; everywhere else COMPARE_MODE
# stays 0 and every finding blocks, unchanged from before this fix.
BASE_REMOTE_REF=""
COMPARE_MODE=0
if [ -n "${GITHUB_BASE_REF:-}" ]; then
  candidate="origin/${GITHUB_BASE_REF}"
  if git rev-parse --verify --quiet "${candidate}^{commit}" >/dev/null 2>&1; then
    BASE_REMOTE_REF="$candidate"
    COMPARE_MODE=1
  else
    echo "::warning::js-dependency-audit: base branch is '${GITHUB_BASE_REF}' but '${candidate}' does not resolve locally (shallow checkout?) — cannot tell a pre-existing finding from one this diff introduced, so every finding will block, same as before #2040."
  fi
fi

# shellcheck disable=SC2016
ALL_LOCKS=$(find "$REPO_ROOT" \
  \( -name node_modules -o -name .git -o -name .worktrees \) -prune -o \
  -type f -name pnpm-lock.yaml -print 2>/dev/null | sort)

# Fail CLOSED, not open, on an empty discovery (#1270). Zero trees — including
# no lockfile at the workspace root itself — means discovery is broken (a
# wrong root, an over-eager prune, a repo layout this script has never seen),
# not that the repo has nothing to audit. Every repo this script is wired into
# ships at least one pnpm-lock.yaml, so an empty result here is a configuration
# error, not a clean pass — the same posture as `verify.sh`'s `ci_has()`
# (#1218) and `ci-wiring-audit.sh`'s empty-map check: "This audit checked
# nothing. That is a configuration error, not a pass." Exit 2 (not 1), the
# estate's "cannot tell" code, distinct from "found a vulnerability" (1) and
# "clean" (0) — CI treats any non-zero as a failed step either way.
if [ -z "$ALL_LOCKS" ]; then
  printf '::error::js-dependency-audit: discovered ZERO pnpm-lock.yaml trees under %s.\n' "$REPO_ROOT" >&2
  printf 'This audit checked nothing. That is a configuration error, not a pass — see #1270.\n' >&2
  exit 2
fi

# Print what was scanned BEFORE auditing, so the list survives even if a later
# tree crashes the run — a bare green must be a falsifiable claim, not
# something a reader has to infer or re-enumerate by hand.
tree_count=$(printf '%s\n' "$ALL_LOCKS" | wc -l | tr -d ' ')
printf 'js-dependency-audit: discovered %s pnpm-lock.yaml tree(s):\n' "$tree_count"
for lock in $ALL_LOCKS; do
  dir=$(dirname "$lock")
  dir_abs=$(cd "$dir" 2>/dev/null && pwd -P)
  case "$dir_abs" in
    "$REPO_ROOT") rel=. ;;
    "$REPO_ROOT"/*) rel=${dir_abs#"$REPO_ROOT"/} ;;
    *) rel="$dir_abs" ;;
  esac
  if [ "$dir_abs" = "$WORKSPACE_ABS" ]; then
    printf '  - %s (workspace)\n' "$rel"
  else
    printf '  - %s\n' "$rel"
  fi
done

# Audit each discovered tree IN PARALLEL (#1874). Each `audit_dir` call is a
# real network round-trip, with its own retry/backoff, to registry.npmjs.org
# — run one after another they cost roughly N x the slowest single tree (a
# real CI run measured 4 trees / 15m21s, blowing the job's 20-minute cap
# mid-way through ~9 other required guard steps). Backgrounding them lets the
# round-trips overlap instead.
#
# A one-word-per-tree result file (see `audit_dir` above) is how the parent
# shell learns each backgrounded verdict back: `$TMP_DIR` is created via
# `mktemp -d` (collision-safe by construction — no need to hand-roll a `$$`
# suffix on top of it) and torn down by the EXIT/HUP/INT/TERM trap below
# whichever way this script leaves.
#
# The workspace's own lockfile is audited WITHOUT --ignore-workspace, so pnpm
# resolves it normally; every other discovered lockfile is a separate,
# vendored project and needs the flag, or pnpm walks up, finds the workspace,
# and silently audits THAT instead — reporting clean for a tree it never
# looked at, which is this exact defect one level down.
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/js-dependency-audit.XXXXXX") || {
  echo "::error::js-dependency-audit: could not create a temp directory for parallel results (mktemp failed)." >&2
  exit 2
}
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

i=0
for lock in $ALL_LOCKS; do
  dir=$(dirname "$lock")
  dir_abs=$(cd "$dir" 2>/dev/null && pwd -P)
  case "$dir_abs" in
    "$REPO_ROOT") rel=. ;;
    "$REPO_ROOT"/*) rel=${dir_abs#"$REPO_ROOT"/} ;;
    *) rel="$dir_abs" ;;
  esac
  i=$((i + 1))
  resultfile="$TMP_DIR/result.$i"
  # `find` was rooted at $REPO_ROOT, so $lock is always an absolute path
  # under it — this strip is unconditional, unlike $rel's dir_abs case above
  # (which also has to tolerate a symlink resolving outside the root).
  lock_rel=${lock#"$REPO_ROOT"/}
  if [ "$dir_abs" = "$WORKSPACE_ABS" ]; then
    audit_dir "$dir" "pnpm audit (workspace: ${rel})" "" "$resultfile" "$lock_rel" &
  else
    audit_dir "$dir" "pnpm audit (${rel})" "--ignore-workspace" "$resultfile" "$lock_rel" &
  fi
done

# Wait for every backgrounded audit_dir before reading any result file back —
# reading early would race a tree that is still auditing.
wait

# Tally the verdicts the backgrounded invocations wrote for themselves. A
# result file that is missing or unreadable (the subshell was killed before
# it could write, or never started) fails CLOSED as inconclusive rather than
# being silently skipped — the same posture as the empty-discovery check
# above: a tree this run cannot account for is not a clean tree.
i=0
for lock in $ALL_LOCKS; do
  i=$((i + 1))
  resultfile="$TMP_DIR/result.$i"
  verdict=$(cat "$resultfile" 2>/dev/null)
  case "$verdict" in
    ok) ;;
    fail) failed=1 ;;
    inconclusive) inconclusive=$((inconclusive + 1)) ;;
    *)
      lock_dir=$(dirname "$lock")
      echo "::error::js-dependency-audit: no verdict recorded for ${lock_dir} (expected ok/fail/inconclusive in ${resultfile}). Treating as inconclusive."
      inconclusive=$((inconclusive + 1))
      ;;
  esac
done

if [ "$failed" -ne 0 ]; then
  exit 1
fi

if [ "$inconclusive" -ne 0 ]; then
  # Exit 2, not 1: "could not determine" is a different fact from "found a real
  # advisory", and conflating them sends whoever reads the red looking for a
  # vulnerability that may not exist. Same three-valued contract as
  # scripts/claim.sh (0 free / 1 taken / 2 cannot tell) and the zero-trees exit
  # above. Until #1269 this returned 0 -- biffo-plugin-ideation rode that path
  # on EVERY run, permanently green while scanning nothing.
  echo "::error::${inconclusive} tree(s) could not be audited; see the errors above. Failing closed."
  exit 2
fi

echo "js-dependency-audit: audited ${tree_count} tree(s), 0 blocking findings."
exit 0
