#!/usr/bin/env sh
#
# Core-version preflight (#1605).
#
# ## Why this exists
#
# A sibling can deploy against a core instance whose TEMPLATE version is
# older than the sibling's code needs — a different failure from #1604's
# route-ordering race. #1604 asks "has core shipped the specific route my
# code depends on RIGHT NOW"; this asks "is the instance running at least
# the core template capability my code was written against". The two are
# deliberately separate scripts and separate deploy jobs, because they read
# different signals and fail for different reasons — conflating them would
# make either failure's message describe the wrong problem. This script does
# NOT address, and is not a substitute for, #1604's deploy-ordering race.
#
# ## The signal
#
# `services/api/src/api/routers/health.py` returns `core_version()`, baked
# in at package time by `scripts/resolve-core-version.sh`, and is publicly
# reachable with no credential at `/api/v1/health` on every deployed
# instance (`{"status":"ok","version":"0.287.12"}`). Nothing new is
# published for this script — it reads a signal that already exists.
#
# ## Why NOT biffo.sibling.json's `template_version`
#
# The obvious-looking home for a declared floor is the sibling's own
# `template_version` field. It is the wrong field: ADR-0007 states it is
# stamped ONCE at scaffold time from `getLatestCoreVersion()`, is
# "visibility only", and explicitly "nothing reads or compares the field
# yet" — it is a backward PROVENANCE stamp that only ever falls further
# behind, not a forward REQUIREMENT that rises as a sibling adopts newer
# core capability. Measured live across all five real siblings at the time
# #1605 was picked up: four had no `template_version` at all and the fifth
# was ~80 minor versions behind the running core — reusing it as a floor
# would be inert for four siblings and trivially satisfied by the fifth.
# Mirroring #1604's already-proven shape instead: a per-environment GitHub
# Actions repo variable the sibling sets by hand, the same way
# CORE_MIN_ROUTE_REVISION is set — no JSON schema change, no ADR amendment,
# no new document.
#
# ## Fail-closed (the #1363 class)
#
# A sibling that declares no minimum has nothing to check — that is a
# legitimate "I depend on nothing beyond whatever core happens to be
# running" state, and it passes with a notice. Once a minimum IS declared,
# absence of a valid version in the health response — an unreachable
# instance, a non-packaged deployment reporting the literal string
# "unknown" (health.py's own documented fallback), a malformed body — must
# never read as "satisfied". It fails, and says WHICH: "missing/unreachable"
# and "present but behind" are different facts, and a caller reading only
# the exit code must not be able to mistake one for the other from the log.
#
# ## Version comparison
#
# Both versions are dotted non-negative-integer strings (e.g. "0.287.12"),
# the shape every `core-v*` tag and every `biffo.core.json` `.version` takes
# (see resolve-core-version.sh). Compared segment-by-segment, numerically,
# left to right, with a missing trailing segment on either side treated as
# 0 — so "0.287" and "0.287.0" compare equal, and "0.9" is correctly less
# than "0.10". This is deliberately NOT full semver (no pre-release/build
# metadata) because nothing in this estate's version strings ever carries
# either.
#
# Exit codes:
#   0 = no minimum declared (nothing to check), or the instance is at/past it.
#   1 = the instance is behind the declared minimum, its health endpoint is
#       unreachable, or its reported version could not be parsed. Every one
#       of these is "not verified as safe" and none may be treated as a pass.
#
# ## Usage
#
#   CORE_MIN_TEMPLATE_VERSION=0.250.0 \
#   CORE_HEALTH_URL=https://dev.example.com/api/v1/health \
#     sh scripts/core-version-preflight.sh
#
# CORE_MIN_TEMPLATE_VERSION — the minimum core template version this
#                              sibling's code needs. Set as a per-environment
#                              repo variable (vars.CORE_MIN_TEMPLATE_VERSION),
#                              the same way CORE_MIN_ROUTE_REVISION is set by
#                              hand. Unset/empty means "no floor declared"
#                              and the check is skipped.
# CORE_HEALTH_URL           — where to fetch the instance's health document,
#                              typically "${CORE_API_URL}/api/v1/health".
#                              Required whenever a minimum is declared.
# PREFLIGHT_CURL             — override the curl binary/wrapper (tests use
#                              this to point at a stub).
#
# Run this file's own tests: sh scripts/core-version-preflight.test.sh

set -u

CURL=${PREFLIGHT_CURL:-curl}

MIN=${CORE_MIN_TEMPLATE_VERSION:-}
if [ -z "$MIN" ]; then
  # Loud on purpose, matching core-revision-preflight.sh's own reasoning: a
  # green "Preflight — core template version" step must never look
  # identical whether it actually checked something or checked nothing.
  MSG="core-version-preflight: preflight did NOT run — CORE_MIN_TEMPLATE_VERSION is not set, so this sibling has declared no core-template-version dependency. This is 'nothing was checked', not 'core was checked and is ready'; set CORE_MIN_TEMPLATE_VERSION once this sibling depends on a specific core template version."
  echo "::notice::${MSG}"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "${MSG}" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

is_version() {
  printf '%s' "$1" | grep -Eq '^[0-9]+(\.[0-9]+)*$'
}

if ! is_version "$MIN"; then
  echo "::error::core-version-preflight: CORE_MIN_TEMPLATE_VERSION='$MIN' is not a dotted non-negative-integer version (e.g. '0.250.0')." >&2
  exit 1
fi

URL=${CORE_HEALTH_URL:-}
if [ -z "$URL" ]; then
  echo "::error::core-version-preflight: CORE_MIN_TEMPLATE_VERSION=$MIN is declared but CORE_HEALTH_URL is not set — cannot fetch the instance's health document to check it against. Refusing to proceed rather than assuming core is ready." >&2
  exit 1
fi

BODY_FILE=/tmp/core-version-preflight-body.$$
ERR_FILE=/tmp/core-version-preflight-err.$$
trap 'rm -f "$BODY_FILE" "$ERR_FILE"' EXIT

HTTP_CODE=$("$CURL" -sS --max-time 15 --retry 2 --retry-delay 2 \
  -o "$BODY_FILE" -w '%{http_code}' "$URL" 2>"$ERR_FILE")
RC=$?
ERR=$(cat "$ERR_FILE" 2>/dev/null)

if [ "$RC" -ne 0 ] || [ "$HTTP_CODE" != "200" ]; then
  echo "::error::core-version-preflight: no health response available at $URL (curl exit $RC, http ${HTTP_CODE:-none}${ERR:+, $ERR}). An absent or unreachable health endpoint is treated as NOT satisfied — this is expected on a down instance, a network issue, or a not-yet-deployed core — and is a DIFFERENT fact from 'core is behind version $MIN'. Refusing to deploy rather than assuming core is ready." >&2
  exit 1
fi

ACTUAL=$(command -v jq >/dev/null 2>&1 && jq -r 'if (.version | type) == "string" then .version else empty end' "$BODY_FILE" 2>/dev/null)

if [ -z "${ACTUAL:-}" ] || ! is_version "$ACTUAL"; then
  # Deliberately covers health.py's own documented fallback: outside a
  # packaged deployment it returns the literal string "unknown", which is
  # not a version and must fail the same way a missing/malformed body does
  # — never be silently treated as satisfied.
  echo "::error::core-version-preflight: health document at $URL did not contain a valid dotted version in 'version' (got '${ACTUAL:-<empty>}'). Treating a malformed or unknown version the same as a missing one — refusing to deploy." >&2
  exit 1
fi

version_ge() {
  # Prints "1" if $1 >= $2, "0" otherwise — dotted numeric segments compared
  # left to right, missing trailing segments on either side treated as 0.
  awk -v a="$1" -v b="$2" '
    BEGIN {
      na = split(a, pa, ".")
      nb = split(b, pb, ".")
      n = (na > nb) ? na : nb
      for (i = 1; i <= n; i++) {
        x = (i <= na) ? pa[i] + 0 : 0
        y = (i <= nb) ? pb[i] + 0 : 0
        if (x > y) { print "1"; exit }
        if (x < y) { print "0"; exit }
      }
      print "1"
    }
  '
}

if [ "$(version_ge "$ACTUAL" "$MIN")" = "1" ]; then
  echo "core-version-preflight: OK — core is at version $ACTUAL, this sibling needs at least $MIN."
  exit 0
fi

echo "::error::core-version-preflight: core is at version $ACTUAL, but this sibling needs at least $MIN. The instance has not upgraded to the core template version this sibling depends on yet — refusing to deploy ahead of it." >&2
exit 1
