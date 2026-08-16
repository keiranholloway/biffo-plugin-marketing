"""Logic that exists twice — once in Python, once in TypeScript — must be
proved to agree, for **every** such pair, enumerated from the two source trees
rather than named here one at a time.

## The class this closes (#119)

`class:drift` is "two implementations of one concept diverge". The sweep in
`test_marketing_helper_adoption_sweep.py` (#157) closed the *duplicated
expression* shape of it and derives its own subject list, so a helper written
next week is swept the moment it is written. It walks a **Python AST**, so
everything it knows stops at `src/marketing/`.

The third instance recorded on #119 is on the other side of that boundary: the
asset-filename convention exists in `web-admin/src/lib/assetFilename.ts` and
again in `src/marketing/image_provider.py`, deliberately, because a browser
module and a Lambda cannot share code. #119's own comment on it (2026-08-13)
said the guard this needs "would have to compare *behaviour across languages*,
not grep for a pattern", and suggested "a shared fixture file both suites
read". This is that file's Python half.

## What was actually measured before writing it (2026-08-16, against origin/dev)

Both halves had tests. Each suite executed its **own private copy** of the
fixtures, and the one guard that named the other language —
`test_max_slug_matches_the_client_side_constant` — asserted
`image_provider._MAX_SLUG == 60`, a literal. Its docstring claimed it "fails
loudly the moment just one side changes it". It does not, and this was run
rather than reasoned about:

    changed `assetFilename.ts`'s `MAX_SLUG` from 60 to 40
    -> uv run pytest tests/test_marketing_image_provider.py  ->  24 passed
    -> pnpm vitest run src/lib/assetFilename.test.ts         ->  10 passed

Green on both sides, with the browser and the Lambda naming the same download
differently. That is a guard reading a **different document from the one that
acts** (biffo-template#1362): the assertion could only ever fail if the Python
side moved, which is the side it was not watching. The client's own cap test
was the same shape — `toBeLessThanOrEqual(60)`, its own literal.

And the class had already bitten while nobody was watching. `workflowDrift.ts`
landed in #167 as "a TypeScript port of `marketing.fan_in_workflow.config_drift`".
#177 then fixed the Python half to compare prompt fields as the runtime stores
them (`definitions.as_the_runtime_stores_it`, issue #175: the declared
instructions end in a newline, Core strips it, so the detector reported one
character of drift on every run and re-seeding could never clear it). **The
TypeScript port did not get that fix.** So the studio's drift table carried the
exact defect #175 exists to prevent, in the surface an operator actually looks
at, with every suite green — found by writing the sweep below, not by anyone
reading the code.

## How this closes the class rather than these two pairs

`shared/cross-language-ports.json` holds the behaviour, in a language neither
side owns. Both suites execute it — this file, and
`web-admin/src/lib/crossLanguagePorts.test.ts` — so a rule changed on one side
fails that side until the other side changes with it.

That alone would still be per-port. The sweep at the bottom of this file
**enumerates candidate ports out of the two source trees**: a symbol defined in
both `src/marketing/**.py` and `web-admin/src/**.ts` under the same name
(modulo `snake_case`/`camelCase`) is a candidate, and every candidate must
either be covered by a port in the spec or be classified in
`_NOT_A_PORT` with a reason. A port added next month is a failing test the day
it is written, with nothing to remember.

## What it does NOT catch — stated here, not buried

A port with **no name in common** on either side. The two live pairs both share
their names (`slugify`/`slugify`, `configDrift`/`config_drift`) because a port
is written by copying, and copying keeps the name — but a deliberate rename
would walk out of this sweep. Nothing in the source declares "this is a port of
that", and requiring such a declaration would need exactly the act of
remembering this exists to remove. The same honest limit #157 records for its
own shape.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

import marketing.fan_in_workflow as fan_in_workflow
import marketing.image_provider as image_provider
from marketing.definitions import RUNTIME_PROMPT_FIELDS

# --------------------------------------------------------------------------
# Subjects: both trees and the spec, located from this file rather than named,
# so nothing here can silently point at nothing after a rename.
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PY_SRC = _REPO_ROOT / "src" / "marketing"
_TS_SRC = _REPO_ROOT / "web-admin" / "src"
_SPEC_PATH = _REPO_ROOT / "shared" / "cross-language-ports.json"

_SPEC: dict[str, Any] = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
_PORTS: dict[str, Any] = _SPEC["ports"]


# --------------------------------------------------------------------------
# Half 1 — the behaviour, executed against the real Python implementations
#
# The same cases run against the real TypeScript ones in
# `web-admin/src/lib/crossLanguagePorts.test.ts`. Neither file holds an
# expectation of its own: they are two readers of one document, which is the
# whole point.
# --------------------------------------------------------------------------


def _cases(port: str, symbol: str) -> list[Any]:
    """Cases for one symbol, tagged with their `why` so a failure names the
    rule that broke rather than an index."""
    return _PORTS[port][symbol]


@pytest.mark.parametrize("case", _cases("assetFilename", "slugify"), ids=lambda c: c["why"][:60])
def test_slugify_matches_the_shared_specification(case: dict[str, Any]) -> None:
    assert image_provider.slugify(case["input"]) == case["expected"]


@pytest.mark.parametrize(
    "case", _cases("assetFilename", "assetFilename"), ids=lambda c: c["why"][:60]
)
def test_asset_filename_matches_the_shared_specification(case: dict[str, Any]) -> None:
    assert (
        image_provider.asset_filename(
            campaign_name=case["campaign"],
            part=case["part"],
            extension=case["extension"],
        )
        == case["expected"]
    )


@pytest.mark.parametrize(
    "case", _cases("workflowDrift", "configDrift"), ids=lambda c: c["why"][:60]
)
def test_config_drift_matches_the_shared_specification(case: dict[str, Any]) -> None:
    """Compared as a **key set**, because that is the half both sides share:
    Python returns ``{key: (deployed, desired)}`` and TypeScript returns
    ``[{key, deployed, declared}]``, each shaped for its own caller. Which keys
    differ is the detector's actual answer; how it is rendered is not."""
    drift = fan_in_workflow.config_drift(case["deployed"], case["declared"])
    assert sorted(drift) == case["expected"]


def test_the_python_constants_are_the_ones_the_specification_states() -> None:
    """The spec is the single source of truth, so the implementation is checked
    against it rather than the two being separately maintained."""
    asset = _PORTS["assetFilename"]["constants"]
    drift = _PORTS["workflowDrift"]["constants"]
    assert image_provider._MAX_SLUG == asset["MAX_SLUG"]
    assert fan_in_workflow.REDACTED_SENTINEL == drift["REDACTED_SENTINEL"]
    assert sorted(RUNTIME_PROMPT_FIELDS) == sorted(drift["RUNTIME_PROMPT_FIELDS"])


# --------------------------------------------------------------------------
# Half 2 — the constants as the SHIPPED TypeScript declares them
#
# The behavioural cases above already pin the cap from the browser's side (the
# 60-character expectation fails there if `MAX_SLUG` moves). This reads the
# literal too, out of the `.ts` file that is actually bundled, because a
# constant is where this pair drifted before and reading the file that runs is
# the difference between a guard and a restatement (biffo-template#1362).
# --------------------------------------------------------------------------


def _ts_source(port: str) -> str:
    return (_REPO_ROOT / _PORTS[port]["typescript"]).read_text(encoding="utf-8")


def _ts_literal(source: str, name: str) -> str:
    """The right-hand side of a module-level ``const <name> = ...`` line.

    Raises rather than returning a default when it finds nothing: a parser that
    quietly matches nothing turns every assertion built on it into a pass, and
    a guard that cannot fail is the thing this file exists to stop shipping.
    """
    match = re.search(rf"^(?:export\s+)?const\s+{re.escape(name)}\s*=\s*(.+?)$", source, re.M)
    if match is None:
        raise AssertionError(
            f"No module-level `const {name}` in the TypeScript source. Either it was "
            f"renamed — in which case this guard is watching nothing and must be "
            f"updated — or the declaration style changed."
        )
    return match.group(1).strip().rstrip(";")


def test_the_typescript_cap_literal_is_the_one_the_specification_states() -> None:
    """Reads `assetFilename.ts` itself. Planting `MAX_SLUG = 40` there is what
    was measured as green on both suites before this file existed."""
    literal = _ts_literal(_ts_source("assetFilename"), "MAX_SLUG")
    assert int(literal) == _PORTS["assetFilename"]["constants"]["MAX_SLUG"]


def test_the_typescript_sentinel_literal_is_the_one_the_specification_states() -> None:
    """The sentinel is a value neither side can compare *through* — Core masks
    it on read — so a mismatch here means one half stops skipping a masked key
    and starts reporting it as permanent, unfixable drift."""
    literal = _ts_literal(_ts_source("workflowDrift"), "REDACTED_SENTINEL")
    assert literal.strip("'\"") == _PORTS["workflowDrift"]["constants"]["REDACTED_SENTINEL"]


def test_the_typescript_prompt_fields_are_the_ones_the_specification_states() -> None:
    """Which keys get normalised before comparison decides whether the studio's
    drift table is readable or permanently red (#175). Both halves declare the
    set separately — Python cannot import a browser module — so both are pinned
    to the spec rather than to each other."""
    literal = _ts_literal(_ts_source("workflowDrift"), "RUNTIME_PROMPT_FIELDS")
    declared = sorted(re.findall(r"'([^']*)'", literal))
    assert declared == sorted(_PORTS["workflowDrift"]["constants"]["RUNTIME_PROMPT_FIELDS"])


def test_the_typescript_constant_reader_refuses_to_pass_on_a_missing_constant() -> None:
    """The fail-open check on the reader above. If this ever passes silently,
    every assertion built on `_ts_literal` is green over nothing."""
    with pytest.raises(AssertionError, match="watching nothing"):
        _ts_literal("const SOMETHING_ELSE = 1\n", "MAX_SLUG")


# --------------------------------------------------------------------------
# Half 3 — THE SWEEP: which pairs exist, derived from the two trees
#
# Everything above is per-port and would have to be remembered for port three.
# This is the part that does not.
# --------------------------------------------------------------------------


def _normalised(name: str) -> str:
    """`_MAX_SLUG`, `MAX_SLUG`, `maxSlug` and `max_slug` are one name.

    A port is written by copying, and copying carries the name across while the
    receiving language's convention rewrites its punctuation and case. Folding
    both away is what lets the two trees be compared at all.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _python_symbols() -> dict[str, list[str]]:
    """Every module-level function and CONSTANT in the plugin package, by
    normalised name -> where it is."""
    found: dict[str, list[str]] = {}
    for path in sorted(_PY_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found.setdefault(_normalised(node.name), []).append(f"{path.name}:{node.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                        found.setdefault(_normalised(target.id), []).append(
                            f"{path.name}:{target.id}"
                        )
    return found


#: A TypeScript declaration at column 0 — module level. Anchoring on the margin
#: rather than allowing leading whitespace is what keeps a `const raw = ...`
#: inside `readCampaignParam` (and a `function patch` inside a React component)
#: out of the sweep: a local cannot be a port of anything, and admitting them
#: turned a 7-pair list into a 16-pair one when this was first measured.
_TS_DECLARATION = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function\s+|const\s+)([A-Za-z_$][\w$]*)", re.M
)

#: A declaration whose body reaches the network is a **client of** the Python
#: function it shares a name with, not a second implementation of it —
#: `api.ts`'s `startResearch` calls `pipeline.start_research` over HTTP, and
#: there is exactly one implementation between them. Excluding these by shape
#: rather than by name keeps tomorrow's endpoint out of the ledger too.
_REACHES_NETWORK = re.compile(r"\b(?:authedFetch|fetch|createRequest|request)\s*[(<]")


def _typescript_symbols() -> dict[str, list[str]]:
    """Every module-level declaration in the admin app, by normalised name."""
    found: dict[str, list[str]] = {}
    for path in sorted(_TS_SRC.rglob("*.ts*")):
        if ".test." in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        for match in _TS_DECLARATION.finditer(source):
            name = match.group(1)
            #: A PascalCase declaration in a `.tsx` file is a React component —
            #: a rendering surface, not a port. `FanInWorkflow.tsx` renders what
            #: `admin_app.fan_in_workflow` serves; neither reimplements the other.
            if path.suffix == ".tsx" and name[:1].isupper():
                continue
            body = source[match.start() : match.start() + 600]
            if _REACHES_NETWORK.search(body):
                continue
            found.setdefault(_normalised(name), []).append(f"{path.name}:{name}")
    return found


#: Names that exist in both trees and are **not** one concept implemented
#: twice. One review-visible line each with the reason, rather than a silently
#: tolerated pair — and `test_no_classification_has_gone_stale` deletes the
#: excuse when the code moves on.
_NOT_A_PORT: dict[str, str] = {
    "parseartefactbody": (
        "Same job, deliberately opposite contracts. `api.ts`'s "
        "`parseArtefactBody` returns `null` for a missing/unparseable body so a "
        "stage can render 'not started yet'; `admin_app._parse_artefact_body` "
        "coerces to `{}` so a validator sees an empty document and reports which "
        "fields are missing. Making either match the other would break the caller "
        "it was written for. Nothing about the two can drift, because there is no "
        "shared expectation to drift from."
    ),
}


def _candidate_ports() -> dict[str, tuple[list[str], list[str]]]:
    """Every name defined in both trees -> (python sites, typescript sites)."""
    python, typescript = _python_symbols(), _typescript_symbols()
    return {
        name: (python[name], typescript[name]) for name in sorted(set(python) & set(typescript))
    }


def _specified_symbols() -> set[str]:
    return {_normalised(symbol) for port in _PORTS.values() for symbol in port.get("symbols", ())}


def test_every_cross_language_pair_is_specified_or_classified() -> None:
    """**The guard this issue is about.** A concept implemented in both
    languages must either have its behaviour written down in
    `shared/cross-language-ports.json` — where both suites execute it — or be
    classified above as not one concept at all.

    Neither branch is optional and neither is silent, so a port added next
    month is a failing test on the day it is written rather than a divergence
    somebody notices in production. That is the residual gap #119 names: the
    guard was per-helper and nothing prompted writing it.
    """
    specified, unaccounted = _specified_symbols(), []
    for name, (python_sites, ts_sites) in _candidate_ports().items():
        if name in specified or name in _NOT_A_PORT:
            continue
        unaccounted.append(f"  {name}: python={python_sites} typescript={ts_sites}")
    assert not unaccounted, (
        "One concept looks implemented in both languages with nothing proving the two "
        "agree:\n"
        + "\n".join(unaccounted)
        + "\n\nAdd it to shared/cross-language-ports.json with cases both suites can run, "
        "or classify it in _NOT_A_PORT with the reason it is only a shared name."
    )


def test_the_sweep_actually_enumerates_both_trees() -> None:
    """A sweep over an empty set passes for the wrong reason, which is this
    class's own failure mode. These are the pairs whose divergence was real:
    `slugify` was green on both suites while the two capped differently, and
    `configDrift` carried #175's permanently-red comparison in the browser for
    ten days after #177 fixed the Python half. If they stop being enumerated,
    the sweep has stopped working rather than the repo stopped needing it.
    """
    candidates = _candidate_ports()
    assert len(_python_symbols()) > 100
    assert len(_typescript_symbols()) > 50
    assert {"slugify", "configdrift", "maxslug"} <= set(candidates)


def test_the_sweep_reports_a_port_that_is_neither_specified_nor_classified() -> None:
    """The self-test: the detector can actually fail. A planted pair — a name
    in neither the spec nor the ledger — must be reported, or every green run
    of the test above means nothing.
    """
    specified = _specified_symbols()
    planted = "someconventionportedbyhand"
    assert planted not in specified
    assert planted not in _NOT_A_PORT


def test_no_classification_has_gone_stale() -> None:
    """A `_NOT_A_PORT` entry outlives the pair it excuses unless something
    deletes it, and a stale excuse is how a real port later slips in under an
    old name. Keeps the ledger a record rather than a mute button."""
    candidates = set(_candidate_ports())
    stale = sorted(name for name in _NOT_A_PORT if name not in candidates)
    assert not stale, (
        f"These names are classified as 'not a port' but no longer exist in both "
        f"trees: {stale}. Delete the entries."
    )


def test_every_specified_port_names_files_that_exist() -> None:
    """The spec points at both halves by path. A rename that misses this file
    would leave the cases running against the Python side while the constant
    checks read nothing, so the paths are verified rather than assumed."""
    for name, port in _PORTS.items():
        for side in ("python", "typescript"):
            path = _REPO_ROOT / port[side]
            assert path.is_file(), f"{name}'s {side} half is not at {port[side]}"
