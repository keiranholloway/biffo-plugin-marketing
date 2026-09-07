#!/usr/bin/env sh
#
# Does every scripts/*.test.sh guard actually get executed by this repo's
# own CI, rather than sitting in the tree looking like coverage?
#
# ## Why this exists (#1705)
#
# #1413/#1629/#1582 each wired ONE previously-uncalled scripts/*.test.sh
# guard into ci.yml or release-guards.yml by hand, and each time the fix was
# "add a `run:` line for this one file". That closes the instance found and
# leaves the class open: the next guard someone adds sits unwired exactly
# the same way, silently, until someone happens to notice. A guard with no
# caller is a fail-open — it contributes nothing to CI and looks exactly
# like coverage from the workflow file alone.
#
# This is the derived version: it globs scripts/*.test.sh itself, so the
# denominator is never a hand-maintained list that can drift from the
# filesystem, and it fails closed rather than reporting a partial count as
# success.
#
# ## Two ways a guard counts as "covered", and why both are needed
#
# 1. Already referenced by name in a real (non-comment) line somewhere in
#    .github/workflows/*.yml — e.g. `run: sh scripts/verify-deployed.test.sh`,
#    or a name appearing inside a multi-line `run: |` block such as the
#    docker-wrapped `interpreter-audit.test.sh` self-test in
#    release-guards.yml. That wrapping exists for a real reason (#1652: the
#    self-test compares bash's output against a GENUINE dash, and some
#    self-hosted instance runners have no dash package at all, no busybox,
#    nothing to install it from) — re-running that guard a second time here,
#    directly with plain `sh` on whatever runner this job happens to land
#    on, would reproduce exactly the false-positive #1652 fixed, this time
#    via THIS script. So a guard with a real caller elsewhere is left alone:
#    this script only confirms the reference exists, it does not re-run it.
# 2. Not referenced anywhere — this script runs it directly, right here,
#    with `sh` (the same interpreter every *.test.sh guard's own shebang
#    declares and is invoked with elsewhere in this repo). This is what
#    closes the class: a new guard added tomorrow with no `run:` line
#    anywhere is executed automatically instead of silently doing nothing,
#    and if it fails, THIS step goes red with its name and exit code.
#
# Text-presence in a non-comment line is not proof of execution (a `run:`
# line inside a permanently-disabled `if: false` step would still count) —
# that residual gap is accepted rather than solved here; it requires a step
# already wired well enough that #1413's own history has not produced one.
#
# ## Fail-closed requirements
#
# - Zero files matched by the glob is a FAILURE, not an empty pass — that is
#   the exact shape of the defect this exists to catch (a discovery step
#   that silently discovers nothing).
# - A guard file that is unreadable or not executable is a FAILURE.
# - A directly-run guard that exits non-zero is a FAILURE, reported by name.
# - The denominator (found / already-wired / run-here) is always printed,
#   pass or fail, so a green run can never be read as having skipped
#   verifying anything.

set -eu

GUARD_DIR="scripts"
WORKFLOW_DIR=".github/workflows"

guards=$(find "$GUARD_DIR" -maxdepth 1 -type f -name '*.test.sh' | sort)

guard_count=0
if [ -n "$guards" ]; then
  guard_count=$(printf '%s\n' "$guards" | wc -l | tr -d ' ')
fi

if [ "$guard_count" -eq 0 ]; then
  echo "FAIL: found 0 files matching ${GUARD_DIR}/*.test.sh — the glob matched nothing, so nothing could be verified. This is the exact defect #1705 exists to catch: treat an empty discovery as a failure, never as a vacuous pass." >&2
  exit 1
fi

wired_tmp=$(mktemp)
trap 'rm -f "$wired_tmp"' EXIT

for wf in "$WORKFLOW_DIR"/*.yml "$WORKFLOW_DIR"/*.yaml; do
  [ -f "$wf" ] || continue
  # Strip comment-only lines (first non-blank character is '#') before
  # searching, so a comment merely MENTIONING a guard's filename (there are
  # several in this repo, documenting exactly this history) is never
  # mistaken for a caller.
  grep -v '^[[:space:]]*#' "$wf" | grep -o "${GUARD_DIR}/[A-Za-z0-9_-]*\.test\.sh" || true
done >>"$wired_tmp"

sort -u "$wired_tmp" -o "$wired_tmp"

already_wired=0
run_here=0
failed=""

for g in $guards; do
  name=$(basename "$g")

  if [ ! -r "$g" ]; then
    echo "FAIL: $g exists but is not readable." >&2
    exit 1
  fi
  if [ ! -x "$g" ]; then
    echo "FAIL: $g exists but is not executable (chmod +x it)." >&2
    exit 1
  fi

  if grep -qx "${GUARD_DIR}/${name}" "$wired_tmp"; then
    already_wired=$((already_wired + 1))
    continue
  fi

  echo "-- ${name} has no caller anywhere in ${WORKFLOW_DIR} — running it directly here --"
  if sh "$g"; then
    run_here=$((run_here + 1))
  else
    rc=$?
    failed="${failed}  - ${g} (exit ${rc})
"
  fi
done

echo
echo "Guard wiring audit: found ${guard_count} ${GUARD_DIR}/*.test.sh file(s) — ${already_wired} already wired into ${WORKFLOW_DIR}, ${run_here} discovered here with no other caller and run directly, $(printf '%s' "$failed" | grep -c '.' || true) failed."

if [ -n "$failed" ]; then
  printf 'FAIL: the following newly-discovered guard(s) failed when run:\n%s' "$failed" >&2
  exit 1
fi

total_accounted=$((already_wired + run_here))
if [ "$total_accounted" -ne "$guard_count" ]; then
  echo "FAIL: accounted for ${total_accounted} of ${guard_count} guard(s) — the count does not add up, which means this script has a bug rather than a clean pass." >&2
  exit 1
fi

exit 0
