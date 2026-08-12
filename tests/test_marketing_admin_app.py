"""The admin surface (ADR-0021 `admin_ingress`).

The two assertions that matter here are structural rather than behavioural, and
both encode a failure another plugin already shipped:

* **the StaticFiles mount is last** — a mount at "/" swallows every route
  registered after it, so an API route added below it becomes silently
  unreachable while every unit test still passes;
* **the app is a factory** — a module-level mount would make importing this
  module fail wherever the built UI is absent, which is every CI run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from marketing import admin_app
from marketing.definitions import MEDIA_KINDS, PIPELINE_STAGES, PLACEMENTS


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = admin_app.build_app()
    # The host gates on the Cognito group before this app sees a request
    # (ADR-0011). Overridden here so these tests exercise the routes rather than
    # re-testing the SDK's gate.
    _admin = {"sub": "admin", "groups": ["admin"]}
    app.dependency_overrides[admin_app.require_admin] = lambda: _admin
    return TestClient(app)


def test_config_serves_the_vocabulary_the_ui_renders_from(client: TestClient) -> None:
    """Served rather than duplicated in TypeScript.

    A form offering a placement the renderer cannot produce fails at submit, and
    the mismatch is invisible until somebody tries it. One definition, served.
    """
    body = client.get("/config").json()
    assert body["media_kinds"] == list(MEDIA_KINDS)
    assert body["placements"] == list(PLACEMENTS)
    assert body["pipeline_stages"] == list(PIPELINE_STAGES)


def test_the_static_mount_is_last(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A mount at "/" swallows everything registered after it.

    Asserted rather than trusted to a comment, because the symptom is a route
    that 404s in production while every test here still passes — both existing
    plugins record learning this the expensive way.
    """
    dist = tmp_path / "web-admin" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    monkeypatch.setattr(admin_app, "_static_dir", lambda: dist)

    app = admin_app.build_app()
    mounts = [i for i, r in enumerate(app.routes) if isinstance(r, Mount) and r.path == ""]
    assert mounts, "the SPA mount is missing"
    assert mounts[-1] == len(app.routes) - 1, (
        "the StaticFiles mount is not last — every route after it is unreachable"
    )


def test_the_app_builds_without_a_built_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI never has a built SPA. Importing must still work.

    A module-level mount would raise at import time here, which would fail the
    manifest validation job before any test ran.
    """
    monkeypatch.setattr(admin_app, "_static_dir", lambda: None)
    app = admin_app.build_app()
    assert app is not None
    assert not [r for r in app.routes if isinstance(r, Mount) and r.path == ""]


def test_core_calls_fail_loudly_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """503 rather than a connection error against an empty base URL.

    An unset BIFFO_CORE_API_URL is a deployment that was not wired, not a bug in
    the request — and `httpx` against "" produces an error that names neither.
    """
    import asyncio

    from fastapi import HTTPException

    monkeypatch.setattr(admin_app, "CORE_API_URL", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_app._core("GET", "/api/v1/anything", "token"))
    assert exc.value.status_code == 503


def test_the_manifest_app_path_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin_ingress.app` is "marketing.admin_app:app" — assert that resolves.

    The manifest string is validated for SHAPE by Core, never dereferenced at
    install. A typo here mounts nothing and the surface 404s with no error
    anywhere, which is the whole class of failure this repo keeps guarding.
    """
    monkeypatch.setattr(admin_app, "_static_dir", lambda: None)
    module_name, _, attr = "marketing.admin_app:app".partition(":")
    module = __import__(module_name, fromlist=[attr])
    assert getattr(module, attr) is not None


# ── issue #49: one shared artefact-body parser, not six duplicated ternaries ─


def test_parse_artefact_body_parses_a_json_string() -> None:
    """The common case: `body` as Core actually persists it — JSON text."""
    assert admin_app._parse_artefact_body('{"channels": []}') == {"channels": []}


def test_parse_artefact_body_passes_a_dict_through_unchanged() -> None:
    """A caller that already holds the parsed dict (never re-serialised) must
    not be forced through `json.loads` a second time — this is what every
    converted call site relied on the ternary for, not just the string case."""
    body = {"channels": [{"channel_key": "x"}]}
    assert admin_app._parse_artefact_body(body) is body


def test_parse_artefact_body_treats_none_and_an_empty_dict_as_empty() -> None:
    """A pending-state artefact's `body` can be `None` (never written yet) or
    `{}` (a real, empty payload) — both non-string cases read as `{}`
    without ever reaching `json.loads`, matching every ternary this helper
    replaced. An empty *string* is deliberately NOT covered here: it is a
    `str`, so it takes the `json.loads` branch and raises — the identical
    behaviour the duplicated ternary already had, unchanged by this refactor."""
    assert admin_app._parse_artefact_body(None) == {}
    assert admin_app._parse_artefact_body({}) == {}
