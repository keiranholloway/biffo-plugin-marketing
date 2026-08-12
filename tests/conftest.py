"""Make `marketing` and its test-local fakes module importable.

This template is a standalone repo (not part of the biffo-template
monorepo's uv workspace — see README's "Standalone repo" note), so
`marketing` is installed normally via the project's own
[build-system]/hatchling config when `uv sync` runs. This conftest only
needs to put `tests/` itself on sys.path so `tests/test_marketing.py`
can import its fakes as a plain sibling module (pytest's rootdir insertion
already covers this in most configurations, but the explicit path insert
keeps this working from any cwd, matching the RBAC reference plugin's
`services/rbac/tests/conftest.py` pattern in the biffo-template monorepo).

The fakes module is named after the plugin (`marketing_fakes.py`, which
scaffolding rewrites per plugin) rather than a bare `fakes.py`. Without
`__init__.py` here, pytest's prepend import mode imports test modules by bare
basename, so two vendored plugins both shipping `fakes.py` would fight over the
top-level module name and one would silently import the other's — which is
exactly what happened when a second plugin was installed (issue #688). Guarded
by `cli/src/lib/plugin-skeleton-second-occupant.test.ts`.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _reenable_disabled_loggers() -> None:
    """Undo `disable_existing_loggers` before each test.

    `logging.config.dictConfig()` defaults `disable_existing_loggers` to
    **True**, which sets ``disabled = True`` on every logger that already
    exists. Something in `tabsii-platform`'s suite calls it, so by the time
    these tests run — vendored into that instance and executed after ~5400
    other tests — this plugin's module-level `Logger(child=True)` objects are
    switched off entirely:

        'service_undefined.marketing.ssm'  propagate=True  disabled=True
        'service_undefined'                propagate=True  disabled=True
        isEnabledFor(WARNING) = False

    So the log line is never emitted, `caplog` sees nothing, and two tests
    asserting on log content fail — in the instance only. They pass in this
    repo because nothing here reconfigures logging.

    Measured, not assumed: three earlier hypotheses (a powertools version
    difference, a specific polluting test, propagation severed at the parent)
    each looked right and were each disproved by testing them. The state above
    is what the failing test actually sees.

    Re-enabling per test is the portable fix: it makes no claim about who
    reconfigured logging or when, and holds wherever this plugin is vendored.
    """
    import logging

    for name in list(logging.root.manager.loggerDict):
        obj = logging.getLogger(name)
        if isinstance(obj, logging.Logger):
            obj.disabled = False


@pytest.fixture(autouse=True)
def _reset_principal_client_cache() -> Iterator[None]:
    """Empty `principal_client`'s shared signed-client cache around every test.

    That cache lives for the life of the process (issue #29 — one
    `SignedCoreClient` per `(base_url, timeout)`, so a Core call no longer
    re-resolves AWS credentials and rebuilds a connection pool each time),
    while several test files monkeypatch `principal_client.SignedCoreClient`
    with a per-test fake. Without this, the first such test's fake is cached
    and every later test in the run keeps calling it long after `monkeypatch`
    has restored the real name — a cross-test leak that would show up as an
    unrelated file's test failing depending on run order.

    Autouse and here rather than in one test file, because the tests that
    populate the cache and the tests that would be poisoned by it are not the
    same tests, and this plugin's suite also runs vendored inside an
    instance's (see `_reenable_disabled_loggers` above for what that context
    does to global state).
    """
    from marketing import principal_client

    principal_client.reset_signed_clients_for_tests()
    yield
    principal_client.reset_signed_clients_for_tests()
