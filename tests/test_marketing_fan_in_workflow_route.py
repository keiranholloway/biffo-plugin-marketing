"""The admin route that serves the fan-in workflow this build declares (#160).

The point of the route is that the admin UI never authors a copy of the
declaration. So what these tests hold is **identity with the declaration**, not
the declaration's contents — those are asserted once, against
`fan_in_workflow.definition()` itself, in `test_marketing_seed_fan_in_workflow.py`.
Restating them here would create the second copy the route exists to prevent.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from marketing import admin_app as admin_app_module
from marketing.fan_in_workflow import (
    WORKFLOW_NAME,
    config_fingerprint,
    definition,
)


def _client() -> TestClient:
    """The admin app with its `admin`-group gate satisfied.

    The router carries `Depends(require_admin)` for every route, so an
    unauthenticated call 401s before reaching any handler — which is correct,
    and is asserted separately below rather than worked around silently.
    """
    app = admin_app_module.build_app()
    app.dependency_overrides[admin_app_module.require_admin] = lambda: None
    return TestClient(app)


def test_it_serves_exactly_what_the_seed_script_would_write() -> None:
    """The UI's declared half and the script's are the same document.

    This is the whole reason the declaration moved out of `scripts/` into the
    package. If these two could differ, an operator could re-seed from the
    browser and produce a config the CI fingerprint never saw.
    """
    response = _client().get("/fan-in-workflow")

    assert response.status_code == 200
    assert response.json()["definition"] == definition()


def test_it_names_the_workflow_the_script_looks_up_by() -> None:
    """The browser matches Core's deployed list by name, exactly as
    `seed_fan_in_workflow.main` does. A different name here would mean the UI
    reporting 'not seeded' beside a workflow that is seeded, and then creating
    a second one that fires on the same trigger."""
    assert _client().get("/fan-in-workflow").json()["name"] == WORKFLOW_NAME


def test_it_carries_the_fingerprint_of_the_config_it_serves() -> None:
    """Served rather than recomputed in TypeScript: the fingerprint is what an
    operator reads back to say which build is deployed, and a second
    implementation of the hash would eventually disagree with the pinned one."""
    body = _client().get("/fan-in-workflow").json()

    assert body["fingerprint"] == config_fingerprint(body["definition"]["action_config"])


def test_it_is_admin_gated_like_every_other_route_on_this_app() -> None:
    """Not decoration: the response is the input to a control that WRITES a
    workflow definition. Serving it to a non-admin would hand the shape of the
    estate's orchestration to anyone who could reach the mount."""
    response = TestClient(admin_app_module.build_app()).get("/fan-in-workflow")

    assert response.status_code in (401, 403)


def test_it_tells_the_browser_where_cores_api_actually_lives() -> None:
    """The route carries `core_api_url`, and #171 is what happens without it.

    The admin SPA is served from the instance's own domain, whose CloudFront
    routes only `/api/v1/plugins/*` to the API. A browser asking for
    `/api/v1/orchestration/workflows` on that origin therefore reaches the
    public marketing site and gets a **403 with an HTML body** — the same 403
    with a valid admin token, with an access token, and with no `Authorization`
    header at all, which is exactly why it read as a permissions failure.

    Core's real origin is its API Gateway endpoint, which this Lambda already
    holds as `BIFFO_CORE_API_URL` (Terraform passes `module.api_gateway.
    api_endpoint`). Serving it keeps the instance-specific value out of this
    repo, which is the same rule `web-admin`'s own base resolution follows.
    """
    app = admin_app_module.build_app()
    app.dependency_overrides[admin_app_module.require_admin] = lambda: None

    with patch.object(admin_app_module, "CORE_API_URL", "https://core.example.invalid/"):
        body = TestClient(app).get("/fan-in-workflow").json()

    # Trailing slash stripped: the browser appends `/api/v1/orchestration`, and
    # a doubled separator is a 404 nobody would think to look for.
    assert body["core_api_url"] == "https://core.example.invalid"


def test_an_unset_core_api_url_is_reported_as_empty_rather_than_guessed() -> None:
    """Empty is an answer the panel can act on — it says it cannot check.

    The alternative is this route inventing a plausible origin, which would put
    the guess a layer deeper than #171 had it and make the same 403 harder to
    trace, not easier.
    """
    app = admin_app_module.build_app()
    app.dependency_overrides[admin_app_module.require_admin] = lambda: None

    with patch.object(admin_app_module, "CORE_API_URL", ""):
        body = TestClient(app).get("/fan-in-workflow").json()

    assert body["core_api_url"] == ""
