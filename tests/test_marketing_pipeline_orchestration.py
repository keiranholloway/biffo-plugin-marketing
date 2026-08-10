"""Fan-out/fan-in orchestration over a fake `AgentGateway`.

Fakes rather than mocks, matching this repo's own convention
(`test_marketing_mint_route.py`): what is worth asserting is *what run was
requested with what causation_id*, and a fake that records the calls says
that directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from marketing import pipeline
from marketing.definitions import (
    CHANNEL_PLAN_AGENT_NAME,
    POSITIONING_AGENT_NAME,
    RESEARCH_AGENT_NAMES,
    RESEARCH_SYNTHESIS_AGENT_NAME,
)


@dataclass
class _RequestedRun:
    agent_name: str
    causation_id: str
    input_payload: dict[str, Any]


class _FakeGateway:
    """Records every requested run; runs are advanced to completion by the
    test calling `complete()` / `fire_chain_run()` directly, mirroring how the
    orchestration engine would (invisibly, from this plugin's point of view)."""

    def __init__(self) -> None:
        self.requested: list[_RequestedRun] = []
        self._runs: dict[str, pipeline.AgentRunView] = {}
        self._chain_runs: dict[tuple[str, str], str] = {}
        self._next_id = 0

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        del definition, output_tool
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self.requested.append(
            _RequestedRun(
                agent_name=agent_name, causation_id=causation_id, input_payload=input_payload
            )
        )
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="pending", messages=[])
        return run_id

    async def find_chain_run(
        self, *, chain_id: str, agent_name: str
    ) -> pipeline.AgentRunView | None:
        run_id = self._chain_runs.get((chain_id, agent_name))
        return None if run_id is None else self._runs[run_id]

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
        return self._runs.get(run_id)

    # ── Test-only helpers, standing in for the runtime and the engine ────────

    def complete(
        self, run_id: str, *, status: str = "completed", messages: list | None = None
    ) -> None:
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or []
        )

    def fire_chain_run(
        self,
        *,
        chain_id: str,
        agent_name: str,
        status: str = "completed",
        messages: list | None = None,
    ) -> str:
        """Simulate the engine's `agent_fan_in` firing a joining agent."""
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or []
        )
        self._chain_runs[(chain_id, agent_name)] = run_id
        return run_id


def _findings_call(angle: str, url: str = "https://example.com/x") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_research_synthesis",
                        "arguments": {
                            "summary": f"Findings for {angle}.",
                            "findings": [
                                {
                                    "signal": "x",
                                    "why_it_matters": "y",
                                    "sources": [{"url": url, "note": "n"}],
                                }
                            ],
                        },
                    }
                }
            ],
        }
    ]


# ── start_research / advance_research ────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_research_fans_out_both_agents_on_one_shared_causation_id() -> None:
    gateway = _FakeGateway()

    chain_id, run_ids = await pipeline.start_research(gateway, brief={"campaign_id": "c1"})

    assert len(run_ids) == 2
    assert {r.agent_name for r in gateway.requested} == set(RESEARCH_AGENT_NAMES)
    assert all(r.causation_id == chain_id for r in gateway.requested)


@pytest.mark.asyncio
async def test_advance_research_returns_none_while_research_still_running() -> None:
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    # Neither research run has been completed yet — still "pending" in the fake.

    result = await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    assert result is None


@pytest.mark.asyncio
async def test_advance_research_returns_none_before_the_engine_fires_synthesis() -> None:
    """Both research runs succeeded, but nothing has discovered the
    engine-fired synthesis run yet — must not be mistaken for a failure."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="completed")

    result = await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    assert result is None


@pytest.mark.asyncio
async def test_advance_research_returns_the_synthesis_once_the_engine_fires_it() -> None:
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="completed")
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_findings_call("audience"),
    )

    result = await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    assert isinstance(result, pipeline.ResearchSynthesis)
    assert result.findings[0].sources[0].url == "https://example.com/x"


@pytest.mark.asyncio
async def test_advance_research_raises_when_the_synthesis_run_itself_failed() -> None:
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="completed")
    gateway.fire_chain_run(
        chain_id=chain_id, agent_name=RESEARCH_SYNTHESIS_AGENT_NAME, status="failed"
    )

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)


@pytest.mark.asyncio
async def test_advance_research_raises_when_every_research_agent_failed() -> None:
    """The engine correctly never fires a synthesis run over an empty set —
    this must not sit `pending` forever with nothing to explain why."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="failed")

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)


# ── start_positioning / advance_positioning ──────────────────────────────────


@pytest.mark.asyncio
async def test_start_positioning_requests_one_run_carrying_the_research_body() -> None:
    gateway = _FakeGateway()
    research_body = {"summary": "s", "findings": []}

    causation_id, run_id = await pipeline.start_positioning(gateway, research_body=research_body)

    assert len(gateway.requested) == 1
    requested = gateway.requested[0]
    assert requested.agent_name == POSITIONING_AGENT_NAME
    assert requested.causation_id == causation_id
    assert requested.input_payload == {"research": research_body}
    assert run_id  # a real id was returned


@pytest.mark.asyncio
async def test_advance_positioning_returns_none_while_running() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_positioning(gateway, research_body={})

    result = await pipeline.advance_positioning(gateway, run_id=run_id)

    assert result is None


@pytest.mark.asyncio
async def test_advance_positioning_returns_positioning_once_succeeded() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_positioning(gateway, research_body={})
    gateway.complete(
        run_id,
        messages=[
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
                                        "sources": [{"url": "https://example.com/y", "note": "n"}],
                                    }
                                ],
                                "pillars": [],
                                "ctas": [],
                            },
                        }
                    }
                ],
            }
        ],
    )

    result = await pipeline.advance_positioning(gateway, run_id=run_id)

    assert isinstance(result, pipeline.Positioning)
    assert result.segments[0].name == "Segment"


@pytest.mark.asyncio
async def test_advance_positioning_raises_when_the_run_failed() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_positioning(gateway, research_body={})
    gateway.complete(run_id, status="failed")

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_positioning(gateway, run_id=run_id)


# ── start_channel_plan / advance_channel_plan (M4) ───────────────────────────


@pytest.mark.asyncio
async def test_start_channel_plan_requests_one_run_carrying_the_positioning_body() -> None:
    gateway = _FakeGateway()
    positioning_body = {"segments": [], "pillars": [], "ctas": []}

    causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body=positioning_body
    )

    assert len(gateway.requested) == 1
    requested = gateway.requested[0]
    assert requested.agent_name == CHANNEL_PLAN_AGENT_NAME
    assert requested.causation_id == causation_id
    assert requested.input_payload == {"positioning": positioning_body}
    assert run_id  # a real id was returned


@pytest.mark.asyncio
async def test_advance_channel_plan_returns_none_while_running() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(gateway, positioning_body={})

    result = await pipeline.advance_channel_plan(gateway, run_id=run_id)

    assert result is None


@pytest.mark.asyncio
async def test_advance_channel_plan_returns_the_plan_once_succeeded() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(gateway, positioning_body={})
    gateway.complete(
        run_id,
        messages=[
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
                                        "sources": [{"url": "https://example.com/y", "note": "n"}],
                                    }
                                ]
                            },
                        }
                    }
                ],
            }
        ],
    )

    result = await pipeline.advance_channel_plan(gateway, run_id=run_id)

    assert isinstance(result, pipeline.ChannelPlan)
    assert result.channels[0].channel == "Instagram Reels"


@pytest.mark.asyncio
async def test_advance_channel_plan_raises_when_the_run_failed() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(gateway, positioning_body={})
    gateway.complete(run_id, status="failed")

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_channel_plan(gateway, run_id=run_id)


# ── require_approved ──────────────────────────────────────────────────────────


def test_require_approved_passes_on_approved() -> None:
    pipeline.require_approved("approved", what="Research")  # must not raise


@pytest.mark.parametrize("status", ["pending", "proposed", "rejected", ""])
def test_require_approved_raises_on_anything_else(status: str) -> None:
    with pytest.raises(pipeline.ArtefactNotApprovedError):
        pipeline.require_approved(status, what="Research")
