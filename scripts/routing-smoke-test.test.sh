#!/usr/bin/env sh
#
# Proves routing-smoke-test.sh (#737 item 3) actually catches the three
# recorded routing failures, and that it fails closed when it cannot tell.
#
# A verification script nobody has seen fail is not verified — every FAIL
# scenario below first ran (uncommented) against a plain `curl` stub before
# the real script existed, to confirm it does NOT catch these shapes without
# the fix; that manual step is recorded here in prose since there is nothing
# upstream of this script to diff against.
#
# Stubs `curl` on PATH so this needs no network and no live deployment.
#
# Run: sh scripts/routing-smoke-test.test.sh

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TARGET="$SCRIPT_DIR/routing-smoke-test.sh"
STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT

FAILURES=0
_assert_exit() {
  # _assert_exit <scenario-name> <expected-exit> -- <env assignments already exported>
  name=$1
  expected=$2
  actual_out=$(ROUTING_CURL="$STUB_DIR/curl" sh "$TARGET" 2>&1)
  actual=$?
  if [ "$actual" -eq "$expected" ]; then
    echo "PASS: $name (exit $actual)"
  else
    echo "FAIL: $name — expected exit $expected, got $actual"
    echo "--- output ---"
    echo "$actual_out"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
  fi
}

BASE="https://dev.example.com"
PREFIX="reports"

# --- Stub curl factory ------------------------------------------------
# Each scenario writes its own stub before calling the target, since the
# response shape (which URL gets which body/status) IS the scenario.
_write_stub() {
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
url="$prev"

# ROUTING_TEST_MODE selects the fixture; each case below writes $outfile
# and prints the http status, mirroring curl -w '%{http_code}'.
case "$ROUTING_TEST_MODE" in
  good)
    case "$url" in
      https://dev.example.com/reports)   printf 'REPORTS-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/reports/)  printf 'REPORTS-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/)          printf 'ROOT-APP'    > "$outfile"; printf '200'; exit 0 ;;
    esac
    ;;
  cdn-noop)
    # Every path — including the prefixed one — actually serves the root
    # app's content, because CloudFront never registered a distinct
    # behaviour for /reports and silently fell through to the default.
    case "$url" in
      https://dev.example.com/reports)   printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/reports/)  printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/)          printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
    esac
    ;;
  prefix-mismatch)
    # The app serves a DIFFERENT basePath than PATH_PREFIX declares, so the
    # trailing-slash form 404s even though bare and root are fine.
    case "$url" in
      https://dev.example.com/reports)   printf 'REPORTS-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/reports/)  printf 'Not Found' > "$outfile"; printf '404'; exit 0 ;;
      https://dev.example.com/)          printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
    esac
    ;;
  bare-path-broken)
    # Trailing-slash form works, bare form 404s (or vice versa — either
    # asymmetry is the recorded "bare-path routing" variant).
    case "$url" in
      https://dev.example.com/reports)   printf 'Not Found' > "$outfile"; printf '404'; exit 0 ;;
      https://dev.example.com/reports/)  printf 'REPORTS-APP' > "$outfile"; printf '200'; exit 0 ;;
      https://dev.example.com/)          printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
    esac
    ;;
  unreachable)
    printf 'curl: (6) Could not resolve host: dev.example.com' >&2
    exit 6
    ;;
esac
echo "unhandled URL in stub: $url" >&2
exit 99
STUB
  chmod +x "$STUB_DIR/curl"
}

# 1. GOOD deployment — all three checks should pass, exit 0.
_write_stub
ROUTING_TEST_MODE=good ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="$PREFIX" \
  _assert_exit "good deployment" 0

# 2. CDN registration no-op — prefixed path byte-identical to root.
#    This is the variant a generic "does it 200" check CANNOT catch, because
#    every URL here genuinely returns 200. Confirms the fix, not just a
#    curl failure.
_write_stub
ROUTING_TEST_MODE=cdn-noop ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="$PREFIX" \
  _assert_exit "CDN registration no-op" 1

# 3. Path-prefix mismatch — trailing-slash form 404s.
_write_stub
ROUTING_TEST_MODE=prefix-mismatch ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="$PREFIX" \
  _assert_exit "path-prefix mismatch" 1

# 4. Bare-path routing — bare form 404s while trailing-slash works.
_write_stub
ROUTING_TEST_MODE=bare-path-broken ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="$PREFIX" \
  _assert_exit "bare-path routing broken" 1

# 5. Unreachable deployment — DNS/transport failure must fail CLOSED (exit
#    2, cannot-tell), never exit 0.
_write_stub
ROUTING_TEST_MODE=unreachable ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="$PREFIX" \
  _assert_exit "unreachable deployment" 2

# 6. ROUTING_PATH_PREFIX genuinely unset (not empty) — cannot determine the
#    expected path, must exit 2 without even invoking curl.
_write_stub
env -u ROUTING_PATH_PREFIX ROUTING_TEST_MODE=good ROUTING_BASE_URL="$BASE" \
  sh -c 'ROUTING_CURL="'"$STUB_DIR"'/curl" sh "'"$TARGET"'"' >/tmp/routing-smoke-t6.$$  2>&1
rc=$?
if [ "$rc" -eq 2 ]; then
  echo "PASS: PATH_PREFIX unset (exit 2)"
else
  echo "FAIL: PATH_PREFIX unset — expected exit 2, got $rc"
  cat /tmp/routing-smoke-t6.$$
  FAILURES=$((FAILURES + 1))
fi
rm -f /tmp/routing-smoke-t6.$$

# 7. ROUTING_BASE_URL unset entirely — must exit 2.
_write_stub
env -u ROUTING_BASE_URL ROUTING_TEST_MODE=good ROUTING_PATH_PREFIX="$PREFIX" \
  sh -c 'ROUTING_CURL="'"$STUB_DIR"'/curl" sh "'"$TARGET"'"' >/tmp/routing-smoke-t7.$$ 2>&1
rc=$?
if [ "$rc" -eq 2 ]; then
  echo "PASS: BASE_URL unset (exit 2)"
else
  echo "FAIL: BASE_URL unset — expected exit 2, got $rc"
  cat /tmp/routing-smoke-t7.$$
  FAILURES=$((FAILURES + 1))
fi
rm -f /tmp/routing-smoke-t7.$$

# 8. Root sibling (PATH_PREFIX='', deliberately empty not unset) — the
#    CDN-behaviour-registered check has nothing to compare against and must
#    say so rather than silently passing; bare-path check still runs
#    (denominator stays > 0), so exit is 0 when that succeeds.
_write_stub
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
url="$prev"
case "$url" in
  https://dev.example.com) printf 'ROOT-APP' > "$outfile"; printf '200'; exit 0 ;;
esac
echo "unhandled URL in stub: $url" >&2
exit 99
STUB
chmod +x "$STUB_DIR/curl"
out=$(ROUTING_CURL="$STUB_DIR/curl" ROUTING_BASE_URL="$BASE" ROUTING_PATH_PREFIX="" sh "$TARGET" 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "UNPROVEN"; then
  echo "PASS: root sibling (exit 0, unproven CDN check reported)"
else
  echo "FAIL: root sibling — expected exit 0 with UNPROVEN noted, got exit $rc"
  echo "$out"
  FAILURES=$((FAILURES + 1))
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "routing-smoke-test.test.sh: all checks passed."
  exit 0
else
  echo "routing-smoke-test.test.sh: $FAILURES check(s) failed."
  exit 1
fi
