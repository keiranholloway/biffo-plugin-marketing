#!/usr/bin/env python3
"""Which error-handling branches has the test suite never executed? (#956)

A fail-open is a check that passes without checking. Four surfaced on
2026-07-30 alone, none caught by a gate, and every one lived in a branch that
runs only when something has already gone wrong — an `except` that swallows, a
fallback that returns a permissive default. Ordinary line coverage hides them:
the happy path through a function is well covered, so the file looks fine.

This asks the narrower question. For every `except` handler and every
`return`-a-default fallback, did the suite ever run it? An unexecuted error
branch is not automatically a defect, but it is *unverified* — nobody has ever
observed what it does — and today's evidence is that this is precisely where
fail-opens live.

## Why a ratchet rather than a gate

Plenty of error branches are legitimately untested: an `except ImportError`
around an optional dependency, a defensive re-raise. A hard gate at this
precision gets switched off, taking the real findings with it — the same reason
the substring-assertion lint in #957 was scoped down after measurement. So this
records a **baseline** and fails only when the count *grows*: new unverified
error handling has to be a deliberate, visible choice.

## Scope, stated plainly

**Python only.** The three fail-opens found on 2026-07-30 were in three
different languages — shell (`scripts/verify.sh`'s `ci_has`), TypeScript
(branch-protection's 403 skip) and Python (the plugin-host lifespan
misclassification). This catches the Python one. Shell has no practical coverage
story and is not in scope; TypeScript is a possible follow-on via vitest
coverage. Claiming otherwise would be the same shape of error this tool exists
to find.

## The blind spot this alone cannot see, and the fix for it (#637)

`--coverage` used to take exactly one `coverage.json`. In an instance, the
Python job's coverage is all this ever saw — and that job runs with no
Postgres, so every `*_pg.py` test skips there. An error branch reachable only
from a real-Postgres lane (an RLS policy refusing a write, a trigger firing)
therefore read as unexercised no matter how honestly it was tested, and the
workaround was a second, weaker test with a stub session driving the same
clause — duplication carried only because this gate could not see the real one.

`--coverage` is now repeatable and *combines* what it is given: a
line executed in ANY of them counts as executed. This is deliberately the same
mechanism as `coverage combine` (coverage.py's own tool for exactly this), done
here instead so the combine and the analysis are one step and one dependency.
A repo with a second, Postgres-dependent test lane (e.g. an instance's `RLS
Tests` workflow) can pass both artefacts:

    python scripts/error_branch_coverage.py --check \\
        --coverage coverage.json --coverage rls-coverage.json

Passing one path (or none, using the default) behaves exactly as before — this
is additive, not a breaking change to the single-file case.

## Local and CI used to disagree here, and the cause was not what it looked like (#1588)

`--check`'s verdict is entirely a function of the coverage.json(s) you hand it,
so any gap between what a local pytest run executed and what CI's did shows up
here as a disagreement. In *this* repo the actual cause, found and fixed by
#1588, was neither test selection nor environment: `services/api` is async
throughout and reaches the database through SQLAlchemy's async layer, which
runs user code — including exception handlers — inside a **greenlet**
(`greenlet_spawn`), itself running on a **background thread** spun up by
FastAPI's/Starlette's `TestClient` (an `anyio` blocking portal). Coverage does
not trace either a greenlet context or a non-main thread unless told to, and
`[tool.coverage.run]` named neither — so a local run silently under-recorded
24 files of async DB code (60 unexecuted branches locally against CI's
correctly-measured 47, reproduced exactly at commit `0820ca7f`), for no
reason a careful contributor could see by reading test output. `concurrency =
["greenlet", "thread"]` on `[tool.coverage.run]` closes that gap; **both**
values are required — `greenlet` alone still under-counts (verified: 80
unexecuted, worse than no setting at all), because it never extends tracing
into the TestClient's background thread in the first place.

If `--check` still disagrees with CI on a repo carrying that setting, do not
reach for a narrower local pytest invocation, a Postgres service, or the
two-lane combine below as the explanation by default — confirm what actually
differs. This repo in particular has no `rls-tests.yml` and no Postgres
service on its Python job, so neither applies to it; the paragraph below is
real for a repo that has grown a genuine second test lane, not a first port of
call everywhere this script runs.

## The two-lane combine, for a repo that has a real Postgres lane (#637)

Since #637, CI's own verdict for one commit is not even stable across its own
runs on a repo whose CI *does* run a second, Postgres-backed lane (e.g. an
instance's `RLS Tests` workflow) alongside the plain Python job — that is a
different, additive concern from the greenlet/thread gap above, and applies
only where such a lane exists. A local `--check` reports against whatever
coverage.json(s) YOU hand it — for most contributors, one file, from one
pytest invocation, compared against the recorded baseline. A repo's `ci.yml`
does exactly the same comparison against the same baseline, but best-effort
combines the Postgres-only lane's artefact when it can reach one (see that
file's own comments on the timing this depends on) — and combining coverage
can only mark MORE lines executed, never fewer, so the second artefact can
only turn a branch from "new and unexecuted" into "already covered", never
the reverse. That means: a run of CI that catches the artefact in time can go
green on a branch an earlier, artefact-less run of the very same commit
reported as newly unexecuted. **On a repo with such a lane, a clean local
`--check` is therefore not evidence CI will pass, and neither is a red CI run
evidence the next run of the identical commit will also be red** —
re-running once the Postgres-only lane has finished is the remedy for that
shape of red, not a sign the gate is flaky. Pass every coverage.json you have
(see the `--coverage` usage above) and trust the one that has seen the most.

Usage:
    uv run pytest --cov --cov-report=json      # writes coverage.json
    python scripts/error_branch_coverage.py            # report
    python scripts/error_branch_coverage.py --write    # update the baseline
    python scripts/error_branch_coverage.py --check    # fail if it grew
    python scripts/error_branch_coverage.py --check --coverage a.json --coverage b.json
                                                 # combine two lanes' coverage first (#637)
    python scripts/error_branch_coverage.py --check --source-root <tree> --coverage a.json
                                                 # judge a tree other than this
                                                 # script's own repo (#1595)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_REL = Path("docs/practices/error-branch-baseline.json")
BASELINE = REPO_ROOT / BASELINE_REL
COVERAGE_JSON = REPO_ROOT / "coverage.json"


def baseline_for(source_root: Path) -> Path:
    """The baseline belonging to the tree being judged.

    Ordinarily that is this script's own repo and the answer is `BASELINE`,
    unchanged. It differs only for a caller that passed `--source-root` — see
    `unexecuted`'s note for why the source and the baseline must travel
    together rather than being taken from wherever the script happens to sit.
    """
    if source_root == REPO_ROOT:
        return BASELINE
    return source_root / BASELINE_REL


# The two commands that take the first measurement, in the order they must run.
# Named in every message about a missing baseline, because the missing piece is
# never obvious from the failure: the analyser reads `coverage.json`, which only
# exists after a --cov run, and neither the FileNotFoundError nor "N error
# branches added" says so (#983).
BOOTSTRAP_COMMANDS = (
    "uv run pytest --cov --cov-report=json",
    "uv run python scripts/error_branch_coverage.py --write",
)


# Why an absent baseline is a normal state, not a broken repo.
#
# This script is template-owned and reaches every instance through `biffo core
# upgrade`. Its baseline is NOT, and must not be: the file is a measurement of
# the repo it lives in, so shipping the template's copy would assert the
# template's unexecuted branches against an instance's tree — wrong data, naming
# files that do not exist there.
#
# So the test travels and its data cannot, and every instance arrives at this
# gate having never taken the measurement. A ratchet with no prior position
# should start, not block.
def no_baseline_message(baseline: Path) -> str:
    """Written against the baseline actually looked for, not a fixed path.

    A caller that passed `--source-root` is judging a different tree, and
    naming this repo's baseline in the failure would send the reader to a file
    that was never consulted.
    """
    try:
        where: Path | str = baseline.relative_to(REPO_ROOT)
    except ValueError:
        where = baseline

    return (
        f"No error-branch baseline at {where}.\n"
        "\n"
        "That file is a measurement of THIS repo, so it is not distributed by a core\n"
        "upgrade — a fresh instance has simply never taken it (#983). Take it with:\n"
        "\n"
        f"  {BOOTSTRAP_COMMANDS[0]}\n"
        f"  {BOOTSTRAP_COMMANDS[1]}\n"
        "\n"
        "Until then the ratchet has no prior position to compare against, so it\n"
        "reports what it finds and does not fail."
    )


@dataclass(frozen=True)
class Branch:
    """One error-handling branch, identified by where its body starts."""

    path: str
    line: int
    kind: str
    label: str

    def key(self) -> str:
        return f"{self.path}:{self.kind}:{self.label}"


def _handler_label(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "except:"
    try:
        return f"except {ast.unparse(node.type)}"
    except Exception:  # pragma: no cover - unparse is total on real trees
        return "except <?>"


def error_branches(tree: ast.AST, path: str) -> list[Branch]:
    """Every error-handling branch in one module.

    Two shapes, both of which have produced fail-opens in this estate:

    - an `except` handler, whose body runs only when something raised;
    - a bare `return`/`return <literal>` that is the *only* statement of an
      `if`, which is the fallback shape — "if we cannot tell, say yes".
    """
    found: list[Branch] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = node.body[0]
            found.append(Branch(path, body.lineno, "except", _handler_label(node)))
            continue

        if isinstance(node, ast.If) and len(node.body) == 1:
            stmt = node.body[0]
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            # Only constant defaults. A computed return is ordinary control
            # flow; `return True` / `return frozenset()` under a guard is the
            # shape that decides a question by assumption.
            if isinstance(stmt.value, ast.Constant) or (
                isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id in {"set", "frozenset", "list", "dict", "tuple"}
                and not stmt.value.args
            ):
                try:
                    label = f"if {ast.unparse(node.test)[:50]} -> {ast.unparse(stmt.value)}"
                except Exception:  # pragma: no cover
                    label = "if <?> -> <?>"
                found.append(Branch(path, stmt.lineno, "fallback", label))

    return found


def unexecuted(coverage: dict, root: Path) -> list[Branch]:
    """Error branches whose first executed line never ran under the suite.

    `root` MUST be the tree the coverage was measured against. This function
    parses `root / rel` to find branches and then asks whether their line
    numbers appear in that report's `executed_lines` / `missing_lines` — so a
    `root` from a different revision looks the report's line numbers up in the
    wrong file, and a change of even one line above a branch shifts every
    verdict below it.

    That was live in the `workflow_run` gate (#1595), which runs this script
    from the default branch — correctly, so a fork's PR cannot execute its own
    modified analyser — and until `--source-root` existed took the *source*
    from that same checkout too. On tabsii-platform#922 the default branch's
    `admin_app.py` was 35 lines shorter than the commit under test's, and the
    gate reported two covered branches as newly unexecuted at lines that held
    unrelated code. It diverges only on files a commit changes, which is
    exactly the set a gate exists to judge.
    """
    files = coverage.get("files", {})
    out: list[Branch] = []

    for rel, data in sorted(files.items()):
        source = root / rel
        if not source.is_file():
            continue
        try:
            tree = ast.parse(source.read_text())
        except SyntaxError:
            continue

        executed = set(data.get("executed_lines", []))
        # A line coverage.py never considered (a comment, say) is not evidence
        # of anything; only count a branch whose body line is one coverage.py
        # tracked and reported as missing.
        missing = set(data.get("missing_lines", []))

        for branch in error_branches(tree, rel):
            if branch.line in executed:
                continue
            if branch.line in missing:
                out.append(branch)

    return out


def merge_coverage(reports: list[dict]) -> dict:
    """Combine coverage.json reports so a line executed in ANY of them counts.

    Built for #637: a line is "unverified" only if nothing that ran ever
    reached it, so the merge is a per-file UNION of executed_lines — the same
    outcome `coverage combine` gives, computed here instead so pulling in a
    second lane (e.g. a real-Postgres test run) needs no extra tool, just a
    second coverage.json.

    `missing_lines` follows from the merged `executed_lines`, not from a
    separate union: a line coverage.py called "missing" in one report but
    "executed" in another was, in fact, executed — carrying the stale
    "missing" verdict forward would silently re-introduce the exact blind
    spot this function exists to close. A single input is the identity case:
    merging one report must read exactly as if merge_coverage were never
    called, so the single-`--coverage` path (unchanged since #956) still
    behaves the same after this.
    """
    merged_files: dict[str, dict] = {}
    for report in reports:
        for rel, data in report.get("files", {}).items():
            entry = merged_files.setdefault(rel, {"executed": set(), "missing": set()})
            entry["executed"] |= set(data.get("executed_lines", []))
            entry["missing"] |= set(data.get("missing_lines", []))

    return {
        "files": {
            rel: {
                "executed_lines": sorted(entry["executed"]),
                "missing_lines": sorted(entry["missing"] - entry["executed"]),
            }
            for rel, entry in merged_files.items()
        }
    }


def load_baseline(baseline: Path) -> dict | None:
    """The committed baseline, or None when this repo has never taken one.

    None rather than an empty baseline. They are different states and used to be
    conflated: an empty baseline means "measured, and found nothing", which for a
    tree this size means the analyser is broken; a missing one means "never
    measured". Reading the second as the first made every branch look NEW and
    red-lit the gate on every instance that upgraded (#983).
    """
    if not baseline.is_file():
        return None
    return json.loads(baseline.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="record the current set as baseline")
    parser.add_argument("--check", action="store_true", help="fail if the count grew")
    parser.add_argument(
        "--coverage",
        type=Path,
        action="append",
        default=None,
        help=(
            "coverage.json to analyse. Repeatable (#637): pass it more than once "
            "to combine several lanes' coverage (e.g. the Python job's and a "
            "real-Postgres RLS lane's) — a line executed in ANY of them counts "
            "as executed. Defaults to a single coverage.json at the repo root."
        ),
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help=(
            "tree whose source and baseline to judge the coverage against. "
            "Defaults to this script's own repo, which is correct whenever the "
            "coverage was produced from the same checkout. A caller running "
            "this script from one revision against coverage measured on "
            "ANOTHER — the workflow_run gate, which deliberately executes the "
            "default branch's copy of this script — must point it at the "
            "commit under test, or line numbers are looked up in the wrong "
            "revision's AST."
        ),
    )
    args = parser.parse_args()
    coverage_paths: list[Path] = args.coverage if args.coverage else [COVERAGE_JSON]
    source_root: Path = args.source_root.resolve() if args.source_root else REPO_ROOT
    baseline_path = baseline_for(source_root)

    present = [p for p in coverage_paths if p.is_file()]
    if not present:
        paths_str = ", ".join(str(p) for p in coverage_paths)
        print(
            f"No coverage data at {paths_str}.\nRun:  uv run pytest --cov --cov-report=json",
            file=sys.stderr,
        )
        return 2

    absent = [p for p in coverage_paths if p not in present]
    for p in absent:
        # Not fatal: a repo that has only just adopted a second lane, or is
        # invoked before that lane has produced its artefact yet, still gets an
        # answer from what IS present rather than refusing outright — but the
        # gap must be LOUD, not a line to scroll past. #637 was filed on exactly
        # this shape: "a check that skips an input it cannot evaluate is not
        # neutral — it shrinks its own scope and reports the remainder as the
        # whole" (AGENTS.md §2). A clean result over fewer lanes than requested
        # has to be falsifiable, so this is a `::warning::` GitHub Actions
        # annotation — the estate's own convention for exactly this
        # (scripts/py-dependency-audit.sh, scripts/js-dependency-audit.sh) —
        # not a "note:" that only shows up if someone scrolls the raw log.
        # Printed unconditionally, not gated on running-in-CI: it is equally
        # true, and equally worth seeing, from a terminal.
        print(
            f"::warning::error-branch coverage: requested artefact not found: {p} — "
            f"proceeding with {len(present)}/{len(coverage_paths)} coverage "
            "file(s). This result does NOT reflect that lane's coverage.",
            file=sys.stderr,
        )

    reports = [json.loads(p.read_text()) for p in present]
    coverage = merge_coverage(reports)
    file_count = len(coverage.get("files", {}))

    # Restated in every summary line below (not just the warning above) so the
    # denominator survives even if the warning scrolls past unread: a reader
    # who sees only the final line still learns how much of the requested
    # picture this result is actually over. The file count is part of that
    # denominator, not just the artefact count — see the fail-closed check
    # immediately below for why (#1657).
    coverage_note = (
        f"{len(present)}/{len(coverage_paths)} coverage artefact(s), {file_count} file(s)"
    )
    if absent:
        coverage_note += f" — MISSING: {', '.join(str(p) for p in absent)}"

    if file_count == 0:
        # #1657: an artefact can be present and valid JSON while still
        # describing NOTHING — `{"files": {}}`, or `files` absent entirely.
        # That reads identically to "everything is covered" to the logic
        # below (found == [], keys == [], nothing NEW), so --check would
        # exit 0 over a report that examined zero files: a fail-open in the
        # gate that exists specifically to catch fail-opens. Every other
        # soft-open path here (no baseline, a missing --coverage file)
        # announces itself loudly and still proceeds; this one cannot
        # proceed at all, because there is no denominator left to reason
        # over. Fail closed rather than fail open: refuse both --write
        # (never baseline a measurement of nothing) and --check (never let
        # a growth check pass over nothing to check).
        print(
            f"::error::error-branch coverage: {coverage_note} — the merged "
            "coverage report describes 0 files. Refusing to treat that as "
            "clean: a report with nothing in it is not evidence the tree is "
            "covered, it is evidence nothing was measured (a "
            "[tool.coverage.run] source pointed at the wrong tree, a "
            "truncated or hand-staged artefact, a coverage run against an "
            "empty package). Fix the coverage run and try again.",
            file=sys.stderr,
        )
        return 2

    found = unexecuted(coverage, source_root)
    keys = sorted({b.key() for b in found})

    if args.write:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"total": len(keys), "branches": keys}, indent=2) + "\n",
        )
        print(f"baseline written: {len(keys)} unexecuted error branches ({coverage_note})")
        return 0

    baseline = load_baseline(baseline_path)
    if baseline is None:
        # Report, then stop. Loudly, on stderr, naming both commands — a gate
        # that goes quiet without saying so is the fail-open this whole script
        # exists to hunt.
        print(no_baseline_message(baseline_path), file=sys.stderr)
        for branch in sorted(found, key=lambda b: (b.path, b.line)):
            print(f"      {branch.path}:{branch.line}  [{branch.kind}] {branch.label}")
        print(f"unexecuted error branches: {len(keys)}  (no baseline yet; {coverage_note})")
        return 0

    known = set(baseline.get("branches", []))
    new = [k for k in keys if k not in known]

    print(
        f"unexecuted error branches: {len(keys)}  "
        f"(baseline {baseline.get('total', 0)}; {coverage_note})"
    )
    for branch in sorted(found, key=lambda b: (b.path, b.line)):
        marker = "NEW " if branch.key() not in known else "    "
        print(f"  {marker}{branch.path}:{branch.line}  [{branch.kind}] {branch.label}")

    if args.check and new:
        print(
            f"\n{len(new)} error branch(es) added with no test exercising them:",
            file=sys.stderr,
        )
        for k in new:
            print(f"  {k}", file=sys.stderr)
        print(
            "\nAn unexecuted error branch is unverified — nobody has observed what it does.\n"
            "Either cover it, or run --write to accept it deliberately.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
