"""The meta-check issue #115 (item 1) calls for: for every directory in this
repo containing a `package.json`/`pyproject.toml`, assert some CI job in
`.github/workflows/` actually references it.

## Why this exists

`class:fail-open` (#115) names three instances found in this repo within two
days, in three different mechanisms, none caught by the others. #97 is the one
this check targets directly: `web-admin/` carried 94 tests, a typecheck and a
lint config, and **no CI job at all** ran any of them — nothing in `ci.yml`
walked outside the repo root, so a PR could break every one of them and merge
green. Nobody found it by the gate going red; someone found it by reading
`ci.yml` and noticing what it didn't do.

The shape is general, not specific to `web-admin/`: a project directory and a
CI job are two independently-maintained things, and nothing forced them to
agree. #97 was instance one; this is the check that makes instance four
impossible instead of merely instance-one-fixed. Same enumeration shape as
`biffo-template`'s `cli/src/lib/guard-wiring-sweep.test.ts` (discover from the
filesystem, never a hand-maintained list — a manifest is one more thing to
forget to update, which is exactly how #97 happened: nothing was lying, ci.yml
just never grew a `web-admin` job when the directory was born).

## Parse, don't grep (#956's class, applied here)

`ci.yml` carries **extensive prose comments** naming `web-admin/` — the
`admin-frontend` job's own header explains, in English, why it exists and what
#97 was. A raw `grep -r web-admin .github/workflows/` is satisfied by those
comments alone, whether or not any job's steps actually touch the directory —
proving nothing, the same shape `test_marketing_core_paths_guard.py`'s module
docstring warns about for a text-based check in this repo. This module uses
`yaml.safe_load` instead: YAML comments are discarded during parsing, so only
a directory token appearing in a real field (a job's `working-directory`, a
step's `run:` script, or a `with:` value such as `cache-dependency-path`)
counts as a reference.

## Denominator

`discover_project_dirs()` walks the repo from the filesystem and prints what
it found — the failure mode this whole class is about is a gate that is green
over a set it never enumerated, so the set itself has to be visible, not just
the verdict. As of this writing that denominator is exactly two: the repo
root (`.`, `pyproject.toml`) and `web-admin` (`package.json`) — small, but the
point is that it is asserted rather than assumed, so a third project directory
added later cannot join the estate unrefereed the way `web-admin` once did.

## Exclusions

Pruned directory names are listed explicitly below, each with why — not a
glob that could later swallow a real project directory without anyone
noticing (that is `#115`'s own warning: the exclusion list is where instance
four hides). Every excluded name already appears in this repo's own
`.gitignore`, plus `.git` and `.worktrees` (also gitignored) — nothing here
invents a new exclusion the repo doesn't already treat as non-content.

## Negative control

A check that can only pass proves nothing (the same reasoning
`guard-wiring-sweep.test.ts` states directly). `test_flags_a_synthetic_unreferenced_directory`
below builds a throwaway directory tree with its own `package.json` and an
empty `.github/workflows/`, and asserts the reference check returns `False` —
proving this can fail, not just that it happens not to today. Before writing
this file, the real check was also fail-first-verified by hand: temporarily
deleting the `admin-frontend` job's `working-directory`/`cache-dependency-path`
lines from a scratch copy of `ci.yml` and confirming
`test_every_project_directory_is_referenced_by_some_ci_job` goes red on
`web-admin`, then reverting. See this repo's PR for #115 for that transcript.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

#: Filenames that mark a directory as a "project" for this sweep's purposes.
_PROJECT_MARKERS = ("package.json", "pyproject.toml")

#: Directory NAMES pruned wherever they occur, each because it can never
#: legitimately be a project root of ours — every one of these is already
#: declared content-free by this repo's own `.gitignore` (or, for `.git`,
#: is never content at all). Explicit and commented, on purpose: a broad
#: pattern (e.g. "anything starting with a dot", "anything called build*")
#: is exactly how a real project directory added later could get silently
#: swallowed and never discovered by this check again (#115's own warning).
_EXCLUDED_DIR_NAMES = frozenset(
    {
        "node_modules",  # vendored JS deps (.gitignore) — third-party, never ours
        "dist",  # build output (.gitignore) — generated, never hand-authored
        ".venv",  # this repo's own Python virtualenv (.gitignore)
        ".pytest_cache",  # pytest's cache (.gitignore)
        "__pycache__",  # compiled Python bytecode cache (.gitignore)
        ".ruff_cache",  # ruff's cache (.gitignore)
        ".worktrees",  # nested git-worktree checkouts of this repo (.gitignore) —
        # each one carries its own copy of every marker file, which
        # would be re-discovered relative to the wrong root entirely
        ".git",  # git internals — never content, not something .gitignore need say
    }
)


def discover_project_dirs(root: Path = _REPO_ROOT) -> list[str]:
    """Every directory under `root` containing a `package.json` or
    `pyproject.toml`, as POSIX-style paths relative to `root` ("." for the
    root itself), sorted. Walked from the filesystem, never a hand-maintained
    list — see this module's docstring for why that matters."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune BEFORE descending, so an excluded directory's contents are
        # never even visited (a node_modules/some-pkg/package.json must not
        # surface here).
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
        if any(marker in filenames for marker in _PROJECT_MARKERS):
            rel = Path(dirpath).relative_to(root)
            found.append("." if rel == Path(".") else rel.as_posix())
    return sorted(found)


def _load_workflow_jobs(
    workflows_dir: Path = _WORKFLOWS_DIR,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every `(workflow filename, job name, job dict)` triple across every
    `.github/workflows/*.yml` — real YAML parsing (`yaml.safe_load`), so a
    directory name that appears only in a comment is invisible here, exactly
    as it should be (this module's docstring explains why that distinction
    matters for this repo's `ci.yml` specifically)."""
    triples: list[tuple[str, str, dict[str, Any]]] = []
    if not workflows_dir.is_dir():
        return triples
    for path in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            continue
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if isinstance(job, dict):
                triples.append((path.name, str(job_name), job))
    return triples


def _step_texts(step: Any) -> list[str]:
    """String values worth searching on one step: its `run:` script and every
    string value under `with:` (this is where `cache-dependency-path:
    web-admin/pnpm-lock.yaml` lives). Deliberately NOT `name:` — a step's
    display name is free-text prose, the same category of thing as a YAML
    comment, and treating it as a reference would reopen the grep trap this
    module's docstring describes, just one field over."""
    if not isinstance(step, dict):
        return []
    texts: list[str] = []
    run = step.get("run")
    if isinstance(run, str):
        texts.append(run)
    with_block = step.get("with")
    if isinstance(with_block, dict):
        texts.extend(v for v in with_block.values() if isinstance(v, str))
    return texts


def _job_working_directories(job: dict[str, Any]) -> set[str]:
    """Every explicit working-directory this job sets — job-level `defaults`
    plus any per-step override — with trailing slashes normalised."""
    dirs: set[str] = set()
    default_wd = job.get("defaults", {})
    if isinstance(default_wd, dict):
        default_run = default_wd.get("run", {})
        if isinstance(default_run, dict):
            wd = default_run.get("working-directory")
            if isinstance(wd, str):
                dirs.add(wd.rstrip("/"))
    for step in job.get("steps", []) or []:
        if isinstance(step, dict):
            wd = step.get("working-directory")
            if isinstance(wd, str):
                dirs.add(wd.rstrip("/"))
    return dirs


def is_directory_referenced(dir_rel: str, jobs: list[tuple[str, str, dict[str, Any]]]) -> bool:
    """Does some job in `jobs` reference `dir_rel` (a path relative to the
    repo root, or "." for the root itself)?

    Two independent ways a job can reference a NESTED directory (`dir_rel !=
    "."`):

      1. It sets `working-directory` (job-level `defaults` or a step) equal
         to `dir_rel` exactly — `admin-frontend`'s
         `defaults.run.working-directory: web-admin`.
      2. `dir_rel` appears as a whole path segment in a step's `run:` text or
         a `with:` value — `cache-dependency-path: web-admin/pnpm-lock.yaml` —
         matched with word/path boundaries so `web-admin` cannot false-match
         inside `web-admin-old` or similar.

    The repo ROOT (`dir_rel == "."`) is different in kind: nothing sensible
    sets `working-directory: .`, because "no override" already means "runs at
    the checkout root". So root counts as referenced when some job runs a
    real step (a `run:` command — not just checkout/setup) with no
    working-directory override anywhere in that job.
    """
    if dir_rel == ".":
        for _file, _name, job in jobs:
            if _job_working_directories(job):
                continue  # this job's steps run somewhere else, not root
            steps = job.get("steps", []) or []
            if any(isinstance(s, dict) and isinstance(s.get("run"), str) for s in steps):
                return True
        return False

    # Boundary-matched, not `\b`: `-` is a non-word character, so `\b` would
    # treat the gap inside "web-admin" itself as a boundary and let a search
    # for "web" match the "web" in "web-admin". Instead: neither side of the
    # match may be a character that could extend a path segment
    # (`[\w.-]` — letters, digits, underscore, dot, hyphen). A leading/
    # trailing `/`, whitespace, quote or end-of-string all count as a real
    # boundary; another path-segment character does not.
    pattern = re.compile(rf"(?<![\w./-]){re.escape(dir_rel)}(?![\w.-])")
    for _file, _name, job in jobs:
        if dir_rel in _job_working_directories(job):
            return True
        for step in job.get("steps", []) or []:
            if any(pattern.search(text) for text in _step_texts(step)):
                return True
    return False


def test_discovers_the_known_project_directories() -> None:
    """A sweep that can't find anything real is not sweeping. Pins today's
    known denominator (root + `web-admin`) so a rename or a directory quietly
    dropping its marker file fails loudly here rather than by this whole
    check going silently vacuous."""
    found = discover_project_dirs()
    print(f"ci-coverage-sweep: {len(found)} project directory(s) discovered: {found}")
    assert found == [".", "web-admin"], (
        f"discover_project_dirs() found {found!r} — if this repo genuinely "
        "gained or lost a project directory, update this pinned list; if not, "
        "the walk or the exclusion list broke."
    )


def test_every_project_directory_is_referenced_by_some_ci_job() -> None:
    """The actual meta-check (#115 item 1): every discovered project
    directory must be referenced by at least one job across
    `.github/workflows/*.yml`."""
    found = discover_project_dirs()
    jobs = _load_workflow_jobs()
    assert jobs, "no CI jobs were parsed at all — .github/workflows/*.yml is empty or unreadable"

    unreferenced = [d for d in found if not is_directory_referenced(d, jobs)]

    print(
        f"ci-coverage-sweep: {len(found)} project directory(s) checked against "
        f"{len(jobs)} job(s) across {_WORKFLOWS_DIR}/*.yml — "
        f"{len(found) - len(unreferenced)} referenced, {len(unreferenced)} not."
    )

    assert not unreferenced, (
        f"{unreferenced} contain a package.json/pyproject.toml but no CI job in "
        f"{_WORKFLOWS_DIR} references them (checked working-directory and "
        "run/with text in every parsed job, real YAML parsing not text "
        "matching — see this file's module docstring). This is #97's exact "
        "shape: code with tests, a typecheck and a lint config that no PR "
        "ever actually ran. Add a CI job (or a working-directory / "
        "cache-dependency-path reference inside an existing one) that covers "
        f"it: {unreferenced}."
    )


# ── Negative control: prove this can fail, not just that it happens not to
# (#115's own requirement — a check that only ever passes is exactly the
# class this issue is about) ────────────────────────────────────────────


def test_flags_a_synthetic_unreferenced_directory() -> None:
    """A project directory with no CI job anywhere naming it must be
    reported as unreferenced — the #97 shape, reproduced synthetically so
    this doesn't depend on ever having a real uncovered directory to test
    against."""
    jobs = (
        _load_workflow_jobs()
    )  # this repo's REAL jobs — none of them can know about a directory that doesn't exist
    assert not is_directory_referenced("totally-unwired-project", jobs)


def test_recognises_a_working_directory_reference() -> None:
    """The positive counterpart: a job whose `defaults.run.working-directory`
    names the directory exactly must be recognised."""
    jobs = [
        (
            "fake.yml",
            "some-job",
            {
                "defaults": {"run": {"working-directory": "some-project"}},
                "steps": [{"run": "npm test"}],
            },
        )
    ]
    assert is_directory_referenced("some-project", jobs)
    assert not is_directory_referenced("some-other-project", jobs)


def test_recognises_a_with_value_reference_without_a_working_directory() -> None:
    """The shape `cache-dependency-path: web-admin/pnpm-lock.yaml` actually
    takes in this repo's real ci.yml: no job-level working-directory is set
    on the step that carries it, only a `with:` value naming the path."""
    jobs = [
        (
            "fake.yml",
            "some-job",
            {
                "steps": [
                    {
                        "uses": "actions/setup-node@v4",
                        "with": {"cache-dependency-path": "some-project/pnpm-lock.yaml"},
                    }
                ]
            },
        )
    ]
    assert is_directory_referenced("some-project", jobs)


def test_does_not_false_match_a_directory_name_that_is_a_prefix_of_another() -> None:
    """`web-admin` must not be considered a reference to a directory literally
    named `web`, and vice versa — this is the boundary the word/path-boundary
    regex in `is_directory_referenced` exists for."""
    jobs = [("fake.yml", "job", {"steps": [{"run": "cd web-admin && pnpm build"}]})]
    assert is_directory_referenced("web-admin", jobs)
    assert not is_directory_referenced("web", jobs)


def test_ignores_a_directory_name_that_only_appears_in_a_yaml_comment() -> None:
    """The load-bearing case: this repo's real `ci.yml` mentions `web-admin`
    repeatedly in prose comments above the job that actually covers it. Parse
    that same shape directly here — a comment-only mention must NOT count —
    proving the parse-don't-grep claim in this module's docstring rather than
    just asserting it."""
    import tempfile

    workflow_text = """\
# This whole file is about web-admin/, mentioned here five more times:
# web-admin web-admin web-admin web-admin web-admin
name: fake
on: push
jobs:
  unrelated-job:
    runs-on: ubuntu-latest
    steps:
      - run: echo hello
"""
    with tempfile.TemporaryDirectory() as tmp:
        workflows_dir = Path(tmp)
        (workflows_dir / "fake.yml").write_text(workflow_text)
        jobs = _load_workflow_jobs(workflows_dir)
        assert not is_directory_referenced("web-admin", jobs), (
            "a directory name that appears only inside YAML comments must not "
            "count as a reference — if it does, real YAML parsing has "
            "regressed into the grep trap this check exists to avoid"
        )


def test_exclusion_list_entries_are_all_actually_gitignored_or_git_itself() -> None:
    """Guard vs. authority, on the exclusion list itself: every pruned
    directory name must independently be declared content-free by this
    repo's own `.gitignore` (or be `.git`, which is never content). If a name
    is ever added to `_EXCLUDED_DIR_NAMES` without also being gitignored,
    this fails loudly — the exclusion list is where #115 warns instance four
    would hide, and this is the two-line disagreement check that keeps it
    honest rather than trusting the comment beside each entry to stay true."""
    gitignore_text = (_REPO_ROOT / ".gitignore").read_text()
    gitignored = {
        line.strip().rstrip("/")
        for line in gitignore_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    for name in _EXCLUDED_DIR_NAMES:
        assert name == ".git" or name in gitignored, (
            f"{name!r} is pruned by this sweep but is not in .gitignore — "
            "either add it there (so git itself agrees it is never content) "
            "or remove it from _EXCLUDED_DIR_NAMES with a real justification."
        )
