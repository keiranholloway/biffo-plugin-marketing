"""The research/positioning admin routes (M3), over fake Core and a fake
agent gateway.

Fakes rather than mocks, matching this repo's `test_marketing_mint_route.py`
convention: what is worth asserting is what got written to `marketing_artefact`
and what run was requested, and a fake that records both says that directly.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marketing import admin_app, pipeline
from marketing.definitions import RESEARCH_SYNTHESIS_AGENT_NAME

_CAMPAIGN = "b3f1c0de-0000-4000-8000-000000000002"


class _FakeCore:
    """An in-memory `marketing_campaign` + `marketing_artefact` store, reached
    the same way `admin_app._core` reaches the real one."""

    def __init__(self, *, brief: str | None = "Reach multi-location operators.") -> None:
        self.campaign = {"id": _CAMPAIGN, "brief": brief}
        self.artefacts: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        if method == "GET" and path == f"/campaigns/{_CAMPAIGN}":
            return httpx.Response(200, json=self.campaign, request=request)
        if method == "GET" and path.startswith("/campaigns/"):
            return httpx.Response(404, json={"detail": "not found"}, request=request)
        if method == "GET" and path == "/artefacts":
            params = kw.get("params") or {}
            rows = [
                a
                for a in self.artefacts.values()
                if a.get("campaign_id") == params.get("campaign_id")
                and a.get("kind") == params.get("kind")
            ]
            return httpx.Response(200, json=rows, request=request)
        if method == "POST" and path == "/artefacts":
            self._next_id += 1
            artefact_id = f"artefact-{self._next_id}"
            body = dict(kw["json"])
            body.setdefault("body", None)
            body.setdefault("citations", None)
            body.setdefault("agent_run_id", None)
            row = {
                "id": artefact_id,
                "created_at": f"2026-08-10T00:00:{self._next_id:02d}Z",
                **body,
            }
            self.artefacts[artefact_id] = row
            return httpx.Response(201, json=row, request=request)
        if method == "PATCH" and path.startswith("/artefacts/"):
            artefact_id = path.removeprefix("/artefacts/")
            self.artefacts[artefact_id].update(kw["json"])
            return httpx.Response(200, json=self.artefacts[artefact_id], request=request)
        raise AssertionError(f"unexpected call {method} {path}")


class _FakeGateway:
    """Records requested runs; a test drives them to completion directly,
    standing in for the runtime and the orchestration engine."""

    def __init__(self) -> None:
        self.requested: list[dict[str, Any]] = []
        self._runs: dict[str, pipeline.AgentRunView] = {}
        self._chain_runs: dict[tuple[str, str], str] = {}
        self._next_id = 0

    async def request_agent_run(
        self, *, agent_name: str, definition, output_tool, input_payload, causation_id
    ) -> str:
        del definition, output_tool
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self.requested.append(
            {"agent_name": agent_name, "causation_id": causation_id, "input_payload": input_payload}
        )
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="pending", messages=[])
        return run_id

    async def find_chain_run(self, *, chain_id: str, agent_name: str):
        run_id = self._chain_runs.get((chain_id, agent_name))
        return None if run_id is None else self._runs[run_id]

    async def get_agent_run(self, *, run_id: str):
        return self._runs.get(run_id)

    def complete(
        self, run_id: str, *, status: str = "completed", messages: list | None = None
    ) -> None:
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or []
        )

    def fire_chain_run(self, *, chain_id: str, agent_name: str, messages: list) -> None:
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="completed", messages=messages)
        self._chain_runs[(chain_id, agent_name)] = run_id


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch):
    core = _FakeCore()
    gateway = _FakeGateway()
    monkeypatch.setattr(admin_app, "_core", core)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = _admin_user
    app.dependency_overrides[admin_app.get_agent_gateway] = lambda: gateway
    return TestClient(app), core, gateway


def _synthesis_call(url: str = "https://example.com/thread") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_research_synthesis",
                        "arguments": {
                            "summary": "One clear signal.",
                            "findings": [
                                {
                                    "signal": "s",
                                    "why_it_matters": "m",
                                    "sources": [{"url": url, "note": "n"}],
                                }
                            ],
                        },
                    }
                }
            ],
        }
    ]


def _empty_synthesis_call() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_research_synthesis",
                        "arguments": {"summary": "Nothing found.", "findings": []},
                    }
                }
            ],
        }
    ]


def _positioning_call(url: str = "https://example.com/thread") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_positioning",
                        "arguments": {
                            "segments": [
                                {
                                    "name": "Segment",
                                    "description": "d",
                                    "sources": [{"url": url, "note": "n"}],
                                }
                            ],
                            "pillars": [],
                            "ctas": [],
                        },
                    }
                }
            ],
        }
    ]


def _channel_plan_call(url: str = "https://example.com/thread") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_channel_plan",
                        "arguments": {
                            "channels": [
                                {
                                    "channel": "Instagram Reels",
                                    "motion": "organic",
                                    "rank": 1,
                                    "rationale": "r",
                                    "sources": [{"url": url, "note": "n"}],
                                },
                                {
                                    "channel": "Google Search ads",
                                    "motion": "paid",
                                    "rank": 1,
                                    "rationale": "r",
                                    "sources": [{"url": url, "note": "n"}],
                                },
                            ]
                        },
                    }
                }
            ],
        }
    ]


def _empty_channel_plan_call() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "submit_channel_plan", "arguments": {"channels": []}}}
            ],
        }
    ]


# ── start research ────────────────────────────────────────────────────────────


def test_start_research_fans_out_and_records_a_pending_artefact(ctx) -> None:
    client, core, gateway = ctx

    resp = client.post(f"/campaigns/{_CAMPAIGN}/research")

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "research"
    assert body["status"] == "pending"
    assert body["campaign_id"] == _CAMPAIGN
    assert len(gateway.requested) == 2
    assert all(r["causation_id"] == body["causation_id"] for r in gateway.requested)


def test_start_research_refuses_a_campaign_with_no_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(brief=None)
    gateway = _FakeGateway()
    monkeypatch.setattr(admin_app, "_core", core)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = _admin_user
    app.dependency_overrides[admin_app.get_agent_gateway] = lambda: gateway

    resp = TestClient(app).post(f"/campaigns/{_CAMPAIGN}/research")

    assert resp.status_code == 422
    assert gateway.requested == []


def test_start_research_404s_an_unknown_campaign(ctx) -> None:
    client, _core, _gateway = ctx
    other = str(uuid.uuid4())

    resp = client.post(f"/campaigns/{other}/research")

    assert resp.status_code == 404


# ── read/advance ───────────────────────────────────────────────────────────────


def test_get_artefact_404s_an_unknown_kind(ctx) -> None:
    client, _core, _gateway = ctx
    assert client.get(f"/campaigns/{_CAMPAIGN}/artefacts/not_a_real_kind").status_code == 404


def test_get_artefact_404s_when_none_exists_yet(ctx) -> None:
    client, _core, _gateway = ctx
    assert client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research").status_code == 404


def test_get_artefact_stays_pending_while_research_is_still_running(ctx) -> None:
    client, _core, gateway = ctx
    started = client.post(f"/campaigns/{_CAMPAIGN}/research").json()

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research")

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["id"] == started["id"]


def test_get_artefact_proposes_once_the_engine_fires_synthesis(ctx) -> None:
    client, _core, gateway = ctx
    started = client.post(f"/campaigns/{_CAMPAIGN}/research").json()
    for run_id in list(gateway._runs):
        gateway.complete(run_id)
    gateway.fire_chain_run(
        chain_id=started["causation_id"],
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_synthesis_call(),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    stored_body = json.loads(body["body"])
    assert stored_body["findings"][0]["sources"][0]["url"] == "https://example.com/thread"
    citations = json.loads(body["citations"])
    assert citations == [{"url": "https://example.com/thread", "note": "n"}]


def test_get_artefact_502s_a_zero_citation_run_and_leaves_it_pending(ctx) -> None:
    """The milestone's guard at the HTTP boundary: a run that cited nothing
    must not be proposed to an operator as if it were evidenced."""
    client, core, gateway = ctx
    started = client.post(f"/campaigns/{_CAMPAIGN}/research").json()
    for run_id in list(gateway._runs):
        gateway.complete(run_id)
    gateway.fire_chain_run(
        chain_id=started["causation_id"],
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_empty_synthesis_call(),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research")

    assert resp.status_code == 502
    assert core.artefacts[started["id"]]["status"] == "pending", "must not have been proposed"


# ── approve / reject ────────────────────────────────────────────────────────────


def _propose_research(client, core, gateway) -> dict[str, Any]:
    started = client.post(f"/campaigns/{_CAMPAIGN}/research").json()
    for run_id in list(gateway._runs):
        gateway.complete(run_id)
    gateway.fire_chain_run(
        chain_id=started["causation_id"],
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_synthesis_call(),
    )
    return client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research").json()


def test_approve_requires_proposed_status(ctx) -> None:
    client, core, gateway = ctx
    client.post(f"/campaigns/{_CAMPAIGN}/research")  # still pending

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    assert resp.status_code == 409


def test_approve_moves_proposed_to_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_reject_moves_proposed_to_rejected(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/reject")

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


# ── positioning ──────────────────────────────────────────────────────────────


def test_start_positioning_refuses_when_research_is_not_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)  # proposed, not approved

    resp = client.post(f"/campaigns/{_CAMPAIGN}/positioning")

    assert resp.status_code == 409
    assert gateway.requested == [
        r for r in gateway.requested if r["agent_name"] != "marketing-positioning"
    ]


def test_start_positioning_refuses_when_no_research_exists(ctx) -> None:
    client, _core, _gateway = ctx
    resp = client.post(f"/campaigns/{_CAMPAIGN}/positioning")
    assert resp.status_code == 404


def test_start_positioning_runs_once_research_is_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    resp = client.post(f"/campaigns/{_CAMPAIGN}/positioning")

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "positioning"
    assert body["status"] == "pending"
    positioning_requests = [
        r for r in gateway.requested if r["agent_name"] == "marketing-positioning"
    ]
    assert len(positioning_requests) == 1
    assert positioning_requests[0]["input_payload"]["research"]["summary"] == "One clear signal."


def test_get_positioning_artefact_proposes_once_it_succeeds(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    run_id = started["agent_run_id"]
    gateway.complete(run_id, messages=_positioning_call())

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    stored_body = json.loads(body["body"])
    assert stored_body["segments"][0]["name"] == "Segment"


# ── channel plan (M4, issue #3) ───────────────────────────────────────────────


def _propose_and_approve_positioning(client, core, gateway) -> dict[str, Any]:
    """Research approved, positioning proposed AND approved — the state
    `start_channel_plan_route` requires."""
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()
    gateway.complete(started["agent_run_id"], messages=_positioning_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")  # advances pending -> proposed
    return client.post(f"/campaigns/{_CAMPAIGN}/artefacts/positioning/approve").json()


def test_start_channel_plan_refuses_when_positioning_is_not_approved(ctx) -> None:
    """The gate enforcement itself, mirroring `test_start_positioning_refuses_
    when_research_is_not_approved`: `proposed` positioning must not be usable
    by the channel-plan stage, or the positioning approval gate is
    decorative."""
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()
    gateway.complete(started["agent_run_id"], messages=_positioning_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")  # proposed, not approved

    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    assert resp.status_code == 409
    assert gateway.requested == [
        r for r in gateway.requested if r["agent_name"] != "marketing-channel-plan"
    ]


def test_start_channel_plan_refuses_when_no_positioning_exists(ctx) -> None:
    client, _core, _gateway = ctx
    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")
    assert resp.status_code == 404


def test_start_channel_plan_runs_once_positioning_is_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "channel_plan"
    assert body["status"] == "pending"
    channel_plan_requests = [
        r for r in gateway.requested if r["agent_name"] == "marketing-channel-plan"
    ]
    assert len(channel_plan_requests) == 1
    assert channel_plan_requests[0]["input_payload"]["positioning"]["segments"][0]["name"] == (
        "Segment"
    )


def test_get_channel_plan_artefact_proposes_organic_and_paid_once_it_succeeds(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    run_id = started["agent_run_id"]
    gateway.complete(run_id, messages=_channel_plan_call())

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    stored_body = json.loads(body["body"])
    motions = {c["motion"] for c in stored_body["channels"]}
    assert motions == {"organic", "paid"}


def test_get_channel_plan_artefact_502s_a_zero_citation_run_and_leaves_it_pending(ctx) -> None:
    """The milestone's guard at the HTTP boundary, the channel-plan half: a
    run that cited nothing must not be proposed as if it were evidenced."""
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    run_id = started["agent_run_id"]
    gateway.complete(run_id, messages=_empty_channel_plan_call())

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")

    assert resp.status_code == 502
    assert core.artefacts[started["id"]]["status"] == "pending", "must not have been proposed"


def test_channel_plan_artefact_can_be_approved(ctx) -> None:
    """The same generic approve route already covers `channel_plan` once it
    is a known kind — nothing channel-plan-specific in `approve_artefact_route`
    itself, so this is the confirmation that wiring holds end to end."""
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()
    gateway.complete(started["agent_run_id"], messages=_channel_plan_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")  # proposes it

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
