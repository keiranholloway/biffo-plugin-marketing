#!/usr/bin/env sh
#
# Proves core-version-preflight.sh (#1605) actually blocks a sibling from
# deploying against an instance whose core template version is too old,
# passes it once the instance has caught up, and fails CLOSED — never open
# — when the health document is missing, unreachable, or reports the
# "unknown" version health.py falls back to outside a packaged deployment.
#
# Stubs `curl` on PATH so this needs no network and no live deployment.
#
# Run: sh scripts/core-version-preflight.test.sh

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET="$SCRIPT_DIR/core-version-preflight.sh"
STUB_DIR=$(mktemp -d)
OUT_FILE=/tmp/core-version-preflight-test-out.$$
trap 'rm -rf "$STUB_DIR"; rm -f "$OUT_FILE"' EXIT

FAILURES=0
URL="https://dev.example.com/api/v1/health"

# _run <env assignments...> -- runs the target with PREFLIGHT_CURL pointed at
# the stub, capturing combined output to $OUT_FILE and the exit code into
# $LAST_RC. NOT run inside a subshell/command-substitution, matching
# core-revision-preflight.test.sh's own reasoning: an assertion made inside
# `out=$(...)` runs FAILURES++ in a subshell that never reaches the parent,
# so a real regression could not fail the test.
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
  version=$1
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
printf '{"status": "ok", "version": "$version"}' > "\$outfile"
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

# 1. No CORE_MIN_TEMPLATE_VERSION declared at all — this sibling depends on
#    no specific core template version. Must pass WITHOUT even invoking
#    curl, and must say so LOUDLY: a GitHub Actions ::notice:: annotation
#    (not a bare echo a reader would have to open the step log to find),
#    and never wording that could be mistaken for "checked and fine".
_write_stub_never_called
_run -u CORE_MIN_TEMPLATE_VERSION -u CORE_HEALTH_URL --
_assert_exit "no minimum declared" 0
_assert_output_contains "no minimum declared — is a ::notice:: annotation" "::notice::"
_assert_output_contains "no minimum declared — says preflight did not run" "did NOT run"

# 1b. Same scenario, but with GITHUB_STEP_SUMMARY set as it is in a real
#     GitHub Actions run — the skip must ALSO land in the job summary.
SUMMARY_FILE=/tmp/core-version-preflight-test-summary.$$
: > "$SUMMARY_FILE"
_write_stub_never_called
_run -u CORE_MIN_TEMPLATE_VERSION -u CORE_HEALTH_URL -- GITHUB_STEP_SUMMARY="$SUMMARY_FILE"
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

# 2. Sibling requires a version core HAS reached (core is AHEAD) — passes.
_write_stub_ok "0.287.12"
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "core ahead of required version (0.250.0 <= 0.287.12)" 0
_assert_output_contains "core ahead — names actual" "version 0.287.12"

# 2b. Exact match — core is AT the required version — passes.
_write_stub_ok "0.250.0"
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "core exactly at required version" 0

# 2c. Lexicographic trap: "0.9" must compare LESS than "0.10" numerically,
#     not greater as a naive string compare would report.
_write_stub_ok "0.9.0"
_run CORE_MIN_TEMPLATE_VERSION=0.10.0 CORE_HEALTH_URL="$URL"
_assert_exit "0.9.0 is numerically behind 0.10.0 (string compare would say ahead)" 1

# 2d. Missing trailing segment treated as 0 — "0.287" == "0.287.0".
_write_stub_ok "0.287"
_run CORE_MIN_TEMPLATE_VERSION=0.287.0 CORE_HEALTH_URL="$URL"
_assert_exit "0.287 satisfies a 0.287.0 floor (missing segment = 0)" 0

# 3. Sibling requires a version core has NOT reached — fails, naming both
#    the required and actual versions.
_write_stub_ok "0.208.1"
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "core behind required version" 1
_assert_output_contains "core behind — names actual" "version 0.208.1"
_assert_output_contains "core behind — names required" "at least 0.250.0"

# 4. No health document available at all (transport failure) — fails
#    CLOSED, and says the health endpoint was missing rather than that core
#    is behind.
_write_stub_unreachable
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "health endpoint unreachable" 1
_assert_output_contains "health endpoint unreachable — says missing, not behind" "no health response available"

# 5. Health endpoint 404s — fails CLOSED, same "missing" message as
#    scenario 4, never "core is behind".
_write_stub_404
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "health endpoint 404" 1
_assert_output_contains "health endpoint 404 — says missing" "no health response available"

# 6. Instance reports the literal "unknown" version — health.py's own
#    documented fallback outside a packaged deployment. Must fail CLOSED
#    exactly like a malformed/missing body, never be treated as satisfied.
_write_stub_ok "unknown"
_run CORE_MIN_TEMPLATE_VERSION=0.250.0 CORE_HEALTH_URL="$URL"
_assert_exit "instance reports 'unknown' version" 1
_assert_output_contains "'unknown' version — treated as malformed, not satisfied" "did not contain a valid dotted version"

# 7. A minimum is declared but CORE_HEALTH_URL is not set — must fail
#    without invoking curl (misconfiguration, not "core is ready").
_write_stub_never_called
_run -u CORE_HEALTH_URL -- CORE_MIN_TEMPLATE_VERSION=0.250.0
_assert_exit "minimum declared but URL unset" 1

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "core-version-preflight.test.sh: all checks passed."
  exit 0
else
  echo "core-version-preflight.test.sh: $FAILURES check(s) failed."
  exit 1
fi
