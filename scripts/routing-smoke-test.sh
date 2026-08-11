#!/usr/bin/env sh
#
# Does the sibling actually route, not just deploy?
#
# ## Why this exists (#737 item 3)
#
# The Lambda smoke test added by #162 (`deploy.yml`'s "Smoke test the deployed
# Lambda" step) proves the Lambda boots and answers a direct API Gateway
# request. It says nothing about the CDN path a real user hits:
# `dev.<domain>/<path>`. One sibling went fully green three separate times
# while that path was broken in three different ways:
#
#   1. CDN registration no-op    — the path never got a CloudFront behaviour,
#                                   so the request silently fell through to
#                                   the PARENT's default behaviour (the root
#                                   app) instead of 404ing.
#   2. Path-prefix mismatch      — the app's basePath didn't match the prefix
#                                   CloudFront forwards, so the prefixed path
#                                   404'd even though the app was live.
#   3. Bare-path routing         — the prefix resolved with a trailing slash
#                                   but not without one (or vice versa).
#
# A generic "does the origin respond" check passes in all three cases —
# variant 1 gets a real 200 (from the WRONG origin), and variants 2/3 differ
# only in exactly which shape of the URL is broken. So this checks three
# specific routes, not one generic one, and prints how many it checked and
# which — "routing OK" over a denominator of zero is the #1363 class.
#
# ## Fail-closed
#
# 0 = every route checked resolved correctly.
# 1 = a route was checked and did NOT resolve correctly (a real routing bug).
# 2 = could not tell — DNS didn't resolve, the deployment was unreachable, or
#     the expected path could not be determined. 2 is NEVER a pass; callers
#     (deploy.yml) must treat it as a failure exactly like 1.
#
# ## Usage
#
#   ROUTING_BASE_URL=https://dev.example.com \
#   ROUTING_PATH_PREFIX=reports \
#     sh scripts/routing-smoke-test.sh
#
# ROUTING_BASE_URL   — the parent CDN origin the sibling is served through
#                       (CORE_PORTAL_URL in deploy.yml). Required.
# ROUTING_PATH_PREFIX — the sibling's routed path segment (PATH_PREFIX in
#                       deploy.yml). Required to be SET, but may be the empty
#                       string — the root application sibling (issue #306)
#                       legitimately has one. Unset (vs set-empty) is treated
#                       as "cannot determine the expected path" and exits 2,
#                       because those are different facts: one is a known
#                       root sibling, the other is a workflow that forgot to
#                       pass the variable.
# ROUTING_CURL        — override the curl binary/wrapper (tests use this to
#                       point at a stub).
#
# Run this file's own tests: sh scripts/routing-smoke-test.test.sh

set -u

CURL=${ROUTING_CURL:-curl}
CHECKED=0
FAILED=0
CANNOT_TELL=0

_report() {
  # _report <status> <label> <detail>
  status=$1
  label=$2
  detail=$3
  case "$status" in
    ok) echo "  [OK]   $label — $detail" ;;
    fail) echo "  [FAIL] $label — $detail"; FAILED=$((FAILED + 1)) ;;
    cannot-tell) echo "  [????] $label — $detail"; CANNOT_TELL=$((CANNOT_TELL + 1)) ;;
  esac
}

# _fetch <url> -> prints "<http_status>\n<body_sha256>" or "" on transport
# failure (DNS, connection refused, timeout — curl exit != 0).
_fetch() {
  url=$1
  out=$("$CURL" -sS --max-time 15 --retry 2 --retry-delay 2 \
    -o /tmp/routing-smoke-body.$$ -w '%{http_code}' "$url" 2>/tmp/routing-smoke-err.$$)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    err=$(cat /tmp/routing-smoke-err.$$ 2>/dev/null)
    rm -f /tmp/routing-smoke-body.$$ /tmp/routing-smoke-err.$$
    echo "TRANSPORT_ERROR: $err"
    return 1
  fi
  body_hash=$(cksum /tmp/routing-smoke-body.$$ 2>/dev/null | awk '{print $1"-"$2}')
  rm -f /tmp/routing-smoke-body.$$ /tmp/routing-smoke-err.$$
  echo "$out $body_hash"
  return 0
}

if [ -z "${ROUTING_BASE_URL+x}" ] || [ -z "$ROUTING_BASE_URL" ]; then
  echo "::error::routing-smoke-test: ROUTING_BASE_URL is not set — cannot determine which deployment to check." >&2
  exit 2
fi

if [ -z "${ROUTING_PATH_PREFIX+x}" ]; then
  echo "::error::routing-smoke-test: ROUTING_PATH_PREFIX is unset (not even empty) — cannot determine the expected path. If this is genuinely the root sibling, pass ROUTING_PATH_PREFIX='' explicitly." >&2
  exit 2
fi

BASE=${ROUTING_BASE_URL%/}
PREFIX=$ROUTING_PATH_PREFIX

echo "routing-smoke-test: base=$BASE prefix='${PREFIX}'"

# --- Route A: bare path, no trailing slash --------------------------------
# Directly the "bare-path routing" recorded variant.
if [ -n "$PREFIX" ]; then
  BARE_URL="$BASE/$PREFIX"
else
  BARE_URL="$BASE"
fi
CHECKED=$((CHECKED + 1))
result=$(_fetch "$BARE_URL")
if [ $? -ne 0 ]; then
  _report cannot-tell "bare path ($BARE_URL)" "$result"
else
  code=${result%% *}
  case "$code" in
    2*) _report ok "bare path ($BARE_URL)" "HTTP $code" ;;
    *) _report fail "bare path ($BARE_URL)" "HTTP $code — expected 2xx" ;;
  esac
fi

# --- Route B: trailing slash, path prefix serving -------------------------
# Directly the "path-prefix mismatch" recorded variant: the app's basePath
# not matching what CloudFront forwards shows up as this route 404ing even
# though the app is live and the bare path (or root) is fine.
if [ -n "$PREFIX" ]; then
  PREFIXED_URL="$BASE/$PREFIX/"
  CHECKED=$((CHECKED + 1))
  result=$(_fetch "$PREFIXED_URL")
  if [ $? -ne 0 ]; then
    _report cannot-tell "path prefix ($PREFIXED_URL)" "$result"
  else
    code=${result%% *}
    case "$code" in
      2*) _report ok "path prefix ($PREFIXED_URL)" "HTTP $code" ;;
      *) _report fail "path prefix ($PREFIXED_URL)" "HTTP $code — expected 2xx" ;;
    esac
  fi
else
  echo "  [SKIP] path prefix — PATH_PREFIX is empty (root sibling, issue #306); trailing-slash form is identical to the bare-path check above, by design."
fi

# --- Route C: CDN behaviour actually registered ---------------------------
# The CDN-no-op variant: if no distinct CloudFront behaviour was ever
# registered for this path, the request falls through to the PARENT's
# default behaviour and returns a real 200 — from the WRONG origin. A bare
# status check cannot see this; it must compare against a known-different
# response. Only meaningful when PREFIX is non-empty (the root sibling IS
# the default behaviour, so there is nothing to differ from — that case is
# recorded as unproven below, not silently skipped as a pass).
if [ -n "$PREFIX" ]; then
  ROOT_URL="$BASE/"
  CHECKED=$((CHECKED + 1))
  prefixed_result=$(_fetch "$BASE/$PREFIX/")
  prefixed_rc=$?
  root_result=$(_fetch "$ROOT_URL")
  root_rc=$?
  if [ "$prefixed_rc" -ne 0 ] || [ "$root_rc" -ne 0 ]; then
    _report cannot-tell "CDN behaviour registered (vs $ROOT_URL)" "could not fetch one or both paths for comparison"
  else
    prefixed_hash=${prefixed_result#* }
    root_hash=${root_result#* }
    prefixed_code=${prefixed_result%% *}
    case "$prefixed_code" in
      2*)
        if [ "$prefixed_hash" = "$root_hash" ]; then
          _report fail "CDN behaviour registered" "response at $BASE/$PREFIX/ is byte-identical to $ROOT_URL — CloudFront is serving the PARENT's default behaviour, not this sibling (registration no-op)"
        else
          _report ok "CDN behaviour registered" "response at $BASE/$PREFIX/ differs from $ROOT_URL"
        fi
        ;;
      *)
        _report fail "CDN behaviour registered" "prefixed path did not return 2xx (HTTP $prefixed_code), cannot compare bodies"
        ;;
    esac
  fi
else
  echo "  [UNPROVEN] CDN behaviour registered — PATH_PREFIX is empty (root sibling), so there is no distinct-from-root comparison this script can make. Root-sibling CDN registration is NOT covered by this check."
fi

echo
echo "routing-smoke-test: checked $CHECKED route(s) against $BASE (prefix='${PREFIX}')"

if [ "$CANNOT_TELL" -gt 0 ]; then
  echo "::error::routing-smoke-test: $CANNOT_TELL of $CHECKED check(s) could not be evaluated (unreachable / DNS / transport error) — treating as failure, never a pass."
  exit 2
fi

if [ "$FAILED" -gt 0 ]; then
  echo "::error::routing-smoke-test: $FAILED of $CHECKED route check(s) failed."
  exit 1
fi

if [ "$CHECKED" -eq 0 ]; then
  echo "::error::routing-smoke-test: 0 routes were checked — refusing to report success over an empty denominator (#1363)."
  exit 2
fi

echo "routing-smoke-test: all $CHECKED checked route(s) OK."
exit 0
