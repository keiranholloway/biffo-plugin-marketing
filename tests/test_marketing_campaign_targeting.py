"""Campaign-level motion and operator channel selection (issue #67).

The channel-plan agent used to choose both the motion mix and the channel set
on its own, and the operator's first involvement was approving or rejecting a
finished plan. #67 moves two decisions earlier — to the campaign, before the
plan runs:

1. **Motion** (`organic` / `paid` / `both`) is campaign state, set alongside
   the brief.
2. **Target channels** are selected by the operator from the seeded taxonomy,
   also campaign state, and the plan runs *within* that selection.

## Why these tests assert a STRUCTURAL constraint, not a prompted one

#128's lesson, stated in this repo's own code: a constraint the model is
merely *asked* to respect is not a constraint. So the tests below never
assert "the prompt mentions organic". They assert that:

- the taxonomy the run is **shown** is already narrowed to
  `selection ∩ motion`, so a paid `channel_key` on an organic campaign is
  rejected by the taxonomy validator #76 increment 2 already ships
  (`pipeline.extract_channel_plan`), with no new trust in the model; and
- the one place the model still asserts a motion of its own — a
  `suggested_label` proposal for a channel outside the taxonomy — is checked
  against the campaign's motion when the run comes back
  (`MotionNotAllowedError`).

Both halves are checked against what **this run** was shown, stashed on the
pending artefact at start time, exactly as `channel_taxonomy` (#76) and
`allowed_source_urls` (#22/#113) already are — never re-read at advance time,
where the campaign may have been edited since.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marketing import admin_app, definitions, pipeline

_MANIFEST = Path(__file__).resolve().parents[1] / "biffo.plugin.json"
_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ce"
_URL = "https://example.com/thread"


class _FakeCore:
    """An in-memory `marketing_campaign` + `marketing_artefact` + taxonomy
    store, reached the way `admin_app._core` reaches the real one — the same
    fake shape `test_marketing_pipeline_routes.py` uses, kept local so this
    module can vary the campaign's motion/selection per test."""

    CHANNELS: list[dict[str, Any]] = [
        {
            "key": "instagram_organic",
            "label": "Instagram — organic",
            "motion": "organic",
            "category": "social",
            "ad_platform": None,
        },
        {
            "key": "linkedin_organic",
            "label": "LinkedIn — organic",
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

    def __init__(
        self,
        *,
        motion: str | None = "both",
        target_channel_keys: str | None = "instagram_organic,linkedin_organic,google_search_paid",
    ) -> None:
        self.campaign: dict[str, Any] = {
            "id": _CAMPAIGN,
            "brief": "Reach multi-location operators.",
            "motion": motion,
            "target_channel_keys": target_channel_keys,
        }
        self.artefacts: dict[str, dict[str, Any]] = {}
        self.channels = [dict(c) for c in self.CHANNELS]
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
                "created_at": f"2026-08-14T00:00:{self._next_id:02d}Z",
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
    def __init__(self) -> None:
        self.requested: list[dict[str, Any]] = []
        self._runs: dict[str, pipeline.AgentRunView] = {}
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

    async def find_chain_run(self, *, chain_id: str, agent_name: str):  # pragma: no cover - unused
        del chain_id, agent_name
        return None

    async def get_agent_run(self, *, run_id: str):
        return self._runs.get(run_id)

    def complete(self, run_id: str, *, messages: list) -> None:
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="completed", messages=messages)


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def _client(core: _FakeCore, gateway: _FakeGateway, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(admin_app, "_core", core)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = _admin_user
    app.dependency_overrides[admin_app.get_agent_gateway] = lambda: gateway
    return TestClient(app)


def _approve_positioning(client: TestClient, gateway: _FakeGateway, core: _FakeCore) -> None:
    """Drive research + positioning to approved, which is what
    `start_channel_plan_route` gates on — everything this module cares about
    happens after that gate."""
    research = client.post(f"/campaigns/{_CAMPAIGN}/research").json()
    gateway.complete(
        research["agent_run_id"] or "run-1",
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "submit_research_synthesis",
                            "arguments": {
                                "summary": "s",
                                "findings": [
                                    {
                                        "signal": "s",
                                        "why_it_matters": "m",
                                        "sources": [{"url": _URL, "note": "n"}],
                                    }
                                ],
                            },
                        }
                    }
                ],
            }
        ],
    )
    # Research fans out and fans in via the engine; the positioning stage is
    # the only approved parent `start_channel_plan_route` actually reads, so
    # seed that artefact directly rather than simulating the fan-in.
    core._next_id += 1
    artefact_id = f"artefact-{core._next_id}"
    core.artefacts[artefact_id] = {
        "id": artefact_id,
        "created_at": "2026-08-14T00:01:00Z",
        "campaign_id": _CAMPAIGN,
        "kind": "positioning",
        "status": "approved",
        "causation_id": "chain",
        "agent_run_id": "run-positioning",
        "body": json.dumps(
            {
                "segments": [
                    {
                        "id": "seg-1",
                        "name": "Segment",
                        "description": "d",
                        "sources": [{"url": _URL, "note": "n"}],
                    }
                ],
                "pillars": [],
                "ctas": [],
            }
        ),
        "citations": json.dumps([{"url": _URL, "note": "n"}]),
    }


def _plan_call(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "submit_channel_plan", "arguments": {"channels": channels}}}
            ],
        }
    ]


def _evidence_call() -> list[dict[str, Any]]:
    """The grounding run's output (#65). These runs carry no `annotations`, so
    nothing is handed on to cite and nothing in this module's subject — the
    campaign's motion — depends on it; the tool call still has to be there,
    because a grounding run that answered in prose is the #159 failure."""
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "submit_channel_evidence", "arguments": {"evidence": []}}}
            ],
        }
    ]


def _plan_through_both_runs(
    client: TestClient,
    core: _FakeCore,
    gateway: _FakeGateway,
    started: dict[str, Any],
    messages: list[dict[str, Any]],
) -> httpx.Response:
    """Drive both runs of the two-step channel stage and return the response
    that carries the plan (#65).

    `agent_run_id` is the grounding run; the planning run is started by the
    first advance and its id is stashed in the pending body, which is where
    this reads it from — deliberately, rather than assuming a run id, because
    that stashing is the thing that stops a second planning run being started
    on every poll.
    """
    gateway.complete(started["agent_run_id"], messages=_evidence_call())
    client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")
    plan_run_id = json.loads(core.artefacts[started["id"]]["body"])["channel_plan_run_id"]
    gateway.complete(plan_run_id, messages=messages)
    return client.get(f"/campaigns/{_CAMPAIGN}/artefacts/channel_plan")


def _recommendation(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "channel_key": "instagram_organic",
        "rank": 1,
        "rationale": "r",
        "sources": [{"url": _URL, "note": "n"}],
    }
    base.update(overrides)
    return base


# ── Motion is campaign state, with a closed vocabulary ───────────────────────


def test_the_campaign_carries_motion_and_target_channels() -> None:
    """Both live on `marketing_campaign`, next to the brief — #67 settles
    motion as a campaign-strategy property, and the selection is per-campaign
    state read once, wholesale, when the plan starts."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    (campaign,) = [t for t in manifest["tables"] if t["name"] == "marketing_campaign"]
    columns = {c["name"]: c for c in campaign["columns"]}

    assert columns["motion"]["type"] == "String(16)"
    assert columns["motion"]["nullable"] is True
    # Text, not String(255) like `media_kinds`: 30+ keys of up to 64
    # characters each outgrow 255 long before the taxonomy stops growing.
    assert columns["target_channel_keys"]["type"] == "Text"
    assert columns["target_channel_keys"]["nullable"] is True


def test_campaign_motions_are_exactly_organic_paid_both() -> None:
    assert definitions.CAMPAIGN_MOTIONS == ("organic", "paid", "both")


@pytest.mark.parametrize(
    ("campaign_motion", "expected"),
    [
        ("organic", {"organic"}),
        ("paid", {"paid"}),
        ("both", {"organic", "paid"}),
    ],
)
def test_motions_allowed_by_maps_campaign_motion_to_channel_motions(
    campaign_motion: str, expected: set[str]
) -> None:
    assert definitions.motions_allowed_by(campaign_motion) == expected


def test_an_unknown_campaign_motion_is_a_hard_failure_not_a_permissive_default() -> None:
    with pytest.raises(ValueError):
        definitions.motions_allowed_by("hybrid")


# ── The gate: neither decision may be left to the agent ──────────────────────


def test_channel_plan_refuses_a_campaign_with_no_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(motion=None)
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    assert resp.status_code == 422
    assert "motion" in resp.json()["detail"].lower()
    # Refused before anything was spent: no channel-plan run was requested.
    assert [r for r in gateway.requested if "channel_taxonomy" in r["input_payload"]] == []


def test_channel_plan_refuses_a_campaign_with_no_channel_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _FakeCore(target_channel_keys=None)
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    assert resp.status_code == 422
    assert "channel" in resp.json()["detail"].lower()


def test_channel_plan_refuses_a_selection_that_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An organic campaign whose only selected channels are paid ones — a
    real state, reachable by choosing channels and then narrowing the motion.
    Refused with a sentence naming the disagreement rather than quietly
    planning against an empty taxonomy, which would return an empty plan and
    read as "the agent found nothing"."""
    core = _FakeCore(motion="organic", target_channel_keys="google_search_paid")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    resp = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    assert resp.status_code == 422
    assert "organic" in resp.json()["detail"].lower()


# ── The constraint is applied to the INPUT, so it needs no trust ─────────────


def test_the_run_is_shown_only_the_selected_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(target_channel_keys="linkedin_organic")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    (plan_run,) = [r for r in gateway.requested if "channel_taxonomy" in r["input_payload"]]
    assert [c["channel_key"] for c in plan_run["input_payload"]["channel_taxonomy"]] == [
        "linkedin_organic"
    ]


def test_an_organic_campaign_is_never_shown_a_paid_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard half of #67's motion constraint. Not "the prompt discourages
    paid": the paid rows are absent from the taxonomy the run is given, and
    `extract_channel_plan` rejects any `channel_key` outside that snapshot —
    so a paid recommendation cannot survive, whatever the model does."""
    core = _FakeCore(motion="organic")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    (plan_run,) = [r for r in gateway.requested if "channel_taxonomy" in r["input_payload"]]
    shown = {c["channel_key"] for c in plan_run["input_payload"]["channel_taxonomy"]}
    assert shown == {"instagram_organic", "linkedin_organic"}
    stashed = json.loads(started["body"])
    assert set(stashed["channel_taxonomy"]) == shown
    assert sorted(stashed["allowed_motions"]) == ["organic"]


def test_the_run_is_told_the_campaign_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Told, so it does not spend output proposing what will be rejected —
    but the telling is not the enforcement; the two tests above are."""
    core = _FakeCore(motion="paid")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    client.post(f"/campaigns/{_CAMPAIGN}/channel-plan")

    (plan_run,) = [r for r in gateway.requested if "channel_taxonomy" in r["input_payload"]]
    assert plan_run["input_payload"]["campaign_motion"] == "paid"


def test_the_selection_does_not_widen_the_citation_provenance_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113's guard threads the approved parent's sources through untouched.
    A channel selection is a constraint on OUTPUT, never a source of
    evidence, so `allowed_source_urls` must still be exactly the approved
    positioning's URLs."""
    core = _FakeCore()
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)

    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    assert json.loads(started["body"])["allowed_source_urls"] == [_URL]


# ── The one place the model still asserts a motion: an outside proposal ──────


def test_a_proposal_outside_the_campaign_motion_fails_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _FakeCore(motion="organic")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    resp = _plan_through_both_runs(
        client,
        core,
        gateway,
        started,
        _plan_call(
            [
                _recommendation(),
                _recommendation(
                    channel_key=None, suggested_label="Sponsored trade newsletter", motion="paid"
                ),
            ]
        ),
    )

    assert resp.status_code == 502
    assert "organic" in resp.json()["detail"].lower()
    # Left pending, not half-written — same discipline as every other
    # extraction failure in this pipeline.
    assert core.artefacts[started["id"]]["status"] == "pending"


def test_a_proposal_within_the_campaign_motion_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#67 keeps the agent able to propose outside the operator's selection —
    the constraint is the motion and the taxonomy, not the ability to
    suggest."""
    core = _FakeCore(motion="organic")
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()

    resp = _plan_through_both_runs(
        client,
        core,
        gateway,
        started,
        _plan_call(
            [
                _recommendation(
                    channel_key=None, suggested_label="Regional franchise forum", motion="organic"
                )
            ]
        ),
    )

    assert resp.status_code == 200
    (channel,) = json.loads(resp.json()["body"])["channels"]
    assert channel["suggested_label"] == "Regional franchise forum"
    assert channel["channel_key"] is None


def test_extract_channel_plan_rejects_a_taxonomy_row_outside_the_allowed_motions() -> None:
    """Defence in depth: the route filters the taxonomy, and the extractor
    checks it again. A caller that filtered wrongly cannot smuggle a paid
    channel into an organic campaign's plan."""
    with pytest.raises(pipeline.MotionNotAllowedError):
        pipeline.extract_channel_plan(
            _plan_call([_recommendation(channel_key="google_search_paid")]),
            taxonomy={"google_search_paid": "paid"},
            allowed_motions=frozenset({"organic"}),
            allowed_source_urls=[_URL],
        )


def test_extract_channel_plan_defaults_to_allowing_both_motions() -> None:
    """A run started before this change stashed no `allowed_motions`, and was
    never constrained by one. Failing it retroactively would reject an
    artefact for a rule it was never shown — the same reasoning
    `allowed_source_urls=None` already encodes for #22."""
    plan = pipeline.extract_channel_plan(
        _plan_call([_recommendation(channel_key="google_search_paid")]),
        taxonomy={"google_search_paid": "paid"},
        allowed_source_urls=[_URL],
    )

    assert plan.channels[0].motion == "paid"


def test_a_legacy_pending_artefact_still_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end version of the above, through the advance path: a pending
    row written before this change has no `allowed_motions` key at all."""
    core = _FakeCore()
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)
    _approve_positioning(client, gateway, core)
    started = client.post(f"/campaigns/{_CAMPAIGN}/channel-plan").json()
    legacy = json.loads(core.artefacts[started["id"]]["body"])
    legacy.pop("allowed_motions", None)
    core.artefacts[started["id"]]["body"] = json.dumps(legacy)

    resp = _plan_through_both_runs(
        client,
        core,
        gateway,
        started,
        _plan_call([_recommendation(channel_key="google_search_paid")]),
    )

    assert resp.status_code == 200


def test_channel_plan_still_404s_an_unknown_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore()
    gateway = _FakeGateway()
    client = _client(core, gateway, monkeypatch)

    resp = client.post(f"/campaigns/{uuid.uuid4()}/channel-plan")

    assert resp.status_code == 404
