#!/usr/bin/env sh
#
# Run the Biffo CLI, from whichever copy is correct for this repo.
#
#   - **Template** (no `biffo.core.json`): the local `cli/` workspace, through
#     tsx. The template develops the CLI, so its own CI has to exercise the code
#     in the working tree — running the published package here would test the
#     last release instead of the change under review, and a broken guard would
#     go green and only fail after publish.
#
#   - **Instance** (`biffo.core.json` present): the published package, pinned to
#     the core version this instance is actually on. Instances no longer carry
#     `cli/` at all: it is 31k lines of a scaffolding tool they never develop
#     and never deploy, and shipping it meant the template's own test suite ran
#     in every tenant's CI — where a failure could not be fixed by the repo it
#     failed in.
#
# Pinning to `biffo.core.json` rather than `@latest` keeps the guards in step
# with the core the instance runs. An instance mid-upgrade is checked by the
# version it is upgrading from, which is the one whose rules its tree follows.
#
# That last sentence is why the version is read from `HEAD` and not the working
# tree (#667). `biffo core upgrade --apply` rewrites `biffo.core.json` to the
# TARGET version before committing, so by the time the `commit-msg` hook runs
# the working tree already names a version that, by definition, may not be
# published yet:
#
#     npm error notarget No matching version found for @biffo/cli@0.133.1
#     husky - commit-msg script failed (code 1)
#
# The upgrade then cannot be committed at all, and `--no-verify` does not help
# because CI runs the same check. Reading HEAD judges the commit by the rules
# the tree currently follows, and decouples committing an upgrade from whether
# the target has reached npm yet.
set -eu

root=$(git rev-parse --show-toplevel)

# Where the CALLER stood, before this script normalises to the repo root.
#
# Some packaged scripts take their target from the working directory rather than
# from a repo layout -- the dependency audits are invoked from a CI job's
# `working-directory: apps/frontend`, and one byte-identical script is correct
# at a repo root, in a sibling's frontend and in a service directory alike. The
# `cd` below would silently retarget them at the repo root, which is a wrong
# ANSWER rather than an error: the audit would pass, having audited the wrong
# tree. packagedScriptCommand() spawns each script back in this directory.
BIFFO_ORIGINAL_CWD=$PWD
export BIFFO_ORIGINAL_CWD

cd "$root"

# Satellites (sibling apps, plugin repos, runner fleets) carry neither
# `biffo.core.json` nor `cli/`. They DO carry `.biffo-shared-version`, written
# by `scripts/shared-sync.sh` and naming the template core version their shared
# files came from — the same pin, under a different name.
#
# Without this branch a satellite fell through to the template's `pnpm --filter`
# line and failed, so every guard a satellite ran had to be a COPIED shell
# script kept in step by hand. That is the drift surface `shared-files.json`
# exists to police: 16 files across 15 repos, roughly 240 copies, and about ten
# of the estate's guards written to police them. Giving satellites the same
# versioned CLI instances already use is what makes those copies unnecessary
# (#1109).
#
# The tag form is `core-v0.231.2`; npm wants `0.231.2`.
if [ ! -f biffo.core.json ] && [ -f .biffo-shared-version ]; then
  shared=$(tr -d ' \t\n\r' < .biffo-shared-version)
  version=${shared#core-v}
  if [ -z "$version" ]; then
    echo "biffo.sh: .biffo-shared-version is present but carries no readable version." >&2
    exit 2
  fi
  exec npx --yes "@biffo/cli@$version" "$@"
fi

if [ -f biffo.core.json ]; then
  # The committed record first. Falls through to the working tree when there is
  # no HEAD yet (a fresh `biffo init`, before the first commit) or when the file
  # is newly added and not yet in any commit.
  version=$(git show HEAD:biffo.core.json 2>/dev/null |
    node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version" 2>/dev/null || echo '')
  if [ -z "$version" ] || [ "$version" = 'undefined' ]; then
    version=$(node -p "require('./biffo.core.json').version" 2>/dev/null || echo '')
  fi
  if [ -z "$version" ] || [ "$version" = 'undefined' ]; then
    echo "biffo.sh: biffo.core.json is present but carries no readable version." >&2
    exit 2
  fi
  exec npx --yes "@biffo/cli@$version" "$@"
fi

# Nothing above matched, so this must be the template — the only repo that
# carries `cli/`. A satellite that reaches here has no pin, which since #1109
# means it cannot run guards at all; before this branch it fell through and
# exec'd a `tsx` that does not exist, exiting 127 with no explanation.
#
# `biffo sibling create` now stamps the pin at birth, so this is reachable
# mainly by a repo scaffolded before that, or by a plugin repo (`biffo plugin
# create` scaffolds INTO an existing repo, so it has no standalone repo root to
# stamp). `shared-sync.sh` writes the pin on its first run, which is the fix.
if [ ! -d cli ]; then
  echo "biffo.sh: no biffo.core.json, no .biffo-shared-version, and no cli/ here." >&2
  echo "  If this is a satellite, run shared-sync from the template to stamp it:" >&2
  echo "    sh scripts/shared-sync.sh --estate <path-to-your-repos>" >&2
  exit 2
fi

# NOT `pnpm --filter @biffo/cli exec tsx ...`: `pnpm exec` normalises every
# non-zero exit to 1. Verified — the CLI exits 2 and pnpm reports 1.
#
# That silently flattened the exit code of every guard this bridge runs in the
# template, and it matters now because the estate's conventions are built on a
# THREE-valued contract: 0 green, 1 failed, **2 cannot tell**, where 2 is never
# a pass (`wait-for-checks.sh`, `branch-health.sh`, `claim.sh`). Collapsing 2
# into 1 turns "I could not tell" into "it failed", which is the safe direction
# by luck rather than design — and the reverse collapse would be a fail-open.
#
# `exec` on the binary directly keeps the child's status as this script's own.
exec "$root/cli/node_modules/.bin/tsx" "$root/cli/src/index.ts" "$@"
