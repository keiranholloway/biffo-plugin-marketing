# The error-branch ratchet

`error-branch-baseline.json` beside this file is **a measurement, not a
target**. It records the error-handling branches this repo's test suite has
never executed, as of the last time somebody re-took it. CI's
`Error-branch coverage` step compares today's set against it and fails **only
on an entry that is not already in the list** — a branch already there never
blocks anybody.

## What counts as an error branch

`scripts/error_branch_coverage.py` parses every Python file that appears in
`coverage.json` and looks for two shapes, both of which have produced real
fail-opens in this estate:

- an `except` handler, whose body runs only when something raised;
- an `if` whose single statement is `return <constant>` — the "if we cannot
  tell, say yes" fallback.

A branch is listed when coverage.py reports its first body line as *missing*.
An unexecuted error branch is not automatically a defect, but it is
**unverified**: nobody has ever observed what it does.

## Why a ratchet and not a gate

Some error branches are legitimately untested — an `except ImportError` around
an optional dependency, a defensive re-raise. A hard gate at this precision is
red every morning, and a guard that is red every morning is one people learn to
scroll past, taking the real findings with it. So this fails on **growth**
only: new unverified error handling has to be a deliberate, visible choice.

## How to shrink it

The list is meant to get shorter. To remove an entry:

1. write a test that actually drives the handler — raise the thing it catches,
   or arrange the condition the fallback answers;
2. re-measure and re-record:

   ```sh
   uv run pytest --cov --cov-report=json
   uv run python scripts/error_branch_coverage.py --write
   ```

3. commit the shrunken `error-branch-baseline.json` with the test.

`--write` rewrites the file from whatever it currently measures, so **it
accepts additions as readily as removals**. Running it to make a red CI green
is how a ratchet quietly becomes a rubber stamp; do it only when you have
either covered something or decided, in the PR description, that a new
uncovered branch is correct.

Without `--write`, the analyser only reports:

```sh
uv run python scripts/error_branch_coverage.py                        # report
uv run python scripts/error_branch_coverage.py --check --coverage coverage.json  # what CI runs
```

## Why this repo has one at all

It did not, until #183. `scripts/error_branch_coverage.py` is template-owned
and reaches a **core instance** through `biffo core upgrade`; it is in neither
`biffo-template`'s `shared-files.json` nor the `@biffo/cli` package that
`scripts/biffo.sh` resolves, so no channel existed by which a satellite like
this one could receive it.

That mattered because `biffo plugin install` vendors this repo whole into
`tabsii-platform` as `services/marketing/`, where the instance's own
`Error-branch coverage` gate **does** run — over `src/` *and* `scripts/*`.
PR #180 added an `except Exception` to `scripts/check_runtime_ceiling.py`,
passed all twelve checks here, and reddened the instance on vendoring:

```
1 error branch(es) added with no test exercising them:
  services/marketing/scripts/check_runtime_ceiling.py:except:except Exception
```

The gate existed one repo downstream of the person who could fix it. It now
runs here, over the same scope (`[tool.coverage.run] source = ["src",
"scripts"]`, matching the instance's `source = ["services", "packages"]` /
`omit = ["*/tests/*"]`).

## Keep the analyser byte-identical

`scripts/error_branch_coverage.py` is a verbatim copy of
`biffo-template/scripts/error_branch_coverage.py`. **Do not edit it here** —
edit it upstream, the same rule `scripts/py-dependency-audit.sh` already
carries. A local edit is drift that nothing polices, and the instance would go
on judging this plugin by the upstream version regardless.

The durable fix is to add it to `biffo-template`'s `shared-files.json` so
`shared-sync.sh` arms every satellite; until that lands, this copy and
`biffo-plugin-idea-scout` / `biffo-plugin-ideation` having none is the actual
state of the estate.

## The wiring is tested

`tests/test_marketing_error_branch_gate.py` asserts that CI really runs the
analyser with `--check`, that the same job writes the `coverage.json` it reads,
that `[tool.coverage.run] source` covers every top-level directory of Python
found on disk, and that this baseline exists — because an absent baseline makes
the analyser report and exit 0, which is the gate present and inert.
</content>
