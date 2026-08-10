"""Every path this plugin sends to Core must start with ``/api/v1/`` (#17's
class — see below for why a narrower check would miss most of it).

Three real, deployment-breaking bugs have now shipped this shape: `image_routes`
gained `/internal/plugins/me/storage` and `/internal/media-generations`
(missing `/api/v1`) in M6, `admin_app._core` was built with no `/api/v1`
prefix and no signing at all from M2 onward (#17), and — once #17 was
understood — twelve more call sites turned out to need the *same* prefix
plus a second, harder-to-see credential: Core's internal, per-plugin CRUD
mount authorises on the calling admin's own role even over a SigV4-signed
request, so a signed-but-tokenless call reaches Core and fails silently as a
permission error rather than loudly as a 404 (#27; see
`src/marketing/principal_client.py`'s module docstring for the full
mechanism, and `admin_app._core`'s for how every one of those twelve now
gets both credentials).

A check over *named constants* would have caught the first bug — it would
NOT have caught the second or third, because most of that shape lives in
inline string/f-string literals at call sites
(`campaign_client.get(f"/campaigns/{campaign_id}")`,
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

Only the file that makes a call is asked to prove its path is fixed — a
module-level constant is resolved from `ast.Assign` nodes in the SAME file's
own tree (`_module_string_constants`), never across an import. `admin_app.py`,
`channel_plan_routes.py` and `image_routes.py` each therefore carry their own
literal `/api/v1/internal/plugins/marketing` text (as a local `_INTERNAL_PREFIX`
constant in the first two, inline in the third's one call site) rather than
importing one shared constant — the same reasoning `admin_app.py`'s module
docstring gives for duplicating `_validated_campaign_id` rather than
importing it: what the guard can see has to live at the call site.

## `_PENDING_SDK_RELEASE`: empty, and meant to stay that way

This baseline held the twelve #27 call sites while `biffo-plugin-sdk` 1.2.0 —
the release carrying `SignedCoreClient.raw_request(...,
extra_signed_headers=...)`, the one SDK method that signs a request AND
carries the forwarded user token through that signature — existed in source
but had never reached PyPI (keiranholloway/biffo-template#1480). Landing
that release (confirmed live: PyPI lists 1.0.0, 1.1.0, 1.2.0) and rewriting
every call site to use it (`principal_client.py`) is what emptied it.

The test still asserts the ACTUAL violations found equal this baseline
exactly, in both directions, so an empty baseline is not a weaker check than
a populated one — it is the same check with nothing exempted:

- Any path that fails the `/api/v1/` check is now a violation outright — the
  baseline no longer absorbs the twelve #27 sites.
- If a future defect needs a temporary exemption again, name it explicitly
  here with a comment explaining why it can't be fixed directly, and drain it
  the same way this one was drained — never leave a stale entry once the
  underlying call is fixed.
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
#: docstring's "`_PENDING_SDK_RELEASE`: empty, and meant to stay that way"
#: section. Empty is the asserted invariant, not a placeholder — the test
#: fails just as loudly on a NEW unexempted violation as it did while this
#: held the twelve #27 sites.
_PENDING_SDK_RELEASE: dict[tuple[str, str], int] = {}


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


def test_the_internal_prefix_constant_agrees_across_every_copy() -> None:
    """Guard vs. authority: `_find_violations` above only checks that a path
    starts with `/api/v1/` — it never checks that the three independent
    `_INTERNAL_PREFIX` copies this file's own docstring explains
    (`admin_app.py`, `channel_plan_routes.py`, `image_routes.py`, each
    forced local because the AST walk resolves a named constant only within
    the file that defines it) actually agree with each other or with the
    real mounted path. Verified empirically before this test was written: a
    single-character typo in one copy (`marketting` for `marketing`) sends
    every call in that file to an unmounted path while the rest of this
    suite — including `test_every_core_call_path_is_api_v1_except_the_known_
    pending_ones` above — stays green, because `/api/v1/internal/plugins/
    marketting/...` still starts with `/api/v1/`. This is a two-line
    disagreement check, not a redesign: the guard reads the SHAPE of each
    path; this reads whether the three sources of that shape's prefix still
    say the same thing.
    """
    from marketing import admin_app, channel_plan_routes, image_routes

    canonical = "/api/v1/internal/plugins/marketing"
    assert admin_app._INTERNAL_PREFIX == canonical
    assert channel_plan_routes._INTERNAL_PREFIX == canonical
    assert image_routes._INTERNAL_PREFIX == canonical
