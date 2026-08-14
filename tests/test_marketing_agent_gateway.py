"""`_CoreAgentGateway` — the seam between Core's raw `AgentRunResponse` JSON
and `pipeline.AgentRunView` (issue #82).

The citation guard's fix is only real if the field it reads actually arrives
here: `pipeline.py`'s `AgentRunView.annotations` is inert unless whatever
builds a view from a Core response carries it across. This is that boundary,
and until this issue it had no dedicated test at all — every existing
route-level test overrides `get_agent_gateway` with a fake `AgentGateway`
(`test_marketing_pipeline_routes.py`), which bypasses `_CoreAgentGateway`
entirely.

A minimal duck-typed fake client is enough: `_CoreAgentGateway` only ever
calls `.get`/`.post` on it (`admin_app.py`'s own comment on `_CoreAgentGateway
.__init__`), so it does not need to be a real `BiffoAPIClient` at runtime —
`_CoreAgentGateway.__init__` is typed against the concrete class regardless
(so callers get its actual `.get`/`.post` signatures), which is why each
construction below carries `# type: ignore[arg-type]` rather than widening
that signature for one test file.
"""

from __future__ import annotations

from typing import Any

import pytest

from marketing import admin_app


class _FakeClient:
    """Records nothing; just returns whatever `Any` response is queued,
    mirroring `BiffoAPIClient.get`'s return type (parsed JSON, untyped)."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        del path, params
        return self._response


_BASE_RUN: dict[str, Any] = {
    "id": "run-1",
    "status": "completed",
    "messages": [{"role": "assistant", "content": "..."}],
}


@pytest.mark.asyncio
async def test_get_agent_run_carries_non_empty_annotations_onto_the_view() -> None:
    """The shape Core actually sends — `AgentRunResponse.annotations`
    (biffo-template#1528/#1530) is `list[dict[str, Any]] | None`, and a
    non-empty entry has exactly the `Annotation.to_dict()` shape
    (`agent_runtime/openrouter.py`): `type`/`url`/`title`, nothing else."""
    run = {
        **_BASE_RUN,
        "annotations": [
            {"type": "url_citation", "url": "https://example.com/a", "title": "A"},
            {"type": "url_citation", "url": "https://example.com/b", "title": "B"},
        ],
    }
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.annotations == run["annotations"]


@pytest.mark.asyncio
async def test_get_agent_run_carries_an_explicit_empty_annotations_list() -> None:
    """`[]` — the run WAS `:online` and retrieval genuinely found nothing.
    Must arrive on the view as `[]`, not be coerced to `None` (which would
    make a proven-empty retrieval read as merely unknown)."""
    run = {**_BASE_RUN, "annotations": []}
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.annotations == []
    assert view.annotations is not None


@pytest.mark.asyncio
async def test_get_agent_run_carries_null_annotations_as_none() -> None:
    """An explicit JSON `null` — Core sends this for a pre-#1528 run or one
    that was never `:online`. Must read as `None` ("not known")."""
    run = {**_BASE_RUN, "annotations": None}
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.annotations is None


@pytest.mark.asyncio
async def test_get_agent_run_defaults_a_missing_annotations_key_to_none() -> None:
    """An older Core build's response predating the `annotations` field
    entirely (the key is simply absent, not `null`) must read identically to
    an explicit `null` — both mean "not known", never "confirmed zero"."""
    run = dict(_BASE_RUN)
    assert "annotations" not in run
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.annotations is None


# ── definition_snapshot: what the run ACTUALLY ran with (issue #160) ──────────
#
# Same boundary, same failure mode as `annotations` above: the drift check in
# `pipeline.synthesis_config_drift` is inert unless the field it reads survives
# the trip from Core's JSON onto the view. It matters more here than usual —
# the synthesis run is fired by the orchestration engine from a *copy* of this
# plugin's run definition frozen into the seeded workflow, so this snapshot is
# the only thing in the whole system that can tell the plugin what that copy
# actually says.


@pytest.mark.asyncio
async def test_get_agent_run_carries_the_definition_snapshot_onto_the_view() -> None:
    """Core's `AgentRunResponse.definition_snapshot` — the configuration the
    runtime read and billed, including the model and wall clock #160 found
    two releases behind."""
    run = {
        **_BASE_RUN,
        "definition_snapshot": {
            "instructions": "…",
            "model": "anthropic/claude-opus-4.8",
            "max_turns": 3,
        },
    }
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.definition_snapshot == run["definition_snapshot"]


@pytest.mark.asyncio
async def test_get_agent_run_defaults_a_missing_definition_snapshot_to_none() -> None:
    """Absent must stay `None` — "not known", never `{}`. An empty dict would
    make `synthesis_config_drift` report every key as drifted, and a check
    that screams on every run is one nobody reads."""
    run = dict(_BASE_RUN)
    assert "definition_snapshot" not in run
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.definition_snapshot is None
