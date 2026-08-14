"""No module may re-derive an expression one of this plugin's own helpers
already holds — for **every** such helper, enumerated from the code itself
rather than named here one at a time.

## The class this closes (#119)

A shared helper is introduced to fix a defect, adopted at some call sites and
not others, and the helper's existence makes everyone believe it is fixed. The
unconverted sites keep the bug, behind a closed issue, looking done. Twice in
this repo: `public_base_url_for` was adopted at **1 call site of 3** (#72 — the
two missed sites 500'd both packs), and the artefact-body parser was filed
against **2** sites when the tree held **7 across 6 files** (#49, swept in
#111 — 3.5x the reported blast radius).

## Why this file exists when two per-helper guards already do

`test_marketing_base_url_call_sites.py` and
`test_marketing_artefact_body_call_sites.py` each name one helper. They are
correct and they stay. But they only catch the helper they name, and **nothing
prompts an author to write the third one** — the guard is per-helper, so a
helper introduced next week is unguarded by construction, and the first anyone
hears of it is the next production defect. That residual gap is what #119 says
must be closed, and a file that names a third literal would not close it.

So this guard **derives its own subject list from the source tree**: it finds
the plugin's consolidation helpers by shape, then asks whether their expression
is re-derived anywhere outside them. A helper written after this file is in the
sweep the moment it is written, with nothing to remember and nothing to update.

## What counts as a consolidation helper

A function whose entire body is ``return <expression>`` (a docstring is
allowed). That is the shape of every helper this class has produced: one
expression, held once, called from many places. The expression becomes a
**template** whose parameters are wildcards; any expression elsewhere in the
package that structurally unifies with it is a call site that bypassed the
helper.

Three shapes are excluded, each for a reason rather than to quieten the output:

- **FastAPI dependency providers** (a parameter defaulting to ``Depends(...)``).
  Each router deliberately declares its own so tests can override it per app —
  `get_campaign_client` is defined identically in four routers on purpose, and
  consolidating them would break `dependency_overrides`.
- **Pure delegations** — a lone call whose every argument is a bare parameter
  (``return await self._load_owned(owner_sub=owner_sub, ...)``). A façade like
  that holds no expression, so every ordinary call to the underlying function
  would be reported as bypassing it.
- **Expressions below `_MIN_TEMPLATE_NODES`.** ``return value or {}`` matches
  half the plugin and means nothing.

## What it deliberately does NOT catch

This finds *duplicated expressions*. The other half of the class — a **call to
a superseded function** the newer helper replaced (#72's shape: `public_base_url`
still called where `public_base_url_for` was needed) — leaves no duplicated
expression behind, and nothing in the source says one function supersedes
another. `test_marketing_base_url_call_sites.py` covers that case by name, and
this file does not generalise it. Anyone reading this as "the drift class is
now impossible" should read that paragraph again.

## Waivers are a ledger, not a mute button

A real duplicate that should *not* be consolidated goes in `_ACCEPTED_DUPLICATES`
with the reason, so it is one review-visible line rather than a silently
tolerated copy — and `test_no_accepted_duplicate_has_gone_stale` deletes the
excuse when the code moves on.
"""

from __future__ import annotations

import ast
from pathlib import Path

# --------------------------------------------------------------------------
# Subject: this plugin's own package, located from the test file rather than
# named, so the sweep cannot silently point at nothing after a rename.
# --------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "marketing"

#: Below this many AST nodes an expression is too common to mean anything.
#: `_parse_artefact_body`'s ternary is 20 nodes; `_pipeline_error_to_http`'s
#: `HTTPException(...)` is 14. Eight admits both with room to spare and still
#: rejects `raw or {}` (4).
_MIN_TEMPLATE_NODES = 8

#: Helpers the sweep MUST find. A collector that quietly stops collecting —
#: after a refactor moves a helper into a class, say — would leave this file
#: green over an empty denominator, which is the failure mode this whole class
#: is about. These are two helpers whose bypassed call sites were real
#: production defects (#49, #27), so if they stop being enumerated the sweep
#: has stopped working, not the repo stopped needing it.
_MUST_ENUMERATE = {"_parse_artefact_body", "_pipeline_error_to_http"}

#: (helper name, file that re-derives it) -> why that copy is correct.
#: Every entry is a duplicate the sweep really found; none is hypothetical.
_ACCEPTED_DUPLICATES: dict[tuple[str, str], str] = {
    ("_pipeline_error_to_http", "image_routes.py"): (
        "Same expression, different failure. `admin_app._pipeline_error_to_http` "
        "is typed `pipeline.PipelineError` and its docstring is about a stage "
        "that ran and produced nothing useful; image_routes maps an "
        "`ImageProviderError` from `create_image_provider`/`generate_still`. "
        "Both are 502s built the same way, and calling the pipeline mapping for "
        "an image-provider failure would pass a value its annotation forbids to "
        "hide a coincidence. Consolidate these only if the two error families "
        "ever merge."
    ),
}


# --------------------------------------------------------------------------
# Finding the helpers
# --------------------------------------------------------------------------


def _parameter_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name bound by the signature — the template's wildcards."""
    args = fn.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _sole_returned_expression(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    """The expression of a body that is exactly ``return <expr>``, or `None`.

    A leading docstring is skipped, because every helper in this plugin has
    one and requiring a bare body would exclude all of them.
    """
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1:
        return None
    only = body[0]
    return only.value if isinstance(only, ast.Return) and only.value is not None else None


def _node_count(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _is_dependency_provider(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for a FastAPI dependency (a parameter defaulting to `Depends(...)`).

    Routers declare these per-module on purpose: `dependency_overrides` is keyed
    by the function object, so a shared one could not be overridden for a single
    app under test.
    """
    defaults = [d for d in [*fn.args.defaults, *fn.args.kw_defaults] if d is not None]
    return any(
        isinstance(d, ast.Call)
        and (getattr(d.func, "id", None) == "Depends" or getattr(d.func, "attr", None) == "Depends")
        for d in defaults
    )


def _is_pure_delegation(expr: ast.expr, params: set[str]) -> bool:
    """True for ``return f(a, b=b)`` — a call forwarding parameters unchanged.

    Such a function is a façade over another function, not a held expression:
    treating it as a template would report every ordinary call to the callee as
    a bypass of the façade, which is the opposite of what this guard means.
    """
    call = expr.value if isinstance(expr, ast.Await) else expr
    if not isinstance(call, ast.Call):
        return False
    arguments = [*call.args, *[kw.value for kw in call.keywords]]
    return all(isinstance(a, ast.Name) and a.id in params for a in arguments)


#: Node types that make an expression a *composition* rather than a lookup.
#: `JoinedStr`/`BinOp` are in the list because URL and key building is this
#: estate's most expensive drift shape — #72 was a base URL — and an f-string
#: helper contains no call at all; leaving them out silently excluded
#: `tracked_url`-shaped helpers from the sweep, which a planted duplicate
#: caught before this file shipped.
_COMPOSING_NODES = (
    ast.Call,
    ast.IfExp,
    ast.BoolOp,
    ast.Compare,
    ast.JoinedStr,
    ast.BinOp,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


def _has_structure(expr: ast.expr) -> bool:
    """True if the expression actually composes something. Pure attribute
    chains, subscripts and literals are shared by too much code to be evidence
    of anything."""
    return any(isinstance(n, _COMPOSING_NODES) for n in ast.walk(expr))


class _Helper:
    """A consolidation helper and the expression it holds."""

    def __init__(
        self, module: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, expr: ast.expr
    ) -> None:
        self.module = module
        self.name = fn.name
        self.lineno = fn.lineno
        self.template = expr
        self.params = _parameter_names(fn)
        #: Node identities of the helper's own definition — the one place its
        #: expression is allowed to appear. Identity, not line numbers, so
        #: moving the function neither breaks the guard nor widens it.
        self.own_nodes = {id(n) for n in ast.walk(fn)}

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.module}:{self.lineno} {self.name}()"


def consolidation_helpers(trees: dict[str, ast.AST]) -> list[_Helper]:
    """Every single-expression helper in the package, discovered from the tree.

    This is the sweep's **denominator**, and the reason the guard does not need
    a hand-maintained list of helper names: a helper added tomorrow lands here
    on its own.
    """
    found: list[_Helper] = []
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            expr = _sole_returned_expression(node)
            if expr is None or _is_dependency_provider(node):
                continue
            if _node_count(expr) < _MIN_TEMPLATE_NODES or not _has_structure(expr):
                continue
            if _is_pure_delegation(expr, _parameter_names(node)):
                continue
            found.append(_Helper(module, node, expr))
    return found


# --------------------------------------------------------------------------
# Matching a call site against a helper's expression
# --------------------------------------------------------------------------

#: Fields that carry no meaning for this comparison. `ctx` is Load/Store/Del,
#: which differs between an expression that is read and one that is assigned to
#: without the expressions themselves differing at all.
_IGNORED_FIELDS = {"ctx"}


def _unifies(
    template: ast.AST, candidate: ast.AST, params: set[str], binding: dict[str, str]
) -> bool:
    """True if `candidate` is `template` with its parameters filled in.

    A parameter is a wildcard that matches any sub-expression, but must match
    the *same* sub-expression everywhere it appears — so
    ``json.loads(raw) if isinstance(raw, str) else raw`` matches a call site
    using one variable throughout and not one that mixes two.
    """
    if isinstance(template, ast.Name) and template.id in params:
        dumped = ast.dump(candidate)
        already = binding.get(template.id)
        if already is None:
            binding[template.id] = dumped
            return True
        return already == dumped

    if type(template) is not type(candidate):
        return False
    if isinstance(template, ast.Name):
        return template.id == candidate.id  # type: ignore[attr-defined]
    if isinstance(template, ast.Constant):
        other = candidate.value  # type: ignore[attr-defined]
        return type(template.value) is type(other) and template.value == other

    candidate_fields = dict(ast.iter_fields(candidate))
    for field, expected in ast.iter_fields(template):
        if field in _IGNORED_FIELDS:
            continue
        actual = candidate_fields.get(field)
        if isinstance(expected, list):
            if not isinstance(actual, list) or len(expected) != len(actual):
                return False
            for want, got in zip(expected, actual, strict=True):
                if isinstance(want, ast.AST):
                    if not isinstance(got, ast.AST) or not _unifies(want, got, params, binding):
                        return False
                elif want != got:
                    return False
        elif isinstance(expected, ast.AST):
            if not isinstance(actual, ast.AST) or not _unifies(expected, actual, params, binding):
                return False
        elif expected != actual:
            return False
    return True


def bypassing_call_sites(trees: dict[str, ast.AST]) -> list[tuple[str, str, int]]:
    """`(helper name, module, line)` for every expression that re-derives a
    helper's body outside that helper."""
    helpers = consolidation_helpers(trees)
    hits: set[tuple[str, str, int]] = set()
    for helper in helpers:
        for module, tree in trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.expr) or id(node) in helper.own_nodes:
                    continue
                if type(node) is not type(helper.template):
                    continue
                if _unifies(helper.template, node, helper.params, {}):
                    hits.add((helper.name, module, node.lineno))
    return sorted(hits)


def _parse_package() -> dict[str, ast.AST]:
    return {p.name: ast.parse(p.read_text(), filename=str(p)) for p in sorted(_SRC.glob("*.py"))}


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


def test_no_module_re_derives_a_shared_helpers_expression() -> None:
    offenders = [
        f"{module}:{line} re-derives {helper}()"
        for helper, module, line in bypassing_call_sites(_parse_package())
        if (helper, module) not in _ACCEPTED_DUPLICATES
    ]
    assert not offenders, (
        "These write out an expression a helper in this plugin already holds, "
        "instead of calling it (#119). A hand-written copy stops tracking the "
        "helper the moment the helper is corrected, and review cannot see the "
        "difference — an unconverted call site looks exactly like code that was "
        "never meant to use the helper. Call the helper, or record why this copy "
        "is correct in `_ACCEPTED_DUPLICATES`:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_actually_enumerates_this_plugins_helpers() -> None:
    """A sweep over an empty denominator passes for the wrong reason.

    The whole point of #119 is that nobody notices a guard covering nothing, so
    the denominator is asserted rather than assumed: it must be non-trivial, and
    it must contain the helpers whose bypassed call sites were real defects.
    """
    helpers = consolidation_helpers(_parse_package())
    names = {h.name for h in helpers}
    assert len(helpers) >= 10, f"suspiciously few helpers enumerated: {sorted(names)}"
    assert _MUST_ENUMERATE <= names, (
        "The collector stopped seeing helpers it must see — the sweep is now "
        f"green over a denominator missing {sorted(_MUST_ENUMERATE - names)}."
    )


def test_the_guard_can_actually_fail() -> None:
    """The detector must detect, on source that is never imported.

    Deliberately *not* the code that motivated this guard: `_parse_artefact_body`
    is already converted everywhere, so proving the guard against it would only
    prove it stays green. This plants a fresh helper and a site that bypasses it.
    """
    tree = ast.parse(
        "def _normalise(raw):\n"
        '    """Hold this once."""\n'
        "    return json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
        "\n"
        "def converted(row):\n"
        "    return _normalise(row.get('body'))\n"
        "\n"
        "def bypassing(row):\n"
        "    raw = row.get('body')\n"
        "    return json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
    )
    assert bypassing_call_sites({"planted.py": tree}) == [("_normalise", "planted.py", 10)]


def test_a_near_miss_is_not_reported() -> None:
    """Different code must not be called drift.

    A guard that fires on anything adjacent gets waived wholesale, so the
    near-misses are pinned: a different test, a different call, and the same
    shape over two different variables are all legitimate code.

    (Each near-miss is written with a second statement so it is a call site
    rather than a helper of its own. A one-line function IS a template, and a
    template whose parameters are wildcards legitimately matches another
    helper's body — that is a duplicated *helper*, which this guard reports on
    purpose. Getting that wrong in the fixture is how this test first failed.)
    """
    tree = ast.parse(
        "def _normalise(raw):\n"
        "    return json.loads(raw) if isinstance(raw, str) else (raw or {})\n"
        "\n"
        "def different_test(row):\n"
        "    raw = row.get('body')\n"
        "    return json.loads(raw) if raw.startswith('{') else (raw or {})\n"
        "\n"
        "def different_call(row):\n"
        "    raw = row.get('body')\n"
        "    return int(raw) if isinstance(raw, str) else (raw or {})\n"
        "\n"
        "def two_variables(row, other):\n"
        "    raw = row.get('body')\n"
        "    return json.loads(raw) if isinstance(other, str) else (other or {})\n"
    )
    assert bypassing_call_sites({"planted.py": tree}) == []


def test_a_dependency_provider_is_not_treated_as_a_helper() -> None:
    """Four routers declare `get_campaign_client` identically on purpose.

    `dependency_overrides` is keyed by the function object, so a shared one
    could not be overridden per app. If this exclusion were dropped, the sweep
    would report twelve offenders whose only correct resolution is a waiver —
    and a guard that starts life with a wall of waivers is one nobody reads.
    """
    tree = ast.parse(
        "def get_campaign_client(admin = Depends(require_admin)):\n"
        "    return principal_client.PrincipalCoreClient(admin.token, verify=True)\n"
        "\n"
        "def other_router_provider(admin = Depends(require_admin)):\n"
        "    return principal_client.PrincipalCoreClient(admin.token, verify=True)\n"
    )
    assert consolidation_helpers({"planted.py": tree}) == []
    assert bypassing_call_sites({"planted.py": tree}) == []


def test_a_pure_delegation_is_not_treated_as_a_helper() -> None:
    """`return await self._load_owned(owner_sub=..., session_id=...)` holds no
    expression — every ordinary call to `_load_owned` would otherwise be
    reported as bypassing the façade in front of it."""
    tree = ast.parse(
        "class S:\n"
        "    async def get_session(self, *, owner_sub, session_id):\n"
        "        return await self._load_owned(owner_sub=owner_sub, session_id=session_id)\n"
        "\n"
        "    async def chat_turn(self, *, owner_sub, session_id):\n"
        "        session = await self._load_owned(owner_sub=owner_sub, session_id=session_id)\n"
        "        return session\n"
    )
    assert consolidation_helpers({"planted.py": tree}) == []


def test_no_accepted_duplicate_has_gone_stale() -> None:
    """A waiver outlives the code it excuses unless something deletes it.

    Each entry must still name a duplicate the sweep really finds. When the
    copy is consolidated or the file is renamed, this fails and the excuse goes
    with it, rather than sitting there granting a permission nobody re-examined.
    """
    live = {(helper, module) for helper, module, _ in bypassing_call_sites(_parse_package())}
    stale = sorted(pair for pair in _ACCEPTED_DUPLICATES if pair not in live)
    assert not stale, (
        "These waivers no longer excuse anything — the duplicate is gone or has "
        f"moved. Delete them: {stale}"
    )
