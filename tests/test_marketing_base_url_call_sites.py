"""No module outside `config.py` may call `public_base_url()`.

`config.public_base_url()` reads this deployment's configured base URL. On the
**shared plugin host** — where `admin_ingress` and `user_ingress` apps actually
run — that configuration does not exist: a plugin's Terraform sets environment
on the plugin's OWN Lambda, which is a different function. `config.py`'s own
docstring records this. So `public_base_url()` returns nothing there, forever,
and every caller inside a request path fails.

`public_base_url_for(origin, referer)` exists because of that, deriving the base
URL from the request instead. It was added in #51 and adopted at **one call site
of three**: the mint route got it; `pack_routes._ensure_links` and `user_app`'s
pack route kept the config-only version and returned a 503 on a real deployment
("No public base URL is configured for this deployment") while every test passed.
That is #72, and this guard exists so the next author cannot repeat it.

**Why an AST walk rather than a grep.** A textual search for
``public_base_url()`` matches this file, matches comments and docstrings that
merely name it — including the ones above — and matches ``public_base_url_for``
unless the pattern is written very carefully. A guard that can be satisfied by
its own text is the failure this estate has recorded repeatedly: the check
passes because it cannot really run. Walking the tree asks the only question
that matters — *is there a call node whose callee is this name* — and cannot be
fooled by prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "marketing"

#: The one module allowed to call it: `public_base_url_for` falls back to
#: `public_base_url()` when a request carries no usable Origin/Referer.
_ALLOWED = {"config.py"}

_BANNED = "public_base_url"


def _calls_to(tree: ast.AST, name: str) -> list[int]:
    """Line numbers of every call whose callee is exactly `name`.

    Matches a bare call (`public_base_url()`) and an attribute call
    (`config.public_base_url()`) — both reach the same function, and only one
    of the two forms being caught is how a sweep misses a call site.
    `public_base_url_for` is a different `id`/`attr` and is not matched.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            hits.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            hits.append(node.lineno)
    return hits


def test_no_module_outside_config_calls_public_base_url() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.glob("*.py")):
        if path.name in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno in _calls_to(tree, _BANNED):
            offenders.append(f"{path.name}:{lineno}")

    assert not offenders, (
        "These call `config.public_base_url()`, which returns nothing on the shared "
        "plugin host where these apps actually run — so the route 503s in deployment "
        "while every test passes (#72). Use `public_base_url_for(origin, referer)` and "
        "take a `Request`:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """The guard must detect the thing it claims to detect.

    A guard whose passing condition is never exercised is indistinguishable
    from one that cannot fire. This asserts the detector itself, against source
    that is never imported — both call forms, plus the near-miss that must NOT
    be flagged.
    """
    tree = ast.parse(
        "public_base_url()\n"
        "config.public_base_url()\n"
        "public_base_url_for(a, b)\n"
        "config.public_base_url_for(a, b)\n"
    )
    assert _calls_to(tree, _BANNED) == [1, 2]
