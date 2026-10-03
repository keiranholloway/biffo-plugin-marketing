#!/usr/bin/env sh
#
# Proves js-dependency-audit.sh (#2040) distinguishes "this PR's diff
# introduced or upgraded to a vulnerable package version" from "an advisory
# was published against a version already sitting on the base branch,
# unrelated to this diff" -- rather than reporting an identical red for both.
# This is the JS side of the fix py-dependency-audit.sh already got in
# #1673; see that file's own classification test for the Python case table
# this one mirrors.
#
# The real incident this guards against: two routine npm advisories
# (GHSA-p293-qw3h-jr36 / Next.js, GHSA-82fw-gwwq-j7x9 / vitest) were
# published against packages already on `dev` on 2026-09-08/09, with no code
# change anywhere -- and turned the JS lane red on every open PR at once,
# producing 89 fleet dispatches across 61 units in 8 repos and 10 duplicate
# tickets for one classifier bug (#2040's own cost accounting). Case 1 below
# reconstructs exactly that shape: base and PR both pinned at the flagged
# version, PR diff untouched.
#
# ## Real `pnpm audit --json` shape, captured live
#
# Run against this repo's own workspace (pnpm 9.15.9, 2026-09-10):
#
#   $ pnpm audit --json
#   {
#     "advisories": {
#       "1090893": {
#         "severity": "low",
#         "module_name": "cli",
#         "github_advisory_id": "GHSA-6cpc-mj5c-m9rq",
#         "findings": [{"version": "0.0.0", "paths": []}],
#         ...
#       }
#     },
#     "metadata": {
#       "vulnerabilities": {"info": 0, "low": 1, "moderate": 0, "high": 0, "critical": 0},
#       "totalDependencies": 929
#     }
#   }
#
# `.advisories` is an OBJECT keyed by an internal numeric id, not an array --
# `.advisories[]?` iterates its values the same as an array would. Each
# value carries `.severity`, `.module_name` and `.github_advisory_id`, which
# is exactly what js-dependency-audit.sh's classification reads. The fixture
# JSON below is shaped to match this real structure, not invented -- the
# OLD version of js-dependency-audit-parallel.test.sh's stub JSON carried
# only `.metadata.vulnerabilities` with no `.advisories` key at all, which
# classification silently read as zero findings (fixed alongside this file;
# see that test's own updated `_fail_json` comment).
#
# Also proves `pnpm audit` runs against a BARE lockfile copy with no install
# and no workspace context -- verified directly, not assumed:
#
#   $ mkdir /tmp/probe && cp pnpm-lock.yaml /tmp/probe/ && cd /tmp/probe
#   $ pnpm audit --json --ignore-workspace   # exits 0, real advisory output
#
# which is why the base-branch side of this comparison is a second real
# `pnpm audit` call against a `git show`-extracted copy of the lockfile,
# not a hand-rolled pnpm-lock.yaml parser.
#
# Builds a real, tiny, throwaway git repo under mktemp (never under /tmp as
# a full worktree copy -- this is a from-scratch repo with a handful of
# files, not a copy of this repository's own object store -- see AGENTS.md's
# "never create a git worktree ... under /tmp", which this is not) with a
# base branch and a PR-head state, and stubs `pnpm` on PATH so `pnpm audit
# --json` returns a canned finding selected by a marker comment inside
# whichever pnpm-lock.yaml the stub is invoked against -- no network, no
# real registry. `jq`, `git` and `grep` are used for real -- they are
# exactly what the target script itself depends on, so stubbing them would
# test nothing.
#
# Run: sh scripts/js-dependency-audit-classification.test.sh

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET="$SCRIPT_DIR/js-dependency-audit.sh"

REPO_DIR=$(mktemp -d)
STUB_DIR=$(mktemp -d)
TMPROOT_DIR=$(mktemp -d)
OUT_FILE=$(mktemp)
trap 'rm -rf "$REPO_DIR" "$STUB_DIR" "$TMPROOT_DIR"; rm -f "$OUT_FILE"' EXIT

FAILURES=0

# --- pnpm stub ---------------------------------------------------------------
# Selected by a `# STATE:<tag>` marker comment inside whatever
# pnpm-lock.yaml is in the CURRENT DIRECTORY when invoked -- the target
# script always `cd`s into the tree it is auditing (the repo's own working
# tree for the HEAD run, or a scratch dir holding a `git show`-extracted
# copy for the BASE run), so the marker travels with whichever commit's
# content is actually being read, exactly like a real lockfile diff would.
cat > "$STUB_DIR/pnpm" <<'STUB'
#!/usr/bin/env sh
if [ "$1" = "audit" ]; then
  state=$(grep -o 'STATE:[A-Za-z0-9_-]*' pnpm-lock.yaml 2>/dev/null | head -1 | cut -d: -f2)
  out_file="$STUB_DIR_ENV/output-${state:-clean}.json"
  if [ -f "$out_file" ]; then
    cat "$out_file"
  else
    cat "$STUB_DIR_ENV/output-clean.json"
  fi
  exit 0
fi
echo "pnpm stub: unexpected invocation: $*" >&2
exit 99
STUB
chmod +x "$STUB_DIR/pnpm"

# --- fixture JSON, one per STATE tag ----------------------------------------
cat > "$STUB_DIR/output-clean.json" <<'JSON'
{"metadata":{"vulnerabilities":{"critical":0,"high":0,"moderate":0,"low":0},"totalDependencies":5}}
JSON

# One high-severity advisory against vuln-pkg-a.
cat > "$STUB_DIR/output-vuln-a.json" <<'JSON'
{"metadata":{"vulnerabilities":{"critical":0,"high":1,"moderate":0,"low":0},"totalDependencies":5},"advisories":{"1":{"severity":"high","github_advisory_id":"GHSA-test-0001","module_name":"vuln-pkg-a","findings":[{"version":"1.2.3","paths":[]}]}}}
JSON

# Only vuln-pkg-b's advisory (used as the mixed case's BASE state -- pkg-a
# is absent entirely, pkg-b is present and already flagged).
cat > "$STUB_DIR/output-vuln-b-only.json" <<'JSON'
{"metadata":{"vulnerabilities":{"critical":0,"high":1,"moderate":0,"low":0},"totalDependencies":5},"advisories":{"2":{"severity":"high","github_advisory_id":"GHSA-test-0002","module_name":"vuln-pkg-b","findings":[{"version":"2.0.0","paths":[]}]}}}
JSON

# Both advisories together (the mixed case's HEAD state).
cat > "$STUB_DIR/output-vuln-a-b.json" <<'JSON'
{"metadata":{"vulnerabilities":{"critical":0,"high":2,"moderate":0,"low":0},"totalDependencies":5},"advisories":{"1":{"severity":"high","github_advisory_id":"GHSA-test-0001","module_name":"vuln-pkg-a","findings":[{"version":"1.2.3","paths":[]}]},"2":{"severity":"high","github_advisory_id":"GHSA-test-0002","module_name":"vuln-pkg-b","findings":[{"version":"2.0.0","paths":[]}]}}}
JSON

# pnpm `auditConfig.ignoreGhsas` shape: advisories emptied, metadata still
# counting the ignored high advisory.
cat > "$STUB_DIR/output-ignored.json" <<'JSON'
{"metadata":{"vulnerabilities":{"critical":0,"high":1,"moderate":0,"low":0},"totalDependencies":5},"advisories":{}}
JSON

# --- repo scaffolding --------------------------------------------------------
# One workspace-level pnpm-lock.yaml so discovery finds exactly one tree,
# matching the real incident (the workspace root, not a vendored tree).
_write_lock() {
  # $1 = destination file, $2 = STATE tag
  cat > "$1" <<LOCK
lockfileVersion: '9.0'
# STATE:$2
LOCK
}

_init_repo() {
  # $1 = base STATE tag (what "dev" / origin/<base> holds)
  # $2 = head STATE tag (what the PR branch under test holds)
  base_state=$1
  head_state=$2

  rm -rf "$REPO_DIR"
  mkdir -p "$REPO_DIR"
  ( cd "$REPO_DIR" \
    && git init -q -b trunk \
    && git config user.email test@example.com \
    && git config user.name "Test" )

  _write_lock "$REPO_DIR/pnpm-lock.yaml" "$base_state"
  ( cd "$REPO_DIR" && git add -A && git commit -q -m base )
  # A literal branch named "origin/<base>" -- git tolerates slashes in
  # branch names, so this resolves via `git show origin/<base>:<path>`
  # exactly like a real remote-tracking ref would, with no remote required.
  ( cd "$REPO_DIR" && git branch -q "origin/dev" )

  if [ "$head_state" != "$base_state" ]; then
    _write_lock "$REPO_DIR/pnpm-lock.yaml" "$head_state"
    ( cd "$REPO_DIR" && git add -A && git commit -q -m "pr change" )
  fi
}

# --- runner ------------------------------------------------------------------
_run() {
  # Runs the target script with GITHUB_BASE_REF set from $1 (empty string
  # means unset -- a push/workflow_dispatch/merge_group context), cwd inside
  # the synthetic repo, pnpm stubbed, PATH otherwise untouched (jq/git/grep
  # are the real system tools, same as production).
  base_ref=$1
  (
    cd "$REPO_DIR" || exit 97
    PATH="$STUB_DIR:$PATH"
    STUB_DIR_ENV="$STUB_DIR"
    TMPDIR="$TMPROOT_DIR"
    export PATH STUB_DIR_ENV TMPDIR
    if [ -n "$base_ref" ]; then
      GITHUB_BASE_REF="$base_ref"
      export GITHUB_BASE_REF
    else
      unset GITHUB_BASE_REF
    fi
    sh "$TARGET"
  ) >"$OUT_FILE" 2>&1
  LAST_RC=$?
}

_assert_exit() {
  name=$1
  expected=$2
  if [ "$LAST_RC" -eq "$expected" ]; then
    echo "PASS: $name (exit $LAST_RC)"
  else
    echo "FAIL: $name -- expected exit $expected, got $LAST_RC"
    echo "--- output ---"
    cat "$OUT_FILE"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_output_contains() {
  name=$1
  needle=$2
  if grep -qF "$needle" "$OUT_FILE"; then
    echo "PASS: $name mentions '$needle'"
  else
    echo "FAIL: $name -- expected output to mention '$needle'"
    echo "--- output ---"
    cat "$OUT_FILE"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
  fi
}

# ==============================================================================
# Case table (must-NOT-block first, then must-block), each run against the
# real target script -- not a reimplementation of its logic.
# ==============================================================================

# 1. PRE-EXISTING, PR context, advisory UNCHANGED from base (the
#    GHSA-p293-qw3h-jr36 / 2026-09-08 shape: same version on both sides of
#    the diff). Must NOT block, and must say so as pre-existing rather than
#    a flat red.
_init_repo "vuln-a" "vuln-a"
_run dev
_assert_exit "pre-existing, advisory unchanged by diff" 0
_assert_output_contains "pre-existing case names it as pre-existing" "pre-existing"
_assert_output_contains "pre-existing case cites #2040" "#2040"

# 2. INTRODUCED via upgrade: base had a clean lockfile, this diff's
#    pnpm-lock.yaml moved it to the flagged version. Must block.
_init_repo "clean" "vuln-a"
_run dev
_assert_exit "introduced by upgrade" 1
_assert_output_contains "introduced-by-upgrade case names it as introduced" "introduced or upgraded by this diff"

# 3. INTRODUCED via new dependency: base's lockfile carries no such package
#    at all; this diff added it at an already-vulnerable version. For the
#    advisory-ID-diffing design this is mechanically identical to case 2
#    (base's audit reports no matching id either way) -- kept as its own
#    case for documentation, matching py-dependency-audit-classification's
#    own case 3, which IS mechanically distinct there because Python
#    classifies per-package lockfile version rather than by diffing two
#    full audit runs.
_init_repo "clean" "vuln-a"
_run dev
_assert_exit "introduced via brand-new dependency" 1
_assert_output_contains "new-dependency case names it as introduced" "introduced or upgraded by this diff"

# 4. Non-PR context (push to the integration branch itself, or
#    workflow_dispatch/merge_group -- GITHUB_BASE_REF unset). No diff exists
#    to attribute the finding to, so the classification must NOT apply: any
#    finding blocks, exactly as before this fix. Deliberately the SAME
#    lockfile shape as case 1 (unchanged advisory) to prove the difference
#    in outcome is driven by PR-context alone -- the scheduled base-branch
#    scan added alongside this file (#2040) relies on exactly this: every
#    current finding on `dev` must be reported, not just new ones.
_init_repo "vuln-a" "vuln-a"
_run ""
_assert_exit "no PR context -- always blocks" 1

# 5. PR context, but the base ref cannot be resolved locally (e.g. a shallow
#    checkout, or a distributed copy running where fetch-depth: 0 was not
#    honoured). Comparison fails CLOSED: blocks, same as before this fix,
#    rather than silently waving a real regression through because the
#    comparison itself could not be made.
_init_repo "vuln-a" "vuln-a"
_run "some-branch-that-was-never-fetched"
_assert_exit "base ref unresolvable -- fails closed to blocking" 1
_assert_output_contains "base ref unresolvable -- warns why" "does not resolve locally"

# 6. Mixed tree: one pre-existing finding (vuln-pkg-b, unchanged from base)
#    and one introduced finding (vuln-pkg-a, new to this diff) in the same
#    run. Must still block overall (the introduced one), while the
#    pre-existing one is still named as such rather than folded into an
#    undifferentiated total.
_init_repo "vuln-b-only" "vuln-a-b"
_run dev
_assert_exit "mixed tree -- introduced finding still blocks" 1
_assert_output_contains "mixed tree -- introduced vuln-pkg-a is named" "vuln-pkg-a advisory GHSA-test-0001"
_assert_output_contains "mixed tree -- pre-existing vuln-pkg-b is named separately" "vuln-pkg-b advisory GHSA-test-0002"
_assert_output_contains "mixed tree -- pre-existing vuln-pkg-b says so" "pre-existing"

# 7. Sanity: a clean run (no vulnerabilities at all) is unaffected by any of
#    the above -- still exits 0, still audits normally with the new
#    5-arg audit_dir signature.
_init_repo "clean" "clean"
_run dev
_assert_exit "clean run unaffected" 0

# 8. pnpm ignoreGhsas: metadata still counts the ignored advisory but
#    .advisories is empty -- must not block.
_init_repo "ignored" "ignored"
_run dev
_assert_exit "ignoreGhsas-suppressed advisory does not block" 0
_assert_output_contains "ignored case reports 0 high" "0 critical, 0 high"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "js-dependency-audit-classification.test.sh: all checks passed."
  exit 0
else
  echo "js-dependency-audit-classification.test.sh: $FAILURES check(s) failed."
  exit 1
fi
