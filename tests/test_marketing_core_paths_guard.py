"""Every path this plugin sends to Core must start with ``/api/v1/`` (#17's
class — see the module docstring below for why a narrower check would miss
most of it).

Two real, deployment-breaking bugs have now shipped this shape: `image_routes`
gained `/internal/plugins/me/storage` and `/internal/media-generations`
(missing `/api/v1`) in M6, and `admin_app._core` was built with no `/api/v1`
prefix and no signing at all from M2 onward (#17). A check over *named
constants* would have caught the first — it would NOT have caught the second,
because most of that bug lives in inline string/f-string literals at call
sites (`campaign_client.get(f"/campaigns/{campaign_id}")`,
`admin_app._core("POST", "/artefacts", ...)`), which a constants-only sweep
never looks at. So this walks the AST of every module in `src/marketing/` and
inspects the literal (or statically-resolvable) path argument of every call
that looks like a Core request — `_core(method, path, ...)` and
`<client>.get/post/put/patch/delete(path, ...)` — wherever one is present,
regardless of whether it arrived as a bare string, an f-string, or a
module-level constant referenced by name.

``router.get``/``router.post``/etc. are excluded: those are this plugin's OWN
FastAPI route *registrations* (``@router.post("/campaigns/{id}/links")``),
answering a different question — what this admin app itself serves, mounted
under `/api/v1/plugins/marketing/admin/...` by the host — not a call this
plugin makes outward to Core. Conflating the two would fail every route
handler in the file for the wrong reason.

## The known-pending baseline, and why it exists rather than a plain assert

Twelve of the call sites this walk finds are real, currently-broken instances
of the class — every one blocked on the same thing: Core's internal,
per-plugin CRUD mount (`/api/v1/internal/plugins/marketing/...`) requires
BOTH a SigV4 signature AND the calling admin's own token, forwarded via
`X-Biffo-User-Token` (see `admin_app.py`'s and `image_routes.py`'s module
docstrings, and issue #27, for the full mechanism). The SDK method that does
both together (`SignedCoreClient.raw_request(..., extra_signed_headers=...)`)
exists in `biffo-plugin-sdk`'s source but has never been released — PyPI's
newest version is 1.1.0, which lacks it entirely (keiranholloway/
biffo-template#1480). Fixing these call sites without that capability means
either shipping bearer-only calls that will 403 in a real deployment (exactly
today's bug) or hand-rolling SigV4 signing in this plugin — duplicating SDK
internals that already exist correctly one repo over.

So rather than silently exempting these paths from the guard (which would be
routing around the defect, not fixing it) or failing the suite on a bug
nobody can fix from this repo right now, `_PENDING_SDK_RELEASE` names them
explicitly, keyed by (file, resolved path, how many times it appears). The
test asserts the ACTUAL violations found equal this baseline exactly, in
both directions:

- A path outside this baseline that still fails the `/api/v1/` check is a
  NEW instance of the class — the test fails, same as it would with no
  baseline at all.
- A path in this baseline that no longer fails (because #27 landed a fix) is
  now stale — the test fails until the entry is removed, so a fix can't
  silently widen the exemption for other paths that happen to share its text.

Remove entries from `_PENDING_SDK_RELEASE` as each one is fixed, never add a
new one without a comment explaining why it can't be fixed directly.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "marketing"

#: Attribute-call method names that plausibly send a request to Core.
_CLIENT_METHODS = {"get", "post", "put", "patch", "delete"}

#: Receiver names excluded from `_CLIENT_METHODS` matching: this plugin's own
#: FastAPI routers, whose `.get`/`.post`/... register routes THIS app serves,
#: not calls it makes outward.
_EXCLUDED_RECEIVERS = {"router"}

#: (filename, resolved leading path, occurrence count) — see the module
#: docstring's "known-pending baseline" section. `sorted()` order below is
#: purely so a diff is easy to read; the comparison itself doesn't care.
_PENDING_SDK_RELEASE: dict[tuple[str, str], int] = {
    ("admin_app.py", "/campaigns/"): 2,
    ("admin_app.py", "/artefacts"): 3,
    ("admin_app.py", "/artefacts/"): 3,
    ("admin_app.py", "/links"): 1,
    ("channel_plan_routes.py", "/artefacts"): 1,
    ("image_routes.py", "/campaigns/"): 1,
    ("image_routes.py", "/assets"): 1,
}


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments, so a call site that
    passes a named constant (``_STORAGE_PATH``) can be resolved the same way
    as one that passes a literal inline."""
    consts: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.targets[0].id] = node.value.value
    return consts


def _leading_literal_text(node: ast.expr, consts: dict[str, str]) -> str | None:
    """The longest statically-known literal prefix of a path expression, or
    ``None`` if nothing about it can be determined without running the code.

    Handles a bare string, a named constant, and an f-string whose leading
    segment(s) are literal text and/or a reference to a named constant (e.g.
    ``f"{_STORAGE_PATH}/{media['id']}/url"``) — enough to classify every
    shape actually used in this plugin without needing real type inference.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            elif (
                isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Name)
                and part.value.id in consts
            ):
                out += consts[part.value.id]
            else:
                # An interpolated value we can't resolve statically (e.g. a
                # path parameter) — stop; whatever literal text came before
                # it is all we can vouch for.
                break
        return out
    return None


def _path_argument_index(func: ast.expr) -> int | None:
    """Which positional argument of this call is the request path, or
    ``None`` if the call doesn't look like a Core request at all."""
    if isinstance(func, ast.Attribute) and func.attr in _CLIENT_METHODS:
        if isinstance(func.value, ast.Name) and func.value.id in _EXCLUDED_RECEIVERS:
            return None
        return 0
    if isinstance(func, ast.Name) and func.id == "_core":
        return 1  # _core(method, path, token, **kw)
    if isinstance(func, ast.Attribute) and func.attr == "_core":
        return 1  # admin_app._core(method, path, token, **kw)
    return None


def _find_violations() -> list[tuple[str, str]]:
    """Every (filename, resolved path) sent to Core that does not start with
    ``/api/v1/`` — skipping any call whose path can't be resolved statically
    at all (a genuinely dynamic path is not this guard's concern)."""
    violations: list[tuple[str, str]] = []
    for path in sorted(_SRC.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        consts = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            arg_idx = _path_argument_index(node.func)
            if arg_idx is None or len(node.args) <= arg_idx:
                continue
            text = _leading_literal_text(node.args[arg_idx], consts)
            # Only a value we resolved to something path-shaped counts —
            # `None` (unresolvable) or empty text isn't evidence of anything.
            if not text or not text.startswith("/"):
                continue
            if not text.startswith("/api/v1/"):
                violations.append((path.name, text))
    return violations


def test_every_core_call_path_is_api_v1_except_the_known_pending_ones() -> None:
    found = Counter(_find_violations())
    expected = Counter(_PENDING_SDK_RELEASE)

    new_violations = found - expected
    stale_baseline = expected - found

    assert not new_violations, (
        "New call(s) to Core with a path outside /api/v1/ — every server-side "
        "call to Core must use /api/v1/internal/plugins/<name>/<path> (or "
        "Core's own /api/v1/internal/<path> for a 'me' route), never a bare "
        f"path: {dict(new_violations)}. See this file's module docstring."
    )
    assert not stale_baseline, (
        "_PENDING_SDK_RELEASE lists path(s) that no longer fail the check — "
        "looks like issue #27 fixed one. Remove it from the baseline (do not "
        f"leave a stale entry): {dict(stale_baseline)}."
    )


@pytest.mark.parametrize(
    ("filename", "prefix"),
    [
        ("image_routes.py", "/api/v1/internal/plugins/me/storage"),
        ("image_routes.py", "/api/v1/internal/media-generations"),
        ("admin_app.py", "/api/v1/internal/agent-runs"),
    ],
)
def test_already_fixed_paths_stay_fixed(filename: str, prefix: str) -> None:
    """A narrower, explicit regression check on the three paths this repo has
    already fixed (M6's storage/ledger paths, M3's agent-run gateway) — so a
    reader doesn't have to reconstruct that they're meant to be covered from
    the AST walk's output alone."""
    source = (_SRC / filename).read_text()
    assert prefix in source, f"{filename} no longer contains the fixed path {prefix!r}"
