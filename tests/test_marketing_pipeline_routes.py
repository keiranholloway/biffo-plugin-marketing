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

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000cd"


class _FakeCore:
    """An in-memory `marketing_campaign` + `marketing_artefact` store, reached
    the same way `admin_app._core` reaches the real one."""

    #: The tenant's seeded channel taxonomy (#76 increment 2) —
    #: `start_channel_plan_route` fetches this to build the agent's
    #: `channel_taxonomy` input and the `{channel_key: motion}` map every
    #: fixture below's `_channel_plan_call`/`_copy_call` channel_keys must
    #: resolve against.
    DEFAULT_CHANNELS: list[dict[str, Any]] = [
        {
            "key": "instagram_organic",
            "label": "Instagram — organic",
            "motion": "organic",
            "category": "social",
            "ad_platform": None,
        },
        {
            "key": "google_search_paid",
            "label": "Google Search ads",
            "motion": "paid",
            "category": "search",
            "ad_platform": "google",
        },
    ]

    def __init__(self, *, brief: str | None = "Reach multi-location operators.") -> None:
        self.campaign = {"id": _CAMPAIGN, "brief": brief}
        self.artefacts: dict[str, dict[str, Any]] = {}
        self.channels: list[dict[str, Any]] = [dict(c) for c in self.DEFAULT_CHANNELS]
        self._next_id = 0

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        prefix = admin_app._INTERNAL_PREFIX
        if method == "GET" and path == f"{prefix}/campaigns/{_CAMPAIGN}":
            return httpx.Response(200, json=self.campaign, request=request)
        if method == "GET" and path.startswith(f"{prefix}/campaigns/"):
            return httpx.Response(404, json={"detail": "not found"}, request=request)
        if method == "GET" and path == f"{prefix}/channels":
            return httpx.Response(200, json=self.channels, request=request)
        if method == "GET" and path == f"{prefix}/artefacts":
            params = kw.get("params") or {}
            rows = [
                a
                for a in self.artefacts.values()
                if a.get("campaign_id") == params.get("campaign_id")
                and a.get("kind") == params.get("kind")
            ]
            return httpx.Response(200, json=rows, request=request)
        if method == "POST" and path == f"{prefix}/artefacts":
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
        if method == "PATCH" and path.startswith(f"{prefix}/artefacts/"):
            artefact_id = path.removeprefix(f"{prefix}/artefacts/")
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
                                    "channel_key": "instagram_organic",
                                    "rank": 1,
                                    "rationale": "r",
                                    "sources": [{"url": url, "note": "n"}],
                                },
                                {
                                    "channel_key": "google_search_paid",
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


def test_start_positioning_uses_approved_research_even_with_a_newer_pending_run(ctx) -> None:
    """The divergence case issue #41 is about: an admin approves research,
    then re-runs it (to refresh it, say). The re-run creates a second
    `research` row with a later `created_at` and status `pending` — from
    that point, `_latest_artefact` alone returns the newer, unapproved row,
    so a caller that only checked that row's status would 409 a campaign
    that has perfectly good approved research sitting one row back. This is
    the "downstream stage still starts" shape from the two the issue names.
    """
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    # Re-run research: a second, newer, still-`pending` `research` artefact
    # now sorts ahead of the approved one by `created_at`.
    client.post(f"/campaigns/{_CAMPAIGN}/research")

    resp = client.post(f"/campaigns/{_CAMPAIGN}/positioning")

    assert resp.status_code == 201
    positioning_requests = [
        r for r in gateway.requested if r["agent_name"] == "marketing-positioning"
    ]
    assert len(positioning_requests) == 1
    # Grounded in the APPROVED research's synthesis, not the newer pending
    # run's (still-empty) one.
    assert positioning_requests[0]["input_payload"]["research"]["summary"] == "One clear signal."


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


def test_start_positioning_stashes_the_approved_research_citation_urls(ctx) -> None:
    """Issue #22: the legitimate source set for positioning is closed and
    already known — it is exactly the approved research's `citations` column —
    so it is stashed on the pending artefact at START time, the same way
    `research_run_ids`/`channel_taxonomy` already are. Captured here rather
    than re-read at advance time on purpose: it must be what THIS run was
    shown, not whatever research has been re-run to since."""
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    pending = json.loads(started["body"])
    assert pending["allowed_source_urls"] == ["https://example.com/thread"]


def test_get_positioning_artefact_502s_a_fabricated_citation_and_leaves_it_pending(ctx) -> None:
    """The issue's actual case at the HTTP boundary: the run cites *a* URL, so
    the count-only guard is satisfied, but it is not one the approved research
    ever contained. It must not reach an operator as evidenced."""
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    gateway.complete(
        started["agent_run_id"],
        messages=_positioning_call(url="https://marketing-statistics.example/benchmarks"),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")

    assert resp.status_code == 502
    assert "marketing-statistics.example" in resp.json()["detail"]
    assert core.artefacts[started["id"]]["status"] == "pending", "must not have been proposed"


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
    # The taxonomy fetched from `GET /channels` (#76 increment 2) rides along
    # as the agent's own input, not something it has to be told separately.
    taxonomy = channel_plan_requests[0]["input_payload"]["channel_taxonomy"]
    assert {c["channel_key"] for c in taxonomy} == {"instagram_organic", "google_search_paid"}


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


def test_start_channel_plan_stashes_the_approved_positioning_citation_urls(ctx) -> None:
    """Issue #22, the channel-plan half — stashed alongside `channel_taxonomy`
    in the same pending body, since both are "what this run was shown"."""
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)

    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    pending = json.loads(started["body"])
    assert pending["allowed_source_urls"] == ["https://example.com/thread"]
    assert pending["channel_taxonomy"], "the taxonomy stash must survive the new key"


def test_get_channel_plan_artefact_502s_a_fabricated_citation_and_leaves_it_pending(ctx) -> None:
    """The issue's case one stage down: a channel recommendation citing a URL
    the approved positioning never contained is the most confident-sounding
    fabrication in the pipeline."""
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    gateway.complete(
        started["agent_run_id"],
        messages=_channel_plan_call(url="https://marketing-statistics.example/benchmarks"),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")

    assert resp.status_code == 502
    assert "marketing-statistics.example" in resp.json()["detail"]
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


# ── copy (M5, issue #4) ────────────────────────────────────────────────────────


def _copy_call(url: str = "https://example.com/thread") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_copy",
                        "arguments": {
                            "channels": [
                                {
                                    "channel_key": "instagram_organic",
                                    "headline": "Run every site the same way, finally.",
                                    "body": "One dashboard, every location.",
                                    "cta": "See how it works",
                                    "sources": [{"url": url, "note": "n"}],
                                }
                            ]
                        },
                    }
                }
            ],
        }
    ]


def _empty_copy_call() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "submit_copy", "arguments": {"channels": []}}}],
        }
    ]


def _propose_and_approve_channel_plan(client, core, gateway) -> dict[str, Any]:
    """Research and positioning approved, channel plan proposed AND approved
    — the state `start_copy_route` requires from both upstream stages."""
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()
    gateway.complete(started["agent_run_id"], messages=_channel_plan_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")  # advances pending -> proposed
    return client.post(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan/approve").json()


def test_start_copy_refuses_when_positioning_is_not_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()
    gateway.complete(started["agent_run_id"], messages=_positioning_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")  # proposed, not approved

    resp = client.post(f"/campaigns/{_CAMPAIGN}/copy")

    assert resp.status_code == 409
    assert gateway.requested == [
        r for r in gateway.requested if r["agent_name"] != "marketing-copy"
    ]


def test_start_copy_refuses_when_no_positioning_exists(ctx) -> None:
    client, _core, _gateway = ctx
    resp = client.post(f"/campaigns/{_CAMPAIGN}/copy")
    assert resp.status_code == 404


def test_start_copy_refuses_when_channel_plan_is_not_approved(ctx) -> None:
    """The gate enforcement itself, mirroring the channel-plan/positioning
    pair: `proposed` channel plan must not be usable here, or its own
    approval gate is decorative."""
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()
    gateway.complete(started["agent_run_id"], messages=_channel_plan_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")  # proposed, not approved

    resp = client.post(f"/campaigns/{_CAMPAIGN}/copy")

    assert resp.status_code == 409
    assert gateway.requested == [
        r for r in gateway.requested if r["agent_name"] != "marketing-copy"
    ]


def test_start_copy_refuses_when_no_channel_plan_exists(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_positioning(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/copy")

    assert resp.status_code == 404


def test_start_copy_runs_once_both_upstream_artefacts_are_approved(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/copy")

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "copy"
    assert body["status"] == "pending"
    copy_requests = [r for r in gateway.requested if r["agent_name"] == "marketing-copy"]
    assert len(copy_requests) == 1
    assert copy_requests[0]["input_payload"]["positioning"]["segments"][0]["name"] == "Segment"
    assert copy_requests[0]["input_payload"]["channel_plan"]["channels"][0]["channel_key"] == (
        "instagram_organic"
    )


def test_get_copy_artefact_proposes_once_it_succeeds(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/copy").json()

    run_id = started["agent_run_id"]
    gateway.complete(run_id, messages=_copy_call())

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/copy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    stored_body = json.loads(body["body"])
    assert stored_body["channels"][0]["channel_key"] == "instagram_organic"
    assert stored_body["channels"][0]["motion"] == "organic"  # derived from the plan


def test_get_copy_artefact_502s_a_zero_citation_run_and_leaves_it_pending(ctx) -> None:
    """The milestone's guard at the HTTP boundary, the copy half: a run that
    cited nothing must not be proposed as if it were publish-ready."""
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/copy").json()

    run_id = started["agent_run_id"]
    gateway.complete(run_id, messages=_empty_copy_call())

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/copy")

    assert resp.status_code == 502
    assert core.artefacts[started["id"]]["status"] == "pending", "must not have been proposed"


def test_start_copy_stashes_the_union_of_both_approved_inputs_citation_urls(ctx) -> None:
    """Issue #22, the copy half. Copy is started against TWO approved
    artefacts, so its legitimate source set is the union of both — the
    channel plan's own citations are a subset of the positioning's in
    practice, but that is a consequence of this very check holding upstream,
    not something this stage should assume."""
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)

    started = client.post(f"/campaigns/{_CAMPAIGN}/copy").json()

    pending = json.loads(started["body"])
    assert pending["allowed_source_urls"] == ["https://example.com/thread"]
    assert pending["channel_plan_channels"], "the channel stash must survive the new key"


def test_get_copy_artefact_502s_a_fabricated_citation_and_leaves_it_pending(ctx) -> None:
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/copy").json()

    gateway.complete(
        started["agent_run_id"],
        messages=_copy_call(url="https://marketing-statistics.example/benchmarks"),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/copy")

    assert resp.status_code == 502
    assert "marketing-statistics.example" in resp.json()["detail"]
    assert core.artefacts[started["id"]]["status"] == "pending", "must not have been proposed"


def test_a_pending_artefact_started_before_this_check_still_advances(ctx) -> None:
    """The deploy-day case: a positioning run started by the old code carries
    no `allowed_source_urls` in its pending body. That is "not known", not
    "nothing allowed" — it must still advance, or shipping this check would
    strand every in-flight run on evidence nobody recorded."""
    client, core, gateway = ctx
    _propose_research(client, core, gateway)
    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()
    core.artefacts[started["id"]]["body"] = None  # as the pre-#22 route wrote it

    gateway.complete(
        started["agent_run_id"],
        messages=_positioning_call(url="https://marketing-statistics.example/benchmarks"),
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")

    assert resp.status_code == 200
    assert resp.json()["status"] == "proposed"


def test_copy_artefact_can_be_approved(ctx) -> None:
    """The same generic approve route already covers `copy` once it is a
    known kind — confirms the wiring holds end to end, same as the
    channel-plan equivalent above."""
    client, core, gateway = ctx
    _propose_and_approve_channel_plan(client, core, gateway)
    started = client.post(f"/campaigns/{_CAMPAIGN}/copy").json()
    gateway.complete(started["agent_run_id"], messages=_copy_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/copy")  # proposes it

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/copy/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


# ── partial approval / selection (issue #145) ─────────────────────────────────
#
# `approve_artefact_route` used to flip an artefact's whole body `proposed ->
# approved` in one move. Every artefact is a LIST, so an operator who wanted 8
# of 9 channel recommendations had to accept the ninth or reject the whole
# run. These tests exercise the fix end to end over the HTTP routes; the pure
# functions it is built from (`with_element_ids`/`selected_body`/
# `source_urls_from_body`/`known_element_ids`) have their own unit tests in
# `tests/test_marketing_element_ids.py`.


def _synthesis_call_two_findings(
    url_a: str = "https://example.com/a", url_b: str = "https://example.com/b"
) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_research_synthesis",
                        "arguments": {
                            "summary": "Two signals.",
                            "findings": [
                                {
                                    "signal": "A",
                                    "why_it_matters": "m",
                                    "sources": [{"url": url_a, "note": "n"}],
                                },
                                {
                                    "signal": "B",
                                    "why_it_matters": "m",
                                    "sources": [{"url": url_b, "note": "n"}],
                                },
                            ],
                        },
                    }
                }
            ],
        }
    ]


def _propose_research_two_findings(client, core, gateway) -> dict[str, Any]:
    started = client.post(f"/campaigns/{_CAMPAIGN}/research").json()
    for run_id in list(gateway._runs):
        gateway.complete(run_id)
    gateway.fire_chain_run(
        chain_id=started["causation_id"],
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_synthesis_call_two_findings(),
    )
    return client.get(f"/campaigns/{_CAMPAIGN}/artefacts/research").json()


def test_approve_stamps_ids_on_every_element_of_a_proposed_artefact(ctx) -> None:
    """Issue #145's core requirement, at the HTTP boundary: ids are assigned
    at persist time, so every element already has one by the time an operator
    could even try to select by it."""
    client, core, gateway = ctx
    proposed = _propose_research_two_findings(client, core, gateway)

    ids = [f["id"] for f in json.loads(proposed["body"])["findings"]]

    assert all(isinstance(i, str) and i for i in ids)
    assert len(set(ids)) == len(ids)


def test_approve_rejects_an_empty_selection(ctx) -> None:
    """Design decision #6: an empty `element_ids` is not a distinct way to
    approve nothing — that is what the reject route is for."""
    client, core, gateway = ctx
    proposed = _propose_research(client, core, gateway)

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve", json={"element_ids": []}
    )

    assert resp.status_code == 422
    assert core.artefacts[proposed["id"]]["status"] == "proposed"


def test_approve_rejects_an_unknown_element_id(ctx) -> None:
    """A stale page selecting a since-regenerated element must not silently
    approve nothing — it must be told the selection is stale."""
    client, core, gateway = ctx
    proposed = _propose_research(client, core, gateway)

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve",
        json={"element_ids": ["not-a-real-id"]},
    )

    assert resp.status_code == 422
    assert "not-a-real-id" in resp.json()["detail"]
    assert core.artefacts[proposed["id"]]["status"] == "proposed"


def test_approve_with_no_body_leaves_the_selection_null(ctx) -> None:
    """Today's no-body approve — every existing caller, including the UI,
    which sends no body at all — keeps working unchanged, and NULL is what
    makes a downstream read treat it as "everything"."""
    client, core, gateway = ctx
    proposed = _propose_research(client, core, gateway)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert core.artefacts[proposed["id"]].get("approved_selection") is None


def test_approve_with_a_subset_stores_it(ctx) -> None:
    client, core, gateway = ctx
    proposed = _propose_research_two_findings(client, core, gateway)
    keep_id = json.loads(proposed["body"])["findings"][0]["id"]

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve", json={"element_ids": [keep_id]}
    )

    assert resp.status_code == 200
    assert json.loads(core.artefacts[proposed["id"]]["approved_selection"]) == [keep_id]


def test_null_selection_behaves_as_everything_against_a_pre_change_artefact(ctx) -> None:
    """The explicit backwards-compatibility case (issue #145): an artefact
    approved before this change has a body with no `id` on any element at all
    (never passed through `with_element_ids`) and `approved_selection` is
    NULL. It must keep flowing into the next stage's input whole, not empty."""
    client, core, gateway = ctx
    proposed = _propose_research(client, core, gateway)
    # Simulate a pre-#145 row: strip the ids `with_element_ids` stamped, as a
    # deployment's existing approved research would never have had them.
    legacy_body = json.loads(proposed["body"])
    for finding in legacy_body["findings"]:
        finding.pop("id", None)
    core.artefacts[proposed["id"]]["body"] = json.dumps(legacy_body)

    client.post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")  # no body -> NULL selection
    assert core.artefacts[proposed["id"]].get("approved_selection") is None

    resp = client.post(f"/campaigns/{_CAMPAIGN}/positioning")

    assert resp.status_code == 201
    positioning_requests = [
        r for r in gateway.requested if r["agent_name"] == "marketing-positioning"
    ]
    assert len(positioning_requests[0]["input_payload"]["research"]["findings"]) == 1


def test_a_subset_narrows_what_the_next_stage_sees(ctx) -> None:
    """The behaviour the whole issue exists for: approving fewer than every
    element removes the rest from what the next stage is shown, not just from
    what an operator sees on screen — and the provenance check narrows WITH
    the selection (issue #145's most quietly-breakable requirement)."""
    client, core, gateway = ctx
    proposed = _propose_research_two_findings(client, core, gateway)
    keep = next(f for f in json.loads(proposed["body"])["findings"] if f["signal"] == "A")

    client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve", json={"element_ids": [keep["id"]]}
    )
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    positioning_requests = [
        r for r in gateway.requested if r["agent_name"] == "marketing-positioning"
    ]
    sent_findings = positioning_requests[0]["input_payload"]["research"]["findings"]
    assert [f["signal"] for f in sent_findings] == ["A"]
    pending = json.loads(started["body"])
    assert pending["allowed_source_urls"] == ["https://example.com/a"]


def test_a_legitimate_citation_of_a_surviving_finding_still_passes(ctx) -> None:
    """Provenance narrows CORRECTLY, not merely narrows: a positioning run
    that cites exactly what survived the selection must still be accepted —
    proving this isn't a check that fails closed on every real citation the
    moment any selection at all is applied."""
    client, core, gateway = ctx
    proposed = _propose_research_two_findings(client, core, gateway)
    keep = next(f for f in json.loads(proposed["body"])["findings"] if f["signal"] == "A")
    client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve", json={"element_ids": [keep["id"]]}
    )
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    gateway.complete(
        started["agent_run_id"], messages=_positioning_call(url="https://example.com/a")
    )
    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")

    assert resp.status_code == 200
    assert resp.json()["status"] == "proposed"


def test_citing_a_dropped_findings_url_now_fails(ctx) -> None:
    """The other half of "narrows correctly": a positioning run that cites
    the URL belonging to the finding the operator did NOT approve is treated
    exactly like any other uncited source — even though that URL was
    perfectly legitimate before the selection was applied. This is the case
    issue #145 says is "the one most likely to break quietly" if the
    provenance check does not narrow along with the selection."""
    client, core, gateway = ctx
    proposed = _propose_research_two_findings(client, core, gateway)
    keep = next(f for f in json.loads(proposed["body"])["findings"] if f["signal"] == "A")
    client.post(
        f"/campaigns/{_CAMPAIGN}/artefacts/research/approve", json={"element_ids": [keep["id"]]}
    )
    started = client.post(f"/campaigns/{_CAMPAIGN}/positioning").json()

    gateway.complete(
        started["agent_run_id"], messages=_positioning_call(url="https://example.com/b")
    )
    resp = client.get(f"/campaigns/{_CAMPAIGN}/artefacts/positioning")

    assert resp.status_code == 502
    assert core.artefacts[started["id"]]["status"] == "pending"
