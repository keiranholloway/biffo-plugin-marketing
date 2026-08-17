#!/usr/bin/env sh
#
# Core-revision preflight (#1604).
#
# ## Why this exists
#
# A sibling's deploy is structurally incapable of seeing core's deploy state:
# `resolve-environment -> deploy-infra -> deploy-app` runs with no step that
# reads anything about core, and its only post-deploy proof is its OWN Lambda
# health and its OWN CDN route. Merge ordering does not fix this either — in
# the recorded incident the core PR had already merged and the core deploy
# simply sat in the runner queue while the sibling went live first, serving
# requests against routes core had not shipped yet.
#
# The owner's decision (#1604): core publishes a DIGEST of its route table to
# the existing public `.well-known/` channel — a monotonic revision counter
# paired with a content hash, never the route surface itself (`/openapi.json`
# stays 401-gated). This script is the consumer half: it asks "is core at
# least at the revision I need", using ONLY that published digest, run
# BEFORE `deploy-app` so a sibling that would go live ahead of core never
# gets that far.
#
# ## Why "revision", not the hash
#
# The hash proves the route table is DIFFERENT from some prior observation;
# it cannot prove "at or past", because "different" has no direction. The
# published `revision` is core's `git rev-list --count HEAD` at deploy time
# (scripts/route-table-digest.py) — the number of commits reachable from the
# deployed commit, not a counter that increments once per deploy. That
# distinction is the whole point: a deploy counter goes up on every run
# regardless of what shipped, so a ROLLBACK (a new deploy of an OLDER
# commit) would report a revision higher than the one that actually had the
# routes a sibling needs — a false "yes, at or past" fired exactly when the
# true answer is "no". A commit count is tied to the code's real position in
# history: redeploying the same commit reports the same revision (correctly
# "at least as new" — an equality, not a regression); rolling back to an
# older commit reports a LOWER revision, because that commit genuinely has
# fewer ancestors. This script only ever compares that one integer.
#
# ## This script's monotonicity guarantee is borrowed, not owned
#
# `git rev-list --count HEAD` is only monotonic/collision-free along one
# continuous, non-rewritten history. If core's `dev`/`staging`/`main` ever
# allowed force pushes or a non-linear history, a rewritten branch could
# report a LOWER commit count for a commit that is, in reality, later —
# silently undermining the one property this whole mechanism depends on,
# with no signal anywhere in this script or its caller. Checked directly
# against the live `keiranholloway/biffo-platform` repo during #1635's
# prosecution: `allow_force_pushes: false`, `required_linear_history: true`
# and `enforce_admins: true` are all set on every one of those branches
# today, so the guarantee currently holds — but nothing in this script (or
# in scripts/route-table-digest.py, which computes the number on the
# publishing side) asserts that, or would notice if it changed. Deliberately
# NOT asserted with a live GitHub API call here: this script runs in every
# sibling's deploy job, on every deploy, and a branch-protection check would
# add a second network dependency and a second way to fail closed for a
# property that is core's to guarantee, not each sibling's to re-verify on
# every run. If this ever needs enforcing rather than documenting, it
# belongs as a periodic check against the CORE repo (once, not once per
# sibling deploy) — not bolted onto this script.
#
# ## Fail-closed (the #1363 class)
#
# A sibling that declares no minimum has nothing to check — that is a
# legitimate "I depend on nothing new from core" state, and it passes with a
# notice. But once a minimum IS declared, absence of the digest — an older
# core, a failed publish, a CDN cache miss — must never read as "satisfied".
# It fails, and says WHICH: "missing/unreachable" and "present but behind"
# are different facts, and a caller reading only the exit code must not be
# able to mistake one for the other from the log.
#
# Exit codes:
#   0 = no minimum declared (nothing to check), or core is at/past it.
#   1 = core is behind the declared minimum, the digest is missing or
#       unreachable, or the digest could not be parsed. Every one of these
#       is "not verified as safe" and none may be treated as a pass.
#
# ## Usage
#
#   CORE_MIN_ROUTE_REVISION=42 \
#   CORE_ROUTE_REVISION_URL=https://dev.example.com/.well-known/route-revision.json \
#     sh scripts/core-revision-preflight.sh
#
# CORE_MIN_ROUTE_REVISION — the minimum core revision this sibling's code
#                            needs. Set as a per-environment repo variable
#                            (vars.CORE_MIN_ROUTE_REVISION), the same way a
#                            version pin is set by hand: after merging the
#                            core PR your sibling code depends on, read the
#                            `revision` core published for that deploy and
#                            record it here. Unset/empty means "no new
#                            dependency" and the check is skipped.
# CORE_ROUTE_REVISION_URL  — where to fetch the digest, typically
#                            "${CORE_PORTAL_URL}/.well-known/route-revision.json".
#                            Required whenever a minimum is declared.
# PREFLIGHT_CURL           — override the curl binary/wrapper (tests use
#                            this to point at a stub).
#
# Run this file's own tests: sh scripts/core-revision-preflight.test.sh

set -u

CURL=${PREFLIGHT_CURL:-curl}

MIN=${CORE_MIN_ROUTE_REVISION:-}
if [ -z "$MIN" ]; then
  # Loud on purpose (#1635 prosecution finding 2). A green "Preflight — core
  # route revision" step looks identical whether it actually checked
  # something or checked nothing — the whole point of #1604 is to stop a
  # sibling deploying blind, so its own "nothing to check" state must never
  # read like "checked and fine". Same treatment this repo already gives the
  # structurally identical deliberately-off case
  # (SIBLING_DEPLOY_ENABLED=false in _skeletons/sibling-template's own
  # deploy.yml): a GitHub Actions ::notice:: annotation, not a bare echo that
  # only shows up if someone opens the step log.
  MSG="core-revision-preflight: preflight did NOT run — CORE_MIN_ROUTE_REVISION is not set, so this sibling has declared no core-route dependency. This is 'nothing was checked', not 'core was checked and is ready'; set CORE_MIN_ROUTE_REVISION once this sibling depends on a specific core revision."
  echo "::notice::${MSG}"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "${MSG}" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

case "$MIN" in
  ''|*[!0-9]*)
    echo "::error::core-revision-preflight: CORE_MIN_ROUTE_REVISION='$MIN' is not a non-negative integer." >&2
    exit 1
    ;;
esac

URL=${CORE_ROUTE_REVISION_URL:-}
if [ -z "$URL" ]; then
  echo "::error::core-revision-preflight: CORE_MIN_ROUTE_REVISION=$MIN is declared but CORE_ROUTE_REVISION_URL is not set — cannot fetch the digest to check it against. Refusing to proceed rather than assuming core is ready." >&2
  exit 1
fi

BODY_FILE=/tmp/core-revision-preflight-body.$$
ERR_FILE=/tmp/core-revision-preflight-err.$$
trap 'rm -f "$BODY_FILE" "$ERR_FILE"' EXIT

HTTP_CODE=$("$CURL" -sS --max-time 15 --retry 2 --retry-delay 2 \
  -o "$BODY_FILE" -w '%{http_code}' "$URL" 2>"$ERR_FILE")
RC=$?
ERR=$(cat "$ERR_FILE" 2>/dev/null)

if [ "$RC" -ne 0 ] || [ "$HTTP_CODE" != "200" ]; then
  echo "::error::core-revision-preflight: no route-revision digest available at $URL (curl exit $RC, http ${HTTP_CODE:-none}${ERR:+, $ERR}). An absent or unreachable digest is treated as NOT satisfied — this is expected on an older core, a failed publish, or a CDN cache miss, and is a DIFFERENT fact from 'core is behind revision $MIN'. Refusing to deploy rather than assuming core is ready." >&2
  exit 1
fi

ACTUAL=$(command -v jq >/dev/null 2>&1 && jq -r 'if (.revision | type) == "number" then (.revision | floor) else empty end' "$BODY_FILE" 2>/dev/null)

case "${ACTUAL:-}" in
  ''|*[!0-9]*)
    echo "::error::core-revision-preflight: digest at $URL did not contain a valid integer 'revision' field. Treating a malformed digest the same as a missing one — refusing to deploy." >&2
    exit 1
    ;;
esac

if [ "$ACTUAL" -ge "$MIN" ]; then
  echo "core-revision-preflight: OK — core is at revision $ACTUAL, this sibling needs at least $MIN."
  exit 0
fi

echo "::error::core-revision-preflight: core is at revision $ACTUAL, but this sibling needs at least $MIN. Core has not deployed the routes this sibling depends on yet — refusing to deploy ahead of it." >&2
exit 1
