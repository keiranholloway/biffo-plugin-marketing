"""The user-ingress group name may be spelled out in exactly ONE place.

Issue #46: ``founder`` is biffo-platform's product vocabulary, not this
plugin's. It was hardcoded in **two** independent places — ``biffo.plugin.json``'s
``user_ingress.required_group`` and ``user_app.py``'s
``require_group("founder")`` — with nothing reconciling them. That is precisely
this repo's #119 class ("adopted at some call sites, not all") waiting to
happen: when keiranholloway/biffo-template#1517 lands and the group becomes an
instance-supplied setting, a hunt across the source is how one of the two gets
missed and the surface keeps gating on a stale literal that still looks right.

So: ``src/marketing/ingress.py`` holds the name, the manifest carries the one
copy the shared plugin host actually reads, ``test_marketing_manifest.py``
reconciles those two, and this file asserts nothing else anywhere spells it
out.

## Why the banned name is DERIVED, not typed here

``_BANNED`` is built from ``ingress.USER_INGRESS_GROUP`` rather than written as
a literal. Two reasons, and the second is the one that matters:

1. This guard file cannot then violate its own rule and need an exemption for
   itself — the estate's recurring shape is a check that passes because it was
   quietly excused.
2. It asks the right question. The rule is not "never write ``founder``"; it is
   "the group this plugin gates on is named in one place". Change the group and
   the guard follows it, with no second edit and no window where the guard is
   checking yesterday's value.

## ``admin`` is deliberately NOT banned

``require_group("admin")`` appears as a bare literal in ``admin_app.py`` and
``image_routes.py``, and ``biffo.plugin.json`` declares
``admin_ingress.required_group: "admin"`` and ``required_role: ["admin"]`` on
every table's writes. **None of that is a violation and this guard must never
be extended to cover it.**

``admin`` is a universal Biffo role — every platform has one by construction,
which is what all those table permissions already assume. ``founder`` exists on
biffo-platform and nowhere else. Only one of the two is a name this plugin
cannot know, so only one is instance-supplied. Banning ``admin`` here would
push somebody to declare it in the manifest's ``config`` block, minting an
instance setting every instance must set to the identical value — and an unset
required setting fails the install (#1517 §4). Configuration nobody varies is
configuration nobody sets correctly.

## Enumerated, not per-case

Both checks collect **every** violation and compare the whole set against an
empty baseline, in both directions. A third hardcoded site added later fails
without anybody remembering to write an assertion for it — which is the only
version of this guard worth having, and is the property that was verified by
adding a third site, watching this file go red, and removing it again (see the
PR for #46 for the captured output).

## String VALUES, not substrings

The check compares a string constant's whole stripped value against the banned
name. Prose that merely discusses the group — this docstring, ``ingress.py``'s,
``user_app.py``'s, the manifest's ``marketing_channel`` description — is never
an exact match, so the guard cannot be satisfied or tripped by documentation.
A substring grep would flag every one of them and would then have to be
weakened with exemptions until it checked nothing, which is how the
``public_base_url`` guard's docstring
(``test_marketing_base_url_call_sites.py``) explains its own AST walk.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from marketing import ingress

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "marketing"
_TESTS = Path(__file__).resolve().parent
_MANIFEST = _ROOT / "biffo.plugin.json"

#: Derived, never typed — see the module docstring. A set so a future second
#: instance-supplied group name (a per-unit gate, say) joins it without
#: reshaping the walk.
_BANNED = {ingress.USER_INGRESS_GROUP}

#: The one module allowed to spell the name out. Everything else must reach it
#: through `ingress.user_ingress_group()`.
_HOME = "ingress.py"

#: The one path in the manifest allowed to carry the value. The shared plugin
#: host reads the gate from here, so this copy cannot simply be deleted today;
#: `test_marketing_manifest.py` asserts it equals `ingress.USER_INGRESS_GROUP`
#: so the two cannot drift, and #1517 is what removes the need for it.
_MANIFEST_HOME = "user_ingress.required_group"


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string constant in *tree*, with its line number.

    Docstrings are included rather than skipped: they are exact-matched like
    any other constant, and a docstring whose entire content is the bare group
    name is not a docstring, it is a literal hiding in one.
    """
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _violations_in(directory: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        if path.name == _HOME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, value in _string_constants(tree):
            if value.strip() in _BANNED:
                rel = path.relative_to(_ROOT)
                found.append(f"{rel}:{lineno}: {value.strip()!r}")
    return found


def test_the_group_name_is_spelled_out_in_exactly_one_module() -> None:
    """`src/marketing/` names the group only in `ingress.py`.

    Enumerated: the full list of offending sites is compared against an empty
    baseline, so a newly-added third site fails here with its own file and line
    rather than needing a new assertion written for it.
    """
    assert _violations_in(_SRC) == [], (
        f"The user-ingress group name is instance vocabulary (#46) and must appear "
        f"only in src/marketing/{_HOME}. Reach it through "
        f"`ingress.user_ingress_group()` instead. Offending sites:\n  "
        + "\n  ".join(_violations_in(_SRC))
    )


def test_the_tests_do_not_reintroduce_the_literal_either() -> None:
    """Fixtures count. A test asserting `groups: ["founder"]` is a second copy
    of the decision, and one that would keep passing against a stale gate after
    #1517 moves the real value — a guard that only covers `src/` leaves the
    fixture free to disagree with the code it is testing."""
    assert _violations_in(_TESTS) == [], (
        "A test names the user-ingress group directly. Use "
        "`ingress.USER_INGRESS_GROUP` so the fixture follows the gate. "
        "Offending sites:\n  " + "\n  ".join(_violations_in(_TESTS))
    )


def _manifest_paths_carrying(value: str) -> list[str]:
    """Every dotted path in the manifest whose string value is exactly *value*."""
    hits: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, str):
            if node.strip() == value:
                hits.append(path)
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(json.loads(_MANIFEST.read_text(encoding="utf-8")), "")
    return sorted(hits)


def test_the_manifest_carries_the_group_at_exactly_one_path() -> None:
    """`user_ingress.required_group` and nowhere else.

    The manifest is data the host reads, so this copy is load-bearing today —
    but a second one (a chat agent's `required_group`, a table permission, a
    route description quoting it as a value) would be an unreconciled third
    site. #1517's whole point is that a manifest names no platform's
    vocabulary; until it lands, the count is one.
    """
    for banned in sorted(_BANNED):
        assert _manifest_paths_carrying(banned) == [_MANIFEST_HOME], (
            f"biffo.plugin.json carries {banned!r} at "
            f"{_manifest_paths_carrying(banned)}; the only permitted path is "
            f"{_MANIFEST_HOME}."
        )


def test_admin_is_deliberately_not_banned() -> None:
    """Guard the guard's own exception, so it is not quietly widened.

    `admin` is a universal Biffo role and stays a bare literal — see this
    module's docstring. If somebody adds it to `_BANNED`, this fails and points
    at the reasoning rather than letting the change look like tidying.
    """
    assert "admin" not in _BANNED, (
        "`admin` is a universal Biffo role, not instance vocabulary. Banning it "
        "would push it into the manifest's `config` block as a required setting "
        "every instance must set to the same value, and an unset required "
        "setting fails the install. See this module's docstring and "
        "`admin_app.require_admin`."
    )
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["admin_ingress"]["required_group"] == "admin", (
        "admin_ingress must keep gating on the literal `admin` (#46's settled "
        "judgement call), not on anything instance-supplied."
    )


def test_the_guard_can_actually_see_the_source_it_polices() -> None:
    """Guard the guard: an empty walk makes every assertion above vacuous.

    This estate's dominant defect is a check that passes because it could not
    run. A moved package or a renamed manifest would make `_violations_in`
    return `[]` for the best possible reason and the worst possible cause.
    """
    assert _MANIFEST.is_file(), f"No manifest at {_MANIFEST}"
    modules = list(_SRC.rglob("*.py"))
    assert len(modules) > 5, f"Only {len(modules)} module(s) under {_SRC}"
    assert (_SRC / _HOME).is_file(), f"The one permitted home, {_HOME}, is missing"
    assert any(
        value.strip() in _BANNED
        for _, value in _string_constants(ast.parse((_SRC / _HOME).read_text(encoding="utf-8")))
    ), f"{_HOME} does not contain the name this guard is enumerating copies of"
