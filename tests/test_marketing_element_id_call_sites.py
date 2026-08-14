"""Every `marketing_artefact.body` write must stamp element ids first (issue
#145): `json.dumps(<payload>)` under a `"body"` dict key must wrap `<payload>`
in `pipeline.with_element_ids(...)`.

Only ONE call site (`admin_app._advance_artefact`) actually persists a body
carrying real elements — every other "body" write is a pending-state
placeholder (`{"channel_taxonomy": ..., "allowed_source_urls": [...]}`) with
none of `pipeline.ELEMENT_LIST_KEYS` in it, so wrapping it is a no-op. Every
site is wrapped anyway, deliberately: `with_element_ids` is a no-op on a body
that carries no selectable elements, so there is no cost to routing every
write through it, and doing so is what stops a FOURTH write site — one that
DOES carry real content, added by someone following the pattern already on
screen — from forgetting the stamp. This is the same reasoning and the same
shape as `test_marketing_artefact_body_call_sites.py`'s guard for
`_parse_artefact_body`, applied to the write side instead of the read side.

**Why this guard rather than a purely functional test.** A functional test
proves `with_element_ids` itself is correct; nothing about it proves every
call site that constructs an artefact body actually calls it. Issue #145's
own brief says as much: "A guard test asserting every persisted element
carries an id would be worth more than the helper itself." This is that
guard — an AST walk, not a functional test, because a functional test can
only exercise the call sites it was told about, and the point of this file is
to need no such list.

**Why an AST walk rather than a grep.** Same reasoning as
`test_marketing_core_paths_guard.py`'s module docstring: a textual search for
`with_element_ids` matches this file's own docstring, the helper's own
docstring, and every comment naming it. Asking the tree for *a `json.dumps`
call that is the value of a dict's `"body"` key, whose own argument is a call
to `with_element_ids`* cannot be fooled by prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "marketing"

_HELPER = "with_element_ids"


def _is_json_dumps(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "dumps"
        and (isinstance(func.value, ast.Name) and func.value.id == "json")
    )


def _wraps_with_element_ids(call: ast.Call) -> bool:
    """True if `call` (a `json.dumps(...)` call) wraps its sole argument in
    `pipeline.with_element_ids(...)` or a bare `with_element_ids(...)` — the
    two import shapes actually used across this plugin's modules."""
    if len(call.args) != 1:
        return False
    arg = call.args[0]
    if not isinstance(arg, ast.Call):
        return False
    func = arg.func
    if isinstance(func, ast.Attribute):
        return func.attr == _HELPER
    if isinstance(func, ast.Name):
        return func.id == _HELPER
    return False


def _body_json_dumps_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `json.dumps(...)` call that is the value of a `"body"` key in a
    dict literal, anywhere in the module — regardless of how deeply the dict
    itself is nested (a `json={...}` kwarg to `_core`, or a nested payload
    dict built up before being passed in)."""
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "body"
                and isinstance(value, ast.Call)
                and _is_json_dumps(value)
            ):
                hits.append(value)
    return hits


def _unwrapped_body_writes(tree: ast.AST) -> list[int]:
    return sorted(
        call.lineno for call in _body_json_dumps_calls(tree) if not _wraps_with_element_ids(call)
    )


def test_every_persisted_body_write_stamps_element_ids() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno in _unwrapped_body_writes(tree):
            offenders.append(f"{path.name}:{lineno}")

    assert not offenders, (
        "These write marketing_artefact.body without stamping element ids first "
        "(issue #145) — wrap the json.dumps(...) argument in pipeline.with_element_ids(...): "
        + ", ".join(offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """The guard must detect the thing it claims to detect — an unwrapped
    `"body": json.dumps(...)` — and must not fire on the wrapped form, on a
    `json.dumps` under some OTHER key, or on a dict that merely mentions the
    word `body` as a non-string key."""
    tree = ast.parse(
        "a = {'body': json.dumps(payload)}\n"  # offender
        "b = {'body': json.dumps(pipeline.with_element_ids(payload))}\n"  # wrapped, fine
        "c = {'body': json.dumps(with_element_ids(payload))}\n"  # bare import, fine
        "d = {'citations': json.dumps(payload)}\n"  # different key, not this guard's concern
        "e = {'body': other_call(payload)}\n"  # not json.dumps at all
    )
    assert _unwrapped_body_writes(tree) == [1]
