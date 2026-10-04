#!/usr/bin/env sh
#
# Proves py-dependency-audit.sh (#1673) distinguishes "this PR's diff
# introduced or upgraded to a vulnerable package version" from "an advisory
# was published against a version already sitting on the base branch,
# unrelated to this diff" -- rather than reporting an identical red for both.
#
# The real incident this guards against: PYSEC-2026-3721 was published
# against pip 26.1.2 on 2026-08-21 and turned the Python lane red on PRs
# #1669/#1670, neither of which touched uv.lock or any Python dependency --
# dev's own lockfile was already on that exact pip version, green, the same
# morning before the advisory existed. Case 1 below reconstructs exactly that
# shape: base and PR both pinned at the flagged version, PR diff untouched.
#
# Builds a real, tiny, throwaway git repo under mktemp (never under /tmp as
# a full worktree copy -- this is a from-scratch repo with a handful of
# files, not a copy of this repository's own object store) with a base
# branch and a PR-head state, and stubs `uv` on PATH so `uv run pip-audit -f
# json` returns a canned finding without any network access or real
# environment. `jq`, `git` and `awk` are used for real -- they are exactly
# what the target script itself depends on, so stubbing them would test
# nothing.
#
# Run: sh scripts/py-dependency-audit-classification.test.sh

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET="$SCRIPT_DIR/py-dependency-audit.sh"

REPO_DIR=$(mktemp -d)
STUB_DIR=$(mktemp -d)
OUT_FILE=$(mktemp)
trap 'rm -rf "$REPO_DIR" "$STUB_DIR"; rm -f "$OUT_FILE"' EXIT

FAILURES=0

# --- uv stub -------------------------------------------------------------
# Implements only what the workspace path of the target script calls: `uv
# run pip-audit -f json`. Prints whatever JSON the current scenario staged
# at $STUB_DIR/pip-audit-output.json. Any other invocation is a test bug --
# fail loudly rather than silently returning nothing.
cat > "$STUB_DIR/uv" <<'STUB'
#!/usr/bin/env sh
if [ "$1" = "run" ] && [ "$2" = "pip-audit" ]; then
  cat "$PIPAUDIT_STUB_OUTPUT"
  exit 0
fi
echo "uv stub: unexpected invocation: $*" >&2
exit 99
STUB
chmod +x "$STUB_DIR/uv"

# --- repo scaffolding ------------------------------------------------------
# One workspace-level uv.lock so discovery finds exactly one tree, matching
# the real incident (the workspace/root lockfile, not a vendored one).
_write_lock() {
  # $1 = destination file, $2 = package name, $3 = version
  cat > "$1" <<LOCK
version = 1
requires-python = ">=3.13"

[[package]]
name = "$2"
version = "$3"
source = { registry = "https://pypi.org/simple" }
LOCK
}

_write_lock_two() {
  # $1 = destination file, $2/$3 = first package name/version,
  # $4/$5 = second package name/version. Used only by the mixed-tree case.
  cat > "$1" <<LOCK
version = 1
requires-python = ">=3.13"

[[package]]
name = "$2"
version = "$3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "$4"
version = "$5"
source = { registry = "https://pypi.org/simple" }
LOCK
}

_init_repo() {
  # $1 = base package version (what "dev" / origin/<base> holds)
  # $2 = head package version (what the PR branch under test holds)
  # $3 = package name (default "pip")
  base_version=$1
  head_version=$2
  pkg=${3:-pip}

  rm -rf "$REPO_DIR"
  mkdir -p "$REPO_DIR"
  ( cd "$REPO_DIR" \
    && git init -q -b trunk \
    && git config user.email test@example.com \
    && git config user.name "Test" )

  _write_lock "$REPO_DIR/uv.lock" "$pkg" "$base_version"
  ( cd "$REPO_DIR" && git add -A && git commit -q -m base )
  # A literal branch named "origin/<base>" -- git tolerates slashes in
  # branch names, so this resolves via `git show origin/<base>:<path>`
  # exactly like a real remote-tracking ref would, with no remote required.
  ( cd "$REPO_DIR" && git branch -q "origin/dev" )

  if [ "$head_version" != "$base_version" ]; then
    _write_lock "$REPO_DIR/uv.lock" "$pkg" "$head_version"
    ( cd "$REPO_DIR" && git add -A && git commit -q -m "pr change" )
  fi
}

# --- pip-audit output staging ---------------------------------------------
_stage_finding() {
  # $1 = package name, $2 = version pip-audit reports as installed/flagged
  pkg=$1
  version=$2
  cat > "$STUB_DIR/pip-audit-output.json" <<JSON
{"dependencies": [{"name": "$pkg", "version": "$version", "vulns": [{"id": "PYSEC-2026-3721", "fix_versions": ["26.2.1"]}]}]}
JSON
}

_stage_clean() {
  cat > "$STUB_DIR/pip-audit-output.json" <<'JSON'
{"dependencies": [{"name": "pip", "version": "26.2.1", "vulns": []}]}
JSON
}

# --- runner ----------------------------------------------------------------
_run() {
  # Runs the target script with GITHUB_BASE_REF set from $1 (empty string
  # means unset -- a push/workflow_dispatch/merge_group context), cwd inside
  # the synthetic repo, uv stubbed, PATH otherwise untouched (jq/git/awk are
  # the real system tools, same as production).
  base_ref=$1
  (
    cd "$REPO_DIR" || exit 97
    PATH="$STUB_DIR:$PATH"
    export PATH
    PIPAUDIT_STUB_OUTPUT="$STUB_DIR/pip-audit-output.json"
    export PIPAUDIT_STUB_OUTPUT
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

# ============================================================================
# Case table (must-NOT-block first, then must-block), each run against the
# real target script -- not a reimplementation of its logic.
# ============================================================================

# 1. PRE-EXISTING, PR context, package UNCHANGED from base (the PYSEC-2026-3721
#    / PR #1669 / #1670 shape: same version on both sides of the diff). Must
#    NOT block, and must say so as pre-existing rather than a flat red.
_init_repo "26.1.2" "26.1.2" pip
_stage_finding pip 26.1.2
_run dev
_assert_exit "pre-existing, version unchanged by diff" 0
_assert_output_contains "pre-existing case names it as pre-existing" "pre-existing"
_assert_output_contains "pre-existing case cites #1673" "#1673"

# 2. INTRODUCED via upgrade: base had a clean version, this diff's uv.lock
#    moved it to the flagged version. Must block.
_init_repo "26.0.0" "26.1.2" pip
_stage_finding pip 26.1.2
_run dev
_assert_exit "introduced by upgrade" 1
_assert_output_contains "introduced-by-upgrade case names it as introduced" "introduced"

# 3. INTRODUCED via new dependency: base's lockfile has no such package at
#    all; this diff added it at an already-vulnerable version. Must block.
_init_repo "0.0.0" "0.0.0" some-other-package
_write_lock "$REPO_DIR/uv.lock" "cryptography" "49.0.0"
( cd "$REPO_DIR" && git add -A && git commit -q -m "add new dependency" )
_stage_finding cryptography 49.0.0
_run dev
_assert_exit "introduced via brand-new dependency" 1
_assert_output_contains "new-dependency case names it as introduced" "introduced"

# 4. Non-PR context (push to the integration branch itself, or
#    workflow_dispatch/merge_group -- GITHUB_BASE_REF unset). No diff exists
#    to attribute the finding to, so the classification must NOT apply: any
#    finding blocks, exactly as before this fix. This is deliberately the
#    SAME lockfile shape as case 1 (unchanged version) to prove the
#    difference in outcome is driven by PR-context alone, matching #1671
#    (dev's own red had to be fixed, not reclassified away).
_init_repo "26.1.2" "26.1.2" pip
_stage_finding pip 26.1.2
_run ""
_assert_exit "no PR context -- always blocks" 1

# 5. PR context, but the base ref cannot be resolved locally (e.g. a shallow
#    checkout, or a distributed copy running where fetch-depth: 0 was not
#    honoured). Comparison fails CLOSED: blocks, same as before this fix,
#    rather than silently waving a real regression through because the
#    comparison itself could not be made.
_init_repo "26.1.2" "26.1.2" pip
_stage_finding pip 26.1.2
_run "some-branch-that-was-never-fetched"
_assert_exit "base ref unresolvable -- fails closed to blocking" 1

# 6. Mixed tree: one pre-existing finding (another-pkg, unchanged from base)
#    and one introduced finding (pip, upgraded by this diff) in the same
#    run. Must still block overall (the introduced one), while the
#    pre-existing one is still named as such rather than folded into an
#    undifferentiated total.
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"
( cd "$REPO_DIR" \
  && git init -q -b trunk \
  && git config user.email test@example.com \
  && git config user.name "Test" )
_write_lock_two "$REPO_DIR/uv.lock" pip "26.0.0" another-pkg "1.0.0"
( cd "$REPO_DIR" && git add -A && git commit -q -m base )
( cd "$REPO_DIR" && git branch -q "origin/dev" )
# PR upgrades pip only; another-pkg's pin is untouched.
_write_lock_two "$REPO_DIR/uv.lock" pip "26.1.2" another-pkg "1.0.0"
( cd "$REPO_DIR" && git add -A && git commit -q -m "pr upgrades pip" )
cat > "$STUB_DIR/pip-audit-output.json" <<'JSON'
{"dependencies": [
  {"name": "pip", "version": "26.1.2", "vulns": [{"id": "PYSEC-2026-3721"}]},
  {"name": "another-pkg", "version": "1.0.0", "vulns": [{"id": "PYSEC-0000-0000"}]}
]}
JSON
_run dev
_assert_exit "mixed tree -- introduced finding still blocks" 1
_assert_output_contains "mixed tree -- introduced pip is named" "pip@26.1.2"
_assert_output_contains "mixed tree -- pre-existing another-pkg is named separately" "another-pkg@1.0.0"
_assert_output_contains "mixed tree -- pre-existing another-pkg says so" "pre-existing"

# 7. Sanity: a clean run (no vulnerabilities at all) is unaffected by any of
#    the above -- still exits 0, still audits normally with the new 3-arg
#    audit_deps signature.
_init_repo "26.2.1" "26.2.1" pip
_stage_clean
_run dev
_assert_exit "clean run unaffected" 0

# ============================================================================
# uv WORKSPACE MEMBERS (#2120). The export and the classification must read
# the SAME lockfile. `uv export` run inside a workspace member resolves the
# WORKSPACE ROOT's uv.lock, not <member>/uv.lock; the classifier then compared
# that root-lock version against the member's own lock on the base branch, so a
# pre-existing root-lock advisory read as "introduced by this diff" for every
# member tree, and the member's own lock was never audited at all. Observed in
# biffo-platform PR 197 (anyio 4.14.1 in the root lock, 4.14.2 in both member
# locks).
#
# These cases use the REAL `uv export` (offline, --frozen, hand-written locks)
# because the defect lives in uv's workspace discovery -- a stubbed export
# would test nothing. Only `uv run pip-audit` is stubbed: for `-r <file>` it
# reports an advisory on anyio==4.14.1 (fixed in 4.14.2) found in THAT file,
# and it records the file so the test can assert which lock was audited.
# ============================================================================
REAL_UV=$(command -v uv 2>/dev/null || true)
if [ -z "$REAL_UV" ]; then
  echo "FAIL: workspace-member cases need a real uv on PATH (they exercise uv's workspace discovery) -- not skipping, a skipped case reads as a pass"
  FAILURES=$((FAILURES + 1))
else
  cat > "$STUB_DIR/uv" <<STUB
#!/usr/bin/env sh
if [ "\$1" = "run" ] && [ "\$2" = "pip-audit" ]; then
  reqs=""
  while [ \$# -gt 0 ]; do
    if [ "\$1" = "-r" ]; then reqs="\$2"; fi
    shift
  done
  if [ -z "\$reqs" ]; then
    cat "\$PIPAUDIT_STUB_OUTPUT"
    exit 0
  fi
  cat "\$reqs" >> "\$AUDITED_LOG"
  printf '{"dependencies": ['
  sep=""
  grep -E '^[A-Za-z0-9_.-]+==' "\$reqs" | sed 's/ .*//' | while IFS='=' read -r n _ v; do
    vulns='[]'
    if [ "\$n" = anyio ] && [ "\$v" = 4.14.1 ]; then vulns='[{"id":"GHSA-test-anyio"}]'; fi
    printf '%s{"name":"%s","version":"%s","vulns":%s}' "\$sep" "\$n" "\$v" "\$vulns"
    sep=","
  done
  printf ']}\n'
  exit 0
fi
exec "$REAL_UV" "\$@"
STUB
  chmod +x "$STUB_DIR/uv"
  AUDITED_LOG="$STUB_DIR/audited.log"
  export AUDITED_LOG

  _ws_lock() {
    # $1 = dest, $2 = anyio version. Member locks (uv resolves a lone member
    # as its own project) hold only that member; the workspace ROOT lock holds
    # the root and every member, exactly as `uv lock` writes them.
    # $3 = "root" for the workspace root lock, "member" for a member's own.
    cat > "$1" <<LOCK
version = 1
revision = 3
requires-python = ">=3.12"
LOCK
    if [ "$3" = root ]; then
      cat >> "$1" <<'LOCK'

[manifest]
members = ["m", "root"]
LOCK
    fi
    cat >> "$1" <<LOCK

[[package]]
name = "anyio"
version = "$2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "m"
version = "0.1.0"
source = { virtual = "$([ "$3" = root ] && echo services/m || echo .)" }
dependencies = [{ name = "anyio" }]
LOCK
    if [ "$3" = root ]; then
      cat >> "$1" <<'LOCK'

[[package]]
name = "root"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "anyio" }]
LOCK
    fi
  }

  _ws_repo() {
    # $1 = root-lock anyio, $2 = member-lock anyio ON BASE,
    # $3 = member-lock anyio on the PR head (== $2 when the PR leaves it alone)
    rm -rf "$REPO_DIR"
    mkdir -p "$REPO_DIR/services/m"
    ( cd "$REPO_DIR" \
      && git init -q -b trunk \
      && git config user.email test@example.com \
      && git config user.name "Test" )
    cat > "$REPO_DIR/pyproject.toml" <<'PYP'
[project]
name = "root"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["anyio"]
[tool.uv.workspace]
members = ["services/*"]
PYP
    cat > "$REPO_DIR/services/m/pyproject.toml" <<'PYP'
[project]
name = "m"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["anyio"]
PYP
    _ws_lock "$REPO_DIR/uv.lock" "$1" root
    _ws_lock "$REPO_DIR/services/m/uv.lock" "$2" member
    ( cd "$REPO_DIR" && git add -A && git commit -q -m base && git branch -q origin/dev )
    if [ "$3" != "$2" ]; then
      _ws_lock "$REPO_DIR/services/m/uv.lock" "$3" member
      ( cd "$REPO_DIR" && git add -A && git commit -q -m "pr bumps member lock" )
    fi
    : > "$AUDITED_LOG"
    _stage_clean
  }

  _assert_audited() {
    # $1 = name, $2 = requirement line the MEMBER audit must have received
    if grep -qxF "$2" "$AUDITED_LOG"; then
      echo "PASS: $1 audited '$2'"
    else
      echo "FAIL: $1 -- expected the member tree's OWN lock to be audited ('$2'); pip-audit was given:"
      cat "$AUDITED_LOG"
      FAILURES=$((FAILURES + 1))
    fi
  }

  _assert_not_audited() {
    if grep -qxF "$2" "$AUDITED_LOG"; then
      echo "FAIL: $1 -- pip-audit was given '$2', i.e. the ROOT lock's version, not the member's own"
      FAILURES=$((FAILURES + 1))
    else
      echo "PASS: $1 did not audit '$2'"
    fi
  }

  # 8. The reported shape. Root lock has the vulnerable anyio 4.14.1; the
  #    member's own lock is already fixed (4.14.2); the PR touches neither.
  #    The member's audit must read ITS lock (4.14.2, clean) -- before #2120 it
  #    read the root lock (4.14.1), classified that against the member's base
  #    lock (4.14.2) and blocked on a finding the diff did not introduce.
  _ws_repo "4.14.1" "4.14.2" "4.14.2"
  _run dev
  _assert_exit "workspace member: root-lock advisory not blamed on the diff" 0
  _assert_audited "workspace member (own lock fixed)" "anyio==4.14.2"
  _assert_not_audited "workspace member (own lock fixed)" "anyio==4.14.1"

  # 9. Inverse: the MEMBER's own lock carries the advisory (unchanged from
  #    base), the root is fine. Before #2120 the member lock was never audited,
  #    so this read as a clean pass. It must be audited, and reported as
  #    pre-existing rather than silently skipped or blamed on the diff.
  _ws_repo "4.14.2" "4.14.1" "4.14.1"
  _run dev
  _assert_exit "workspace member: own-lock pre-existing advisory does not block" 0
  _assert_audited "workspace member (own lock vulnerable)" "anyio==4.14.1"
  _assert_output_contains "workspace member: own-lock advisory named pre-existing" "pre-existing"

  # 10. The PR itself bumps the member's own lock INTO the advisory: a real
  #     regression in the file that was previously never looked at. Must block.
  _ws_repo "4.14.2" "4.14.2" "4.14.1"
  _run dev
  _assert_exit "workspace member: PR bumping own lock into an advisory blocks" 1
  _assert_output_contains "workspace member: regression named as introduced" "introduced"

  # 11. A scratch directory INSIDE the repository would put the workspace root
  #     back above the staged copy and silently restore the wrong-lock export.
  #     TMPDIR pointed under the repo must fail closed (INCONCLUSIVE, exit 2),
  #     never fall back to exporting in place.
  _ws_repo "4.14.1" "4.14.2" "4.14.2"
  mkdir -p "$REPO_DIR/scratch"
  (
    cd "$REPO_DIR" || exit 97
    PATH="$STUB_DIR:$PATH"; export PATH
    PIPAUDIT_STUB_OUTPUT="$STUB_DIR/pip-audit-output.json"; export PIPAUDIT_STUB_OUTPUT
    TMPDIR="$REPO_DIR/scratch"; export TMPDIR
    GITHUB_BASE_REF=dev; export GITHUB_BASE_REF
    sh "$TARGET"
  ) >"$OUT_FILE" 2>&1
  LAST_RC=$?
  _assert_exit "scratch dir inside the repo fails closed" 2
  _assert_output_contains "scratch-inside-repo names the tree as unaudited" "INCONCLUSIVE"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "py-dependency-audit-classification.test.sh: all checks passed."
  exit 0
else
  echo "py-dependency-audit-classification.test.sh: $FAILURES check(s) failed."
  exit 1
fi
