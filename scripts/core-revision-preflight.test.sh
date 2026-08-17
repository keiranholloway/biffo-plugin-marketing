#!/usr/bin/env sh
#
# Proves core-revision-preflight.sh (#1604) actually blocks a sibling that
# would go live ahead of the core routes it depends on, passes it once core
# has caught up, and fails CLOSED — never open — when the digest is missing.
#
# Stubs `curl` on PATH so this needs no network and no live deployment.
#
# Run: sh scripts/core-revision-preflight.test.sh

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET="$SCRIPT_DIR/core-revision-preflight.sh"
STUB_DIR=$(mktemp -d)
OUT_FILE=/tmp/core-revision-preflight-test-out.$$
trap 'rm -rf "$STUB_DIR"; rm -f "$OUT_FILE"' EXIT

FAILURES=0
URL="https://dev.example.com/.well-known/route-revision.json"

# _run <env assignments...> -- runs the target with PREFLIGHT_CURL pointed at
# the stub, capturing combined output to $OUT_FILE and the exit code into
# $LAST_RC. NOT run inside a subshell/command-substitution, so a caller that
# wants to assert on both the exit code AND FAILURES afterward gets both —
# an earlier version of this test ran the assertion inside `out=$(...)`,
# which put the whole check in a subshell: FAILURES++ there never reached
# the parent shell, so a real regression could not fail this test.
_run() {
  env "$@" sh -c "PREFLIGHT_CURL='$STUB_DIR/curl' sh '$TARGET'" >"$OUT_FILE" 2>&1
  LAST_RC=$?
}

_assert_exit() {
  # _assert_exit <scenario-name> <expected-exit>
  name=$1
  expected=$2
  if [ "$LAST_RC" -eq "$expected" ]; then
    echo "PASS: $name (exit $LAST_RC)"
  else
    echo "FAIL: $name — expected exit $expected, got $LAST_RC"
    echo "--- output ---"
    cat "$OUT_FILE"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
  fi
}

_assert_output_contains() {
  # _assert_output_contains <scenario-name> <needle>
  name=$1
  needle=$2
  if grep -qF "$needle" "$OUT_FILE"; then
    echo "PASS: $name mentions '$needle'"
  else
    echo "FAIL: $name — expected output to mention '$needle'"
    echo "--- output ---"
    cat "$OUT_FILE"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
  fi
}

# --- Stub curl factory --------------------------------------------------
# Writes a stub that returns a fixed http status + body for the one URL this
# script ever fetches, or simulates a transport failure.
_write_stub_ok() {
  revision=$1
  cat > "$STUB_DIR/curl" <<STUB
#!/usr/bin/env sh
outfile=""
prev=""
for a in "\$@"; do
  case "\$prev" in
    -o) outfile="\$a" ;;
  esac
  prev="\$a"
done
printf '{"revision": $revision, "hash": "deadbeef", "routeCount": 70, "generatedAt": "2026-08-17T00:00:00Z"}' > "\$outfile"
printf '200'
exit 0
STUB
  chmod +x "$STUB_DIR/curl"
}

_write_stub_unreachable() {
  cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env sh
echo "curl: (6) Could not resolve host: dev.example.com" >&2
exit 6
STUB
  chmod +x "$STUB_DIR/curl"
}

_write_stub_404() {
  cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env sh
outfile=""
prev=""
for a in "$@"; do
  case "$prev" in
    -o) outfile="$a" ;;
  esac
  prev="$a"
done
printf 'Not Found' > "$outfile"
printf '404'
exit 0
STUB
  chmod +x "$STUB_DIR/curl"
}

_write_stub_never_called() {
  # A curl invocation in this scenario is itself the bug — the script must
  # short-circuit before ever fetching.
  cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env sh
echo "curl invoked when it should not have been" >&2
exit 99
STUB
  chmod +x "$STUB_DIR/curl"
}

# 1. No CORE_MIN_ROUTE_REVISION declared at all — this sibling depends on
#    nothing new from core. Must pass WITHOUT even invoking curl, and must
#    say so LOUDLY: a GitHub Actions ::notice:: annotation (not a bare echo
#    a reader would have to open the step log to find), and never wording
#    that could be mistaken for "checked and fine" (#1635 prosecution
#    finding 2 — the same treatment this repo already gives
#    SIBLING_DEPLOY_ENABLED=false).
_write_stub_never_called
_run -u CORE_MIN_ROUTE_REVISION -u CORE_ROUTE_REVISION_URL --
_assert_exit "no minimum declared" 0
_assert_output_contains "no minimum declared — is a ::notice:: annotation" "::notice::"
_assert_output_contains "no minimum declared — says preflight did not run" "did NOT run"

# 1b. Same scenario, but with GITHUB_STEP_SUMMARY set as it is in a real
#     GitHub Actions run — the skip must ALSO land in the job summary, not
#     only the step log, matching how this repo's own deploy-app.yml surfaces
#     a deploy-time skip/failure to a reader who never opens raw logs.
SUMMARY_FILE=/tmp/core-revision-preflight-test-summary.$$
: > "$SUMMARY_FILE"
_write_stub_never_called
_run -u CORE_MIN_ROUTE_REVISION -u CORE_ROUTE_REVISION_URL -- GITHUB_STEP_SUMMARY="$SUMMARY_FILE"
_assert_exit "no minimum declared, with GITHUB_STEP_SUMMARY set" 0
if grep -qF "did NOT run" "$SUMMARY_FILE" 2>/dev/null; then
  echo "PASS: no minimum declared — writes to \$GITHUB_STEP_SUMMARY"
else
  echo "FAIL: no minimum declared — expected \$GITHUB_STEP_SUMMARY ($SUMMARY_FILE) to mention 'did NOT run'"
  echo "--- summary file ---"
  cat "$SUMMARY_FILE" 2>/dev/null
  echo "--------------------"
  FAILURES=$((FAILURES + 1))
fi
rm -f "$SUMMARY_FILE"

# 2. Sibling requires a revision core HAS reached (core is AHEAD) — passes.
_write_stub_ok 50
_run CORE_MIN_ROUTE_REVISION=42 CORE_ROUTE_REVISION_URL="$URL"
_assert_exit "core ahead of required revision (42 <= 50)" 0
_assert_output_contains "core ahead — names actual" "revision 50"

# 2b. Exact match — core is AT the required revision — passes.
_write_stub_ok 42
_run CORE_MIN_ROUTE_REVISION=42 CORE_ROUTE_REVISION_URL="$URL"
_assert_exit "core exactly at required revision" 0

# 3. Sibling requires a revision core has NOT reached — fails, naming both
#    the required and actual revisions.
_write_stub_ok 10
_run CORE_MIN_ROUTE_REVISION=42 CORE_ROUTE_REVISION_URL="$URL"
_assert_exit "core behind required revision" 1
_assert_output_contains "core behind — names actual" "revision 10"
_assert_output_contains "core behind — names required" "at least 42"

# 4. No digest published at all (transport failure) — fails CLOSED, and says
#    the digest was missing rather than that the check passed.
_write_stub_unreachable
_run CORE_MIN_ROUTE_REVISION=42 CORE_ROUTE_REVISION_URL="$URL"
_assert_exit "digest unreachable" 1
_assert_output_contains "digest unreachable — says missing, not behind" "no route-revision digest available"

# 5. Digest endpoint 404s (older core / never published) — fails CLOSED, same
#    "missing" message as scenario 4, never "core is behind".
_write_stub_404
_run CORE_MIN_ROUTE_REVISION=42 CORE_ROUTE_REVISION_URL="$URL"
_assert_exit "digest 404" 1
_assert_output_contains "digest 404 — says missing" "no route-revision digest available"

# 6. A minimum is declared but CORE_ROUTE_REVISION_URL is not set — must fail
#    without invoking curl (misconfiguration, not "core is ready").
_write_stub_never_called
_run -u CORE_ROUTE_REVISION_URL -- CORE_MIN_ROUTE_REVISION=42
_assert_exit "minimum declared but URL unset" 1

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "core-revision-preflight.test.sh: all checks passed."
  exit 0
else
  echo "core-revision-preflight.test.sh: $FAILURES check(s) failed."
  exit 1
fi
