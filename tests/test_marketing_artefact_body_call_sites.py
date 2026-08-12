"""No module may re-derive an artefact body inline; they must call
`admin_app._parse_artefact_body`.

An artefact's ``body`` arrives either as Core's persisted JSON *text* or as a
dict a caller already holds (a pending-state placeholder built and read back in
the same request never gets serialised). Every artefact-reading route therefore
needs the same normalisation, and it was written by hand at each one as
``json.loads(raw) if isinstance(raw, str) else (raw or {})``.

`_parse_artefact_body` exists to hold that once (#49). The issue was filed
against two call sites; the sweep found **seven, across six files**, and the
five nobody had named would have survived a fix made as filed — behind a closed
issue, looking done. That asymmetry is the whole reason for this guard: an
unconverted call site is indistinguishable from code that was never meant to
use the helper, so review cannot see it and only enumeration can.

**Why this guard rather than the one that already existed.**
`test_marketing_base_url_call_sites.py` is the same shape for a different
helper, and it did not prevent #49 — because it names `public_base_url` and
nothing else. Each helper this plugin introduces to consolidate a repeated
expression needs its own entry here. That is the real gap #119 describes, and
the cost of it is one file like this per helper.

**Why an AST walk rather than a grep.** The pattern is a literal, so a textual
search matches this file's own docstring, the helper's docstring, and every
comment naming it — a guard satisfiable by its own text is one that cannot
really run, which this estate has recorded more than once. Asking the tree for
*a conditional expression whose branch parses JSON under an `isinstance(_, str)`
test* cannot be fooled by prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "marketing"

#: The helper's own body is the one place the raw ternary belongs. Scoped to the
#: function, not to `admin_app.py` as a whole — allowing the entire module would
#: let the next inline copy land in the largest file in the plugin unseen.
_HELPER = "_parse_artefact_body"


def _is_json_loads(node: ast.AST) -> bool:
    """True for `json.loads(...)` and for a bare `loads(...)` from an
    ``from json import loads`` import — the same function by either route."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "loads"
    return isinstance(func, ast.Name) and func.id == "loads"


def _is_isinstance_str(node: ast.AST) -> bool:
    """True for `isinstance(x, str)`, the test half of the signature."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "isinstance"):
        return False
    if len(node.args) != 2:
        return False
    second = node.args[1]
    return isinstance(second, ast.Name) and second.id == "str"


def _inline_body_parses(tree: ast.AST) -> list[int]:
    """Line numbers of every `json.loads(x) if isinstance(x, str) else ...`
    outside the helper's own definition.

    The helper's ternary is excluded by node identity rather than by line
    number or file name, so moving the function within its module — or
    defining it somewhere else entirely — neither breaks the guard nor
    silently widens what it permits.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _HELPER:
            exempt.update(id(inner) for inner in ast.walk(node))

    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp) or id(node) in exempt:
            continue
        if _is_json_loads(node.body) and _is_isinstance_str(node.test):
            hits.append(node.lineno)
    return sorted(hits)


def test_no_module_parses_an_artefact_body_inline() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno in _inline_body_parses(tree):
            offenders.append(f"{path.name}:{lineno}")

    assert not offenders, (
        "These re-derive an artefact body inline instead of calling "
        f"`admin_app.{_HELPER}` (#49). A hand-written copy drifts from the "
        "helper the moment the helper is corrected, and the divergence is "
        "invisible to review — every one of these passed its own tests:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """The guard must detect the thing it claims to detect.

    A guard whose failing condition is never exercised is indistinguishable
    from one that cannot fire, so this asserts the detector against source that
    is never imported: both import forms of `json.loads`, and the near-misses
    that must NOT be flagged — the helper call itself, and a conditional that
    parses JSON under a different test.
    """
    tree = ast.parse(
        "a = json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
        "b = loads(raw) if isinstance(raw, str) else raw\n"
        "c = admin_app._parse_artefact_body(raw)\n"
        "d = json.loads(raw) if raw.startswith('{') else {}\n"
        "e = int(raw) if isinstance(raw, str) else raw\n"
    )
    assert _inline_body_parses(tree) == [1, 2]


def test_the_helper_is_exempt_only_inside_its_own_definition() -> None:
    """The exemption must be the function, not the module.

    `admin_app.py` legitimately contains the raw ternary once. If the exemption
    were file-scoped, a second inline copy elsewhere in that module — the
    largest in the plugin — would be permitted, which is the drift this guard
    exists to catch.
    """
    tree = ast.parse(
        f"def {_HELPER}(raw):\n"
        "    return json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
        "\n"
        "def some_route(raw):\n"
        "    return json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
    )
    assert _inline_body_parses(tree) == [5]
