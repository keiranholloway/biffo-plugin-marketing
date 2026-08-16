"""The error-branch ratchet is wired, scans what the instance scans, and does
not itself smuggle unverified error handling downstream (#183).

## What went wrong, and why no gate here noticed

PR #180 added `scripts/check_runtime_ceiling.py`, whose `except Exception`
turns any AWS failure into `CannotReadError`. Twelve checks went green on it.
`biffo plugin install` then vendored this repo into `tabsii-platform` as
`services/marketing/`, and that repo's `Error-branch coverage` gate said:

    1 error branch(es) added with no test exercising them:
      services/marketing/scripts/check_runtime_ceiling.py:except:except Exception

Two independent causes, both closed by the change this module guards:

1. **This repo ran no error-branch gate at all** — not a narrower one, none.
   `scripts/error_branch_coverage.py` is template-owned and reaches an
   *instance* through `biffo core upgrade`. It is in neither
   `biffo-template`'s `shared-files.json` nor the `@biffo/cli` package that
   `scripts/biffo.sh` resolves, so no channel existed by which a satellite
   could have received it. That is biffo-template#1570's class exactly:
   "merged upstream" and "present downstream" are different facts, and nothing
   was keeping an inventory.
2. **The scanned scope was narrower here even in principle.** The analyser
   only inspects files that appear in `coverage.json`. This repo measured
   `source = ["src"]`; the instance measures `source = ["services",
   "packages"]` with `omit = ["*/tests/*", ...]`, i.e. all of
   `services/marketing/` bar its tests. Measured at `1e432ff`:
   `uv run pytest --cov --cov-report=json` wrote 23 files, every one under
   `src/`, and the four modules in `scripts/` were in no report at all.

## Parse, don't grep

`ci.yml` explains all of the above in prose comments that name
`error_branch_coverage.py` several times over. A `grep` for it is satisfied by
those comments whether or not a step runs anything — proving nothing. Same
doctrine, and the same `yaml.safe_load`, as
`tests/test_marketing_ci_coverage_sweep.py` (#115): YAML comments are
discarded during parsing, so only a token in a real field counts.

## Why this module also exercises the analyser's own error branches

`scripts/` is vendored wholesale, so the moment this repo's copy of
`error_branch_coverage.py` lands in the instance it becomes
`services/marketing/scripts/error_branch_coverage.py` and its OWN unexecuted
`except`es are new branches by the instance's reckoning. Vendoring the gate
uncovered would therefore reproduce, in the gate itself, the precise failure
the gate is being adopted to prevent. Measured before these tests existed: 4
such branches (`except ValueError`, `except SyntaxError`, and two
`-> <default>` fallbacks). They are covered here rather than baselined, so the
file travels clean.

The analyser is byte-identical to `biffo-template/scripts/error_branch_coverage.py`
and must stay so — edit it upstream, never here. These tests are written
against its public behaviour (a label, a verdict, a return value), not its line
numbers, so an upstream edit that keeps the behaviour keeps them passing.
"""

from __future__ import annotations

import ast
import os
import tomllib
from pathlib import Path
from typing import Any

import yaml
from _scripts import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_ANALYSER_REL = "scripts/error_branch_coverage.py"

#: Where `scripts/error_branch_coverage.py` looks for the ratchet's recorded
#: position. Hardcoded in that (template-owned, byte-identical) file as
#: `BASELINE_REL`, so it is restated rather than imported: if an upstream edit
#: moves it, this constant is what makes the move visible here instead of the
#: gate quietly starting from "no baseline yet" and asserting nothing.
_BASELINE_REL = "docs/practices/error-branch-baseline.json"

#: Directory names pruned when working out which top-level directories hold
#: Python this repo must measure. Every one is already declared content-free by
#: this repo's `.gitignore` (or, for `.git`, is never content). Listed
#: explicitly rather than matched by a pattern, for the reason
#: `test_marketing_ci_coverage_sweep.py` gives at length: a broad pattern is
#: where a real directory later hides.
_EXCLUDED_DIR_NAMES = frozenset(
    {
        "node_modules",
        "dist",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        ".ruff_cache",
        ".worktrees",
        ".git",
    }
)

#: Top-level directories deliberately outside `[tool.coverage.run] source`,
#: with why. `tests` is the only one, and it is excluded by the INSTANCE too
#: (`omit = ["*/tests/*", ...]`) — so this is the two repos agreeing, not this
#: repo carving itself an exemption.
_NOT_MEASURED = frozenset({"tests"})


def _load_ci() -> dict[str, Any]:
    return yaml.safe_load(_CI_WORKFLOW.read_text())


def _steps_of_every_job(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                steps.append(step)
    return steps


def find_error_branch_step(workflow: dict[str, Any]) -> dict[str, Any] | None:
    """The step that actually runs the analyser, or None.

    Matched on the `run:` script rather than on the step's `name:`, because a
    name is a label anyone can attach to a step that does something else — and
    the failure this whole module is about is a check whose name and behaviour
    had drifted apart.
    """
    for step in _steps_of_every_job(workflow):
        run = step.get("run")
        if isinstance(run, str) and _ANALYSER_REL in run:
            return step
    return None


def top_level_python_dirs(root: Path = _REPO_ROOT) -> set[str]:
    """Top-level directories under `root` containing at least one `.py` file.

    Walked from the filesystem, never a hand-maintained list. The failure mode
    this module exists to stop is a gate green over a set nobody enumerated, so
    the set has to be discovered — a fifth directory of Python added later
    cannot join this repo unmeasured the way `scripts/` did.
    """
    found: set[str] = set()
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in _EXCLUDED_DIR_NAMES:
            continue
        for _dirpath, dirnames, filenames in os.walk(entry):
            dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
            if any(f.endswith(".py") for f in filenames):
                found.add(entry.name)
                break
    return found


# --------------------------------------------------------------------------
# The wiring: CI runs it, over the scope the instance scans.
# --------------------------------------------------------------------------


def test_ci_runs_the_error_branch_analyser_in_check_mode() -> None:
    workflow = _load_ci()
    step = find_error_branch_step(workflow)

    assert step is not None, (
        f"No step in {_CI_WORKFLOW.name} runs {_ANALYSER_REL}. Before #183 that was "
        "this repo's entire error-branch story: PR #180's `except Exception` passed "
        "twelve green checks here and reddened tabsii-platform the moment it was "
        "vendored."
    )
    run = step["run"]
    assert "--check" in run, (
        f"{_ANALYSER_REL} is run without `--check`, so it reports and returns 0. "
        "A ratchet that never fails is a log line, not a gate."
    )


def test_ci_writes_the_json_coverage_report_the_analyser_reads() -> None:
    """`--cov-report=xml` is Codecov's; the analyser reads `coverage.json` only.

    Without the json report the analyser exits 2 with "No coverage data" — the
    gate failing to run, which is a different thing from the gate passing, and
    reads identically in a green job's log if nobody checks the exit code.
    """
    workflow = _load_ci()
    pytest_steps = [
        s
        for s in _steps_of_every_job(workflow)
        if isinstance(s.get("run"), str) and "pytest" in s["run"]
    ]
    assert pytest_steps, "no step in ci.yml runs pytest at all"
    assert any("--cov-report=json" in s["run"] for s in pytest_steps), (
        f"no pytest step writes coverage.json, which is the only input {_ANALYSER_REL} reads."
    )


def test_the_analyser_step_and_the_json_report_are_in_the_same_job() -> None:
    """Artefacts do not travel between jobs on their own.

    Two GitHub Actions jobs get two runners and two empty workspaces, so a
    `coverage.json` written in one is simply absent in the other unless it is
    uploaded and downloaded. Splitting these across jobs would leave the gate
    exiting 2 on every run — the "cannot tell" the estate treats as never a
    pass — so the arrangement is asserted rather than assumed.
    """
    workflow = _load_ci()
    for job_name, job in (workflow.get("jobs") or {}).items():
        runs = [s.get("run", "") for s in job.get("steps") or [] if isinstance(s, dict)]
        if any(_ANALYSER_REL in r for r in runs):
            assert any("--cov-report=json" in r for r in runs), (
                f"job `{job_name}` runs {_ANALYSER_REL} but never writes coverage.json "
                "in that same job."
            )
            return
    raise AssertionError(f"no job runs {_ANALYSER_REL}")


def test_coverage_source_covers_every_directory_of_python_the_instance_measures() -> None:
    """The scope, discovered rather than declared.

    The instance measures all of `services/marketing/` bar its tests. This
    repo has to measure the same set or it cannot gate it — which is exactly
    how `scripts/` (four modules, including the one PR #180 added) stayed
    invisible here while being judged there.
    """
    config = tomllib.loads(_PYPROJECT.read_text())
    source = set(config["tool"]["coverage"]["run"]["source"])
    expected = top_level_python_dirs() - _NOT_MEASURED

    # Printed, not just asserted: the denominator is the point.
    missing = expected - source
    assert not missing, (
        f"directories holding Python that `[tool.coverage.run] source` does not "
        f"measure: {sorted(missing)}. The instance measures them as "
        f"services/marketing/<dir>/ and will gate error branches there that this "
        f"repo cannot see. Discovered set: {sorted(expected)}; configured: "
        f"{sorted(source)}."
    )


def test_this_repo_still_has_no_rls_lane() -> None:
    """Guards the deliberate absence of the instance's `hashFiles` condition.

    The instance's copy of the `Error-branch coverage` step skips itself when
    `.github/workflows/rls-tests.yml` exists, because a repo with a real
    Postgres lane has branches this job structurally cannot execute and its
    combined assertion happens once in `error-branch-coverage-gate.yml`
    instead. This repo replicates neither file. Had it copied the condition
    anyway, adding an RLS lane here would silently switch the gate off with
    nothing taking over — so instead the condition is absent and this test is
    what goes red on the day the assumption stops holding.
    """
    assert not (_REPO_ROOT / ".github" / "workflows" / "rls-tests.yml").exists(), (
        "an RLS lane appeared. ci.yml's `Error-branch coverage` step has no "
        "`hashFiles` guard and will now assert over Python-only coverage, flagging "
        "Postgres-only error branches as unexecuted. Port the instance's "
        "error-branch-coverage-gate.yml before adding this lane — see that file."
    )


def test_the_ratchet_has_a_recorded_baseline() -> None:
    """A baseline the analyser cannot find is a gate that reports and returns 0.

    `load_baseline` distinguishes "never measured" (None) from "measured, found
    nothing" ({}), and on None the analyser deliberately prints and exits 0 so a
    fresh repo is not born red. That is right for a repo that has not adopted
    the ratchet, and wrong for this one — here an absent baseline would mean the
    step runs, prints, and gates nothing.
    """
    baseline = _REPO_ROOT / _BASELINE_REL
    assert baseline.is_file(), (
        f"no baseline at {_BASELINE_REL} — the analyser would report and exit 0, "
        "which is the gate present and inert. Re-take it with:\n"
        "  uv run pytest --cov --cov-report=json\n"
        "  uv run python scripts/error_branch_coverage.py --write"
    )


# --------------------------------------------------------------------------
# Negative controls. A check that can only pass proves nothing.
# --------------------------------------------------------------------------


def test_finder_returns_none_for_a_workflow_that_does_not_run_the_analyser() -> None:
    synthetic = {
        "jobs": {
            "test": {
                "steps": [
                    {"uses": "actions/checkout@v5"},
                    # Names it, in a field, without running it — the exact
                    # shape a grep-based check would wave through.
                    {"name": f"Error-branch coverage ({_ANALYSER_REL})", "run": "true"},
                ]
            }
        }
    }
    assert find_error_branch_step(synthetic) is None


def test_discovery_finds_a_synthetic_directory_of_python(tmp_path: Path) -> None:
    (tmp_path / "widgets").mkdir()
    (tmp_path / "widgets" / "thing.py").write_text("x = 1\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "setup.py").write_text("x = 1\n")
    (tmp_path / "prose").mkdir()
    (tmp_path / "prose" / "notes.md").write_text("hello\n")

    assert top_level_python_dirs(tmp_path) == {"widgets"}


# --------------------------------------------------------------------------
# The analyser's own error branches, so the vendored copy travels clean.
# --------------------------------------------------------------------------


def test_no_baseline_message_names_a_path_outside_this_repo_verbatim() -> None:
    """Covers `no_baseline_message`'s `except ValueError`.

    `Path.relative_to` raises rather than returning a `../..` path, and a
    `--source-root` pointing anywhere outside this repo (which is how the
    instance's `workflow_run` gate invokes it) reaches that raise on every run.
    The handler falls back to the absolute path; naming this repo's baseline
    instead would send the reader to a file that was never consulted.
    """
    analyser = load_script("error_branch_coverage")

    outside = Path("/nowhere/else/entirely") / _BASELINE_REL
    message = analyser.no_baseline_message(outside)
    assert str(outside) in message

    inside = analyser.BASELINE
    assert _BASELINE_REL in analyser.no_baseline_message(inside)


def test_a_bare_except_is_labelled_without_a_type() -> None:
    """Covers `_handler_label`'s `if node.type is None -> 'except:'` fallback.

    `except:` with no type is the broadest possible swallow, so it is the one
    label that must never be missing from a report.
    """
    analyser = load_script("error_branch_coverage")

    tree = ast.parse("try:\n    f()\nexcept:\n    pass\n")
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
    assert analyser._handler_label(handler) == "except:"

    typed = ast.parse("try:\n    f()\nexcept ValueError:\n    pass\n")
    typed_handler = next(n for n in ast.walk(typed) if isinstance(n, ast.ExceptHandler))
    assert analyser._handler_label(typed_handler) == "except ValueError"


def test_an_unparseable_source_file_is_skipped_rather_than_crashing(tmp_path: Path) -> None:
    """Covers `unexecuted`'s `except SyntaxError`.

    Coverage data can name a file this interpreter cannot parse — a module
    written for a newer syntax, a partially-written file in a local tree. The
    gate must skip it, not die: a crashed analyser is a step that exits
    non-zero for a reason unrelated to any error branch, which is how a real
    finding gets dismissed as flakiness.
    """
    analyser = load_script("error_branch_coverage")

    (tmp_path / "broken.py").write_text("def (:\n")
    (tmp_path / "fine.py").write_text("try:\n    f()\nexcept ValueError:\n    pass\n")

    coverage = {
        "files": {
            "broken.py": {"executed_lines": [], "missing_lines": [1]},
            "fine.py": {"executed_lines": [1, 2, 3], "missing_lines": [4]},
            # A file the report names but the tree does not hold, which the
            # `is_file()` guard above the parse must drop silently.
            "gone.py": {"executed_lines": [], "missing_lines": [1]},
        }
    }

    found = analyser.unexecuted(coverage, tmp_path)
    assert [b.path for b in found] == ["fine.py"]


def test_a_missing_baseline_reads_as_never_measured_not_as_empty(tmp_path: Path) -> None:
    """Covers `load_baseline`'s `if not baseline.is_file() -> None`.

    None and `{}` are different states and were conflated once already
    (biffo-template#983): reading "never measured" as "measured, found nothing"
    makes every branch look new and red-lights every repo that adopts the gate.
    """
    analyser = load_script("error_branch_coverage")

    assert analyser.load_baseline(tmp_path / "absent.json") is None

    present = tmp_path / "present.json"
    present.write_text('{"total": 0, "branches": []}\n')
    assert analyser.load_baseline(present) == {"total": 0, "branches": []}
