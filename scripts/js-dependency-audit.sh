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

attempts=3
inconclusive=0
failed=0

# Audit one directory. Returns 0 if clean or inconclusive, 1 on a real finding.
# `$1` is the directory, `$2` a human label, `$3` extra pnpm flags.
audit_dir() {
  dir="$1"
  label="$2"
  extra="$3"

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
    out="$(cd "$dir" 2>/dev/null && pnpm audit --json $extra 2>/dev/null)"
    # Stamped the instant the registry answered, not when the run started.
    # `pnpm audit` asks the LIVE registry, so its verdict is a function of what
    # had been ingested at this moment — two runs of the same tree minutes apart
    # can legitimately disagree (#1269). Without this stamp a green is not
    # falsifiable, and a red appearing hours after a merge reads as "someone
    # broke dev" when nothing in the tree moved.
    seen_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if printf '%s' "$out" | jq -e '.metadata.vulnerabilities' >/dev/null 2>&1; then
      high="$(printf '%s' "$out" | jq '.metadata.vulnerabilities.high // 0')"
      crit="$(printf '%s' "$out" | jq '.metadata.vulnerabilities.critical // 0')"
      mod="$(printf '%s' "$out" | jq '.metadata.vulnerabilities.moderate // 0')"
      low="$(printf '%s' "$out" | jq '.metadata.vulnerabilities.low // 0')"
      total="$(printf '%s' "$out" | jq '.metadata.totalDependencies // 0')"
      if [ "$((high + crit))" -gt 0 ]; then
        echo "::error::${label}: ${crit} critical + ${high} high advisory(ies) across ${total} package(s); registry answered ${seen_at}."
        printf '%s' "$out" | jq '.advisories // .metadata.vulnerabilities' 2>/dev/null | head -c 4000
        return 1
      fi
      # A bare "no advisories" is not falsifiable. State the population, the
      # severities that did NOT block, and when the registry was asked, so a
      # reader can tell a clean tree from a tree nobody looked at properly.
      echo "${label}: 0 critical, 0 high across ${total} package(s) (${mod} moderate, ${low} low — reported, not blocking); registry answered ${seen_at}."
      return 0
    fi

    msg="$(printf '%s' "$out" | jq -r '.error.message // "no parseable audit output"' 2>/dev/null | head -c 200)"
    echo "${label}: attempt ${attempt}/${attempts} could not run: ${msg}"
    [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 3))"
  done

  echo "::error::${label}: audit could not run after ${attempts} attempts (the registry returned a non-JSON/error response). Advisory scanning was NOT performed for this tree, so this is INCONCLUSIVE and BLOCKS — a gate that cannot see its input must not report clean (#1269, #591)."
  inconclusive=$((inconclusive + 1))
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

# Audit each discovered tree. The workspace's own lockfile is audited WITHOUT
# --ignore-workspace, so pnpm resolves it normally; every other discovered
# lockfile is a separate, vendored project and needs the flag, or pnpm walks
# up, finds the workspace, and silently audits THAT instead — reporting clean
# for a tree it never looked at, which is this exact defect one level down.
for lock in $ALL_LOCKS; do
  dir=$(dirname "$lock")
  dir_abs=$(cd "$dir" 2>/dev/null && pwd -P)
  case "$dir_abs" in
    "$REPO_ROOT") rel=. ;;
    "$REPO_ROOT"/*) rel=${dir_abs#"$REPO_ROOT"/} ;;
    *) rel="$dir_abs" ;;
  esac
  if [ "$dir_abs" = "$WORKSPACE_ABS" ]; then
    audit_dir "$dir" "pnpm audit (workspace: ${rel})" "" || failed=1
  else
    audit_dir "$dir" "pnpm audit (${rel})" "--ignore-workspace" || failed=1
  fi
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
