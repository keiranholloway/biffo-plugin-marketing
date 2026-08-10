#!/bin/sh
#
# Python dependency audit that fails the build on a real advisory but NOT on a
# broken registry/network response (#591) — same class of flake as the JS
# dependency audit (scripts/js-dependency-audit.sh), applied preventively here
# since pip-audit queries PyPI/OSV over the network per dependency and is
# subject to the same kind of transient hiccup.
#
# `pip-audit -f json` emits a `{"dependencies": [{"name", "version", "vulns"},
# ...]}` shape on a real run; a network/parse failure produces no parseable
# JSON at all (traceback on stderr, non-zero exit, empty/partial stdout). So we
# fail only when the parsed output actually contains a vulnerability, retry a
# few times on an unparseable run, and treat a persistent failure as
# INCONCLUSIVE (warn, don't block) rather than as a vulnerability.
# "Couldn't run the check" and "the check found a problem" must not be the same
# signal on a required gate.
#
# ## Skeleton lockfiles are audited too (#719)
#
# `_skeletons/**/` are scaffolding trees: `biffo sibling create` / `plugin
# create` copy them verbatim into a brand-new repo. They are NOT part of this
# repo's uv project, so `uv sync` never installs them and a workspace-scoped
# `uv run pip-audit` never saw them — which meant the Python half of every
# scaffolded sibling came from a lockfile no CI gate had ever looked at.
#
# #644 closed exactly this hole for the JS skeleton lockfile, where it had
# already cost four unseen high-severity advisories; this is the same hole one
# language over. It is the fail-open shape this file already exists to fight,
# one level up: the gate was green because it was not looking, and "we checked
# and it is clean" read identically to "we never checked".
#
# Unlike `pnpm audit --ignore-workspace`, pip-audit cannot simply be run inside
# a nested directory: it is not an installed environment, and pip-audit's
# default mode audits an environment. So we export `--frozen` requirements from
# the nested lockfile (a read of the committed lock, no re-resolution) and
# audit those with `-r`, from the workspace, which is where pip-audit is
# actually installed.
#
# ## Discovery, not a fixed path list (#1270)
#
# A fixed sweep of "workspace + `_skeletons/**`" is the same shape of gap one
# level further down: it reports clean on any uv.lock outside that pair, and an
# INSTANCE grows trees this repo never has — vendored plugin services such as
# `services/idea-scout/uv.lock`. Found live: that lockfile and
# `services/ideation/uv.lock` both sat on `cryptography@49.0.0` (CVE-2026-69247)
# while this gate stayed green, because it never looked there.
#
# So every uv.lock under the repo is DISCOVERED by walking from the git root,
# rather than assumed from a hardcoded list — see "Discover the trees" below.
#
# ## This file is distributed verbatim (#743)
#
# Both skeletons and every existing sibling/plugin repo hold a BYTE-IDENTICAL
# copy, listed in `shared-files.json` and pushed by `scripts/shared-sync.sh`.
# Until then the hardening above lived in this repo only, while both skeletons
# shipped a raw `uv run pip-audit` — so every satellite was born with the
# original defect and reddened a required check on any PyPI/OSV hiccup.
#
# It therefore takes its WORKSPACE tree from the CURRENT WORKING DIRECTORY —
# the CI job's `working-directory` — rather than assuming a repo layout: `.` is
# the root project here and in a plugin repo, `services/api` in a sibling. Keep
# it layout-agnostic, or the copies stop being interchangeable and
# `shared-sync.sh --check` starts reporting drift that is really divergence.
#
# POSIX sh (the CI step runs `sh scripts/...`, i.e. dash) — no `pipefail`.
set -u

# jq is the parser this whole distinction rests on. Without it EVERY response
# reads as unparseable, so the script reports INCONCLUSIVE on every invocation
# and the gate goes green while scanning nothing — the exact fail-open shape it
# exists to prevent, and precisely what the dash `echo` defect did until #717. A
# missing jq is a deterministic environment defect, not a transient network
# hiccup, so it fails loudly instead of degrading into a permanent pass.
if ! command -v jq >/dev/null 2>&1; then
  echo "::error::jq is not installed on this runner. The dependency audit cannot tell a clean run from an unparseable one without it, and would report INCONCLUSIVE forever — a gate that is green because it never ran. Install jq."
  exit 1
fi

attempts=3
inconclusive=0
failed=0

# Audit one dependency set. Returns 0 if clean or inconclusive, 1 on a real
# finding. `$1` is a human label, `$2` the extra pip-audit flags that select
# what to audit — empty for this workspace's installed environment, or
# `-r <file>` for a requirements file exported from a skeleton lockfile.
audit_deps() {
  label="$1"
  extra="$2"

  for attempt in $(seq 1 "$attempts"); do
    # shellcheck disable=SC2086
    out="$(uv run pip-audit -f json $extra 2>/dev/null)"
    # Stamped when the advisory source answered, not when the run started —
    # pip-audit queries PyPI/OSV live, so its verdict is time-dependent in the
    # same way pnpm audit's is (#1269). Without the stamp a pass cannot be
    # distinguished from a pass taken before an advisory was published.
    seen_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # printf, never echo: the CI step runs `sh scripts/...` i.e. dash, whose
    # `echo` interprets backslash escapes. Advisory payloads contain them, so
    # `echo "$out" | jq` mangles the JSON and jq rejects it — the run then reads
    # as "the registry returned junk" and reports INCONCLUSIVE. That is this
    # gate failing open inside the very fix that exists to stop it failing open
    # (#591): green, every time, while scanning nothing. #644 found the JS gate
    # had been doing exactly that on every run.
    if printf '%s' "$out" | jq -e '.dependencies' >/dev/null 2>&1; then
      vuln_count="$(printf '%s' "$out" | jq '[.dependencies[].vulns[]?] | length')"
      pkg_count="$(printf '%s' "$out" | jq '.dependencies | length')"
      if [ "$vuln_count" -gt 0 ]; then
        echo "::error::${label}: ${vuln_count} vulnerability(ies)."
        printf '%s' "$out" | jq '[.dependencies[] | select(.vulns | length > 0)]' 2>/dev/null | head -c 4000
        return 1
      fi
      # State the population and the moment, so the green is falsifiable: a
      # pass over 0 packages and a pass over 92 look identical otherwise, and
      # the first is the failure this whole file exists to prevent.
      echo "${label}: 0 vulnerabilities across ${pkg_count} package(s); advisory source answered ${seen_at}."
      return 0
    fi

    echo "${label}: attempt ${attempt}/${attempts} produced no parseable JSON output."
    [ "$attempt" -lt "$attempts" ] && sleep "$((attempt * 3))"
  done

  echo "::error::${label}: could not produce parseable output after ${attempts} attempts. Advisory scanning was NOT performed for this tree, so this is INCONCLUSIVE and BLOCKS — a gate that cannot see its input must not report clean (#1269, #591). If this is a missing tool rather than a network fault, the fix is to declare it: biffo-plugin-ideation rode this path on every run, permanently green while scanning nothing."
  inconclusive=$((inconclusive + 1))
  return 0
}

# ## Discover the trees (#1270)
#
# Walk from the git root — not the CWD this script happens to run from — so a
# vendored tree anywhere in the repo is found regardless of where the CI job's
# `working-directory` points. Excludes `node_modules`, `.git`, `.worktrees`
# (other agents' in-progress checkouts) and `.venv` (an installed environment,
# not a lockfile source).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  echo "::error::py-dependency-audit: not inside a git repository ('git rev-parse --show-toplevel' failed). Discovery cannot walk a tree it cannot find." >&2
  exit 2
fi

WORKSPACE_ABS=$(pwd -P)

# shellcheck disable=SC2016
ALL_LOCKS=$(find "$REPO_ROOT" \
  \( -name node_modules -o -name .git -o -name .worktrees -o -name .venv \) -prune -o \
  -type f -name uv.lock -print 2>/dev/null | sort)

# Fail CLOSED, not open, on an empty discovery (#1270). Zero trees — including
# no uv.lock at the workspace root itself — means discovery is broken, not
# that the repo has nothing to audit. Same posture as `verify.sh`'s `ci_has()`
# (#1218) and `ci-wiring-audit.sh`'s empty-map check: "This audit checked
# nothing. That is a configuration error, not a pass." Exit 2, the estate's
# "cannot tell" code, distinct from "found a vulnerability" (1) and "clean"
# (0) — CI treats any non-zero as a failed step either way.
if [ -z "$ALL_LOCKS" ]; then
  printf '::error::py-dependency-audit: discovered ZERO uv.lock trees under %s.\n' "$REPO_ROOT" >&2
  printf 'This audit checked nothing. That is a configuration error, not a pass — see #1270.\n' >&2
  exit 2
fi

# Print what was scanned BEFORE auditing, so the list survives even if a later
# tree crashes the run — a bare green must be a falsifiable claim, not
# something a reader has to infer or re-enumerate by hand.
tree_count=$(printf '%s\n' "$ALL_LOCKS" | wc -l | tr -d ' ')
printf 'py-dependency-audit: discovered %s uv.lock tree(s):\n' "$tree_count"
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

# Audit each discovered tree. The workspace's own lockfile is audited via the
# INSTALLED environment `uv sync` already produced (unchanged from before —
# `uv run pip-audit` with no `-r`). Every other discovered lockfile is not an
# installed environment, so it is exported with `--frozen` (a read of the
# committed lock, no re-resolution) and audited with `-r`, from the workspace,
# which is where pip-audit is actually installed.
for lock in $ALL_LOCKS; do
  dir=$(dirname "$lock")
  dir_abs=$(cd "$dir" 2>/dev/null && pwd -P)
  case "$dir_abs" in
    "$REPO_ROOT") rel=. ;;
    "$REPO_ROOT"/*) rel=${dir_abs#"$REPO_ROOT"/} ;;
    *) rel="$dir_abs" ;;
  esac

  if [ "$dir_abs" = "$WORKSPACE_ABS" ]; then
    audit_deps "pip-audit (workspace: ${rel})" "" || failed=1
    continue
  fi

  reqs="$(mktemp)"

  # `--no-emit-project` drops the tree's own package (nothing published to
  # audit); `--no-dev` keeps the audit to what actually ships.
  #
  # `--no-emit-local` drops LOCAL PATH dependencies, and without it this gate
  # cannot scan a plugin tree at all (#1340). `biffo-plugin-sdk` is pulled in
  # as `-e ./packages/python-sdk`; pip-audit tries to resolve it on PyPI, fails
  # with "Dependency not found on PyPI and could not be audited", and produces
  # no parseable output -- so the whole tree came back INCONCLUSIVE and blocked.
  # Every vendored Python plugin tree in every instance has that dependency, so
  # this was not a niche shape: it blocked all of them.
  #
  # Skipping it costs nothing real. A local path dependency is first-party
  # source, audited in its own repo, and never a published artefact an advisory
  # could name. Its TRANSITIVE dependencies are still exported and still
  # audited -- verified on `services/ideation`, where the export keeps 25
  # packages including three carried `via biffo-plugin-sdk`. So the answer this
  # gate gives is unchanged for every package an advisory can actually be about.
  if (cd "$dir" && uv export --frozen --no-dev --no-emit-project --no-emit-local) >"$reqs" 2>/dev/null && [ -s "$reqs" ]; then
    # Report what was skipped rather than skipping it quietly: "audited
    # everything except the bits we could not" must never read the same as
    # "audited everything", which is the failure this whole file exists to
    # fight. Counted from the same lockfile, so the number is the tree's, not
    # a guess.
    local_deps=$( (cd "$dir" && uv export --frozen --no-dev --no-emit-project) 2>/dev/null \
      | grep -cE '^-e |@ file://' || true )
    if [ "${local_deps:-0}" -gt 0 ]; then
      echo "  (${rel}: ${local_deps} local path dependenc$([ "$local_deps" -eq 1 ] && echo y || echo ies) excluded -- first-party, audited in their own repo; their transitive dependencies are still scanned)"
    fi
    audit_deps "pip-audit (${rel})" "-r $reqs" || failed=1
  else
    # An export failure is "couldn't run", not "clean" — same discipline as a
    # transient pip-audit error, and it must never read as a pass.
    echo "::error::pip-audit (${rel}): could not export requirements from ${lock}. Advisory scanning was NOT performed for this tree, so this is INCONCLUSIVE and BLOCKS (#1269, #719)."
    inconclusive=$((inconclusive + 1))
  fi

  rm -f "$reqs"
done

if [ "$failed" -ne 0 ]; then
  exit 1
fi

if [ "$inconclusive" -ne 0 ]; then
  # Exit 2, not 1: "could not determine" is a different fact from "found a real
  # advisory". Conflating them sends whoever reads the red hunting a
  # vulnerability that may not exist. Same three-valued contract as
  # scripts/claim.sh (0 free / 1 taken / 2 cannot tell).
  echo "::error::${inconclusive} tree(s) could not be audited; see the errors above. Failing closed."
  exit 2
fi

echo "py-dependency-audit: audited ${tree_count} tree(s), 0 blocking findings."
exit 0
