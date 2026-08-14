"""Ground in one run, structure in a second one that is HANDED the evidence
(issue #65, after #155/#162).

## What the live run on 2026-08-14 established, and what it left

Retrieval on this stage works, and works well: `channel plan retrieval breadth:
10 distinct URLs across 1 grounded run(s) (0 shared by more than one, 10 deep
pages, 0 site roots)` — every page deep, not a single site root. The agent
called its output tool (#162's fix) and recommended six channels. And
`UngroundedRecommendationError` (#155) correctly refused all six: every source
on every recommendation had been carried over from the approved positioning.

So the gap is **citation behaviour, not grounding**. The model had its own
evidence in context and did not treat it as citable. `:online` injects the
retrieved pages into the context and records them on `annotations`, but nothing
in the payload ever presented those URLs to the model *as an enumerated set it
should cite from* — while the positioning's sources are enumerated, in the
payload, as structured `Source` objects. It cited the list it could see.

This module pins the two-step that closes that gap:

1. **`marketing-channel-evidence`** — the `:online` run. Its job is to retrieve
   and to read: it returns one note per page it was given. Its `annotations`
   are what the runtime recorded, independent of the model.
2. **`marketing-channel-plan`** — handed `retrieved_evidence` as the FIRST key
   of its payload: one entry per URL the *runtime* recorded, each carrying what
   step one said that page shows. It does not search, and cannot: it is not on
   an `:online` route at all.

Four properties are pinned here because each of them fails silently:

* **The URL set handed to step two comes from `annotations`, never from the
  model's own account of what it read.** Using step one's tool call as the
  authority would reintroduce exactly the trust this pipeline refuses
  everywhere else (`AgentRunView.annotations`' own docstring, #82) — and would
  let a model invent a URL in step one and then "cite what it retrieved" in
  step two.
* **The guard's subject is step ONE's annotations.** Step two never grounds, so
  its own `annotations` can only ever be the "not a grounded run" state.
  Reading it would make `UngroundedRecommendationError` structurally blind —
  the identical defect issue #90 found in `advance_research`, where the guard
  was reading the synthesis run rather than the runs that retrieved.
* **The guard is not softened.** Every assertion #155 shipped still holds; they
  are simply now asked of a plan produced by a model that was shown the
  evidence as a list.
* **Step two is started exactly once.** The stage is polled, so anything
  triggered by "the grounding run is terminal" happens on every poll unless the
  transition is recorded.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from marketing import pipeline
from marketing.definitions import (
    CHANNEL_EVIDENCE_AGENT_NAME,
    CHANNEL_EVIDENCE_TOOL_NAME,
    CHANNEL_PLAN_AGENT_NAME,
    CHANNEL_PLAN_TOOL_NAME,
    DEFAULT_CHANNEL_EVIDENCE_MODEL,
    DEFAULT_CHANNEL_PLAN_MODEL,
)

_PARENT_URL = "https://positioning.example.com/segment-evidence"
_RETRIEVED_URL = "https://benchmarks.example.com/paid-search-cpa-2026"
_SECOND_RETRIEVED_URL = "https://casestudies.example.com/multi-site-operators"
_UNRETRIEVED_URL = "https://invented.example.com/statistics"

_ANNOTATIONS = [
    {"type": "url_citation", "url": _RETRIEVED_URL, "title": "CPA benchmarks 2026"},
    {"type": "url_citation", "url": _SECOND_RETRIEVED_URL, "title": "Operator case studies"},
]

_TAXONOMY: dict[str, Literal["organic", "paid"]] = {
    "google_search_paid": "paid",
    "linkedin_organic": "organic",
}

_TAXONOMY_ROWS = [
    {
        "channel_key": "google_search_paid",
        "label": "Google Search ads",
        "motion": "paid",
        "category": "search",
    }
]

_POSITIONING_BODY = {
    "segments": [
        {
            "name": "Multi-location operators",
            "description": "Run 3+ sites and cannot see them in one place.",
            "sources": [{"url": _PARENT_URL, "note": "n"}],
        }
    ],
    "pillars": [],
    "ctas": [],
}


class _FakeGateway:
    """Records every requested run and lets a test complete each one
    independently — the two-step needs two runs in different states at once,
    which the single-run fakes elsewhere cannot express."""

    def __init__(self) -> None:
        self.requested: list[dict[str, Any]] = []
        self._runs: dict[str, pipeline.AgentRunView] = {}

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        run_id = f"run-{len(self.requested) + 1}"
        self.requested.append(
            {
                "agent_name": agent_name,
                "definition": definition,
                "output_tool": output_tool,
                "input_payload": input_payload,
                "causation_id": causation_id,
            }
        )
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="pending")
        return run_id

    async def find_chain_run(self, *, chain_id: str, agent_name: str):  # pragma: no cover
        del chain_id, agent_name
        return None

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
        return self._runs.get(run_id)

    def complete(
        self,
        run_id: str,
        *,
        status: str = "completed",
        messages: list[dict[str, Any]] | None = None,
        annotations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or [], annotations=annotations
        )


def _tool_call(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}
    ]


def _evidence_call(*urls: str) -> list[dict[str, Any]]:
    return _tool_call(
        CHANNEL_EVIDENCE_TOOL_NAME,
        {"evidence": [{"url": url, "note": f"what {url} shows about conversion"} for url in urls]},
    )


def _channel(channel_key: str, *urls: str, rank: int = 1) -> dict[str, Any]:
    return {
        "channel_key": channel_key,
        "rank": rank,
        "rationale": "Where this segment demonstrably converts.",
        "sources": [{"url": url, "note": "n"} for url in urls],
    }


def _plan_call(*channels: dict[str, Any]) -> list[dict[str, Any]]:
    return _tool_call(CHANNEL_PLAN_TOOL_NAME, {"channels": list(channels)})


def _plan_input() -> dict[str, Any]:
    return pipeline.channel_plan_input(
        positioning_body=_POSITIONING_BODY, taxonomy=_TAXONOMY_ROWS, campaign_motion="paid"
    )


async def _start_and_ground(
    gateway: _FakeGateway,
    *,
    annotations: list[dict[str, Any]] | None,
    evidence_urls: tuple[str, ...] = (_RETRIEVED_URL, _SECOND_RETRIEVED_URL),
    status: str = "completed",
) -> tuple[str, str]:
    """Start the stage and complete its grounding run."""
    causation_id, evidence_run_id = await pipeline.start_channel_evidence(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )
    gateway.complete(
        evidence_run_id,
        status=status,
        messages=_evidence_call(*evidence_urls),
        annotations=annotations,
    )
    return causation_id, evidence_run_id


# ── 1. Two runs, and only the first one grounds ──────────────────────────────


def test_the_grounding_run_is_the_one_on_the_observed_online_route() -> None:
    """#136 leaves exactly one route in this estate whose `:online` grounding
    has been watched to work. The two-step does not move it — it points it at
    the run that actually retrieves."""
    assert DEFAULT_CHANNEL_EVIDENCE_MODEL == "anthropic/claude-sonnet-4:online"


def test_the_planning_run_does_not_retrieve_at_all() -> None:
    """Handed the evidence, step two has nothing to search for. `:online` bills
    per result, so a second retrieval would pay twice for the same question —
    and would inject a SECOND unenumerated set into the context, which is the
    exact condition this two-step exists to remove."""
    assert ":online" not in DEFAULT_CHANNEL_PLAN_MODEL


@pytest.mark.asyncio
async def test_starting_the_stage_starts_the_grounding_run_not_the_plan() -> None:
    gateway = _FakeGateway()
    causation_id, _ = await _start_and_ground(gateway, annotations=_ANNOTATIONS)

    (requested,) = gateway.requested
    assert requested["agent_name"] == CHANNEL_EVIDENCE_AGENT_NAME
    assert requested["causation_id"] == causation_id
    assert next(iter(requested["input_payload"])) == "search_query"


@pytest.mark.asyncio
async def test_advancing_a_finished_grounding_run_starts_the_plan_run() -> None:
    """On the same causation chain — research already fans out and joins on
    `causation_id`, and per-campaign spend is assembled from it."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)

    advance = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )

    assert advance.plan is None
    assert advance.started_plan_run_id == "run-2"
    plan_request = gateway.requested[1]
    assert plan_request["agent_name"] == CHANNEL_PLAN_AGENT_NAME
    assert plan_request["causation_id"] == causation_id
    assert ":online" not in plan_request["definition"]["model"]


@pytest.mark.asyncio
async def test_the_plan_run_is_started_once_however_often_the_stage_is_polled() -> None:
    """Reading is what advances this pipeline, so "the grounding run is
    terminal" is true on every poll. Passing the started run's id back is what
    makes the transition happen once — the same reason the live breadth line
    was logged five times in eight seconds."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)

    first = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )
    again = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        plan_run_id=first.started_plan_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )

    assert again.started_plan_run_id is None
    assert again.plan is None  # the plan run has not finished yet
    assert len(gateway.requested) == 2


# ── 2. What step two is handed, and where it comes from ─────────────────────


@pytest.mark.asyncio
async def test_step_two_is_handed_the_retrieved_urls_as_an_enumerated_set() -> None:
    """The whole point of the two-step. The positioning's sources reach the
    model as structured objects it can see and cite; until now its own
    retrieval reached it only as injected context, and it cited the list it
    could see. `retrieved_evidence` is that list, and it leads the payload for
    the same reason `search_query` leads the grounding run's."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)

    await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )

    payload = gateway.requested[1]["input_payload"]
    assert next(iter(payload)) == "retrieved_evidence"
    assert [entry["url"] for entry in payload["retrieved_evidence"]] == [
        _RETRIEVED_URL,
        _SECOND_RETRIEVED_URL,
    ]
    # ...carrying what the runtime recorded about the page AND what step one
    # read in it, so a rationale can be written from the page rather than from
    # the URL string.
    first = payload["retrieved_evidence"][0]
    assert first["title"] == "CPA benchmarks 2026"
    assert _RETRIEVED_URL in first["what_it_says"]
    # ...and the positioning still travels, because a plan may legitimately
    # lean on it — it simply may not lean on it alone.
    assert payload["positioning"] == _POSITIONING_BODY
    assert payload["campaign_motion"] == "paid"
    assert "search_query" not in payload  # nothing to search: it does not retrieve


@pytest.mark.asyncio
async def test_a_url_only_the_model_claims_to_have_read_is_not_handed_on() -> None:
    """The trust boundary, and the reason the URL set is taken from
    `annotations` rather than from step one's tool call. If the model's own
    account were the authority, a URL invented in step one would come back in
    step two as "evidence this run retrieved" and satisfy the very guard that
    exists to catch it."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(
        gateway,
        annotations=_ANNOTATIONS,
        evidence_urls=(_RETRIEVED_URL, _UNRETRIEVED_URL),
    )

    await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )

    handed = gateway.requested[1]["input_payload"]["retrieved_evidence"]
    urls = [entry["url"] for entry in handed]
    assert _UNRETRIEVED_URL not in urls
    assert urls == [_RETRIEVED_URL, _SECOND_RETRIEVED_URL]
    # The page the runtime recorded but step one wrote no note for is still
    # handed over — it was retrieved, so it is citable; it simply arrives
    # without a reading.
    assert handed[1]["what_it_says"] == ""


@pytest.mark.asyncio
async def test_a_grounding_run_that_retrieved_nothing_hands_over_nothing() -> None:
    """`annotations == []` is the one state that genuinely means retrieval
    returned nothing, and it must not be dressed up as an empty-because-unknown
    set. Step two is handed an empty list, and every recommendation it then
    makes is ungrounded by construction."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=[])

    await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )

    assert gateway.requested[1]["input_payload"]["retrieved_evidence"] == []


# ── 3. The guard reads the run that retrieved (issue #90's lesson) ──────────


@pytest.mark.asyncio
async def test_the_plan_is_judged_against_the_grounding_runs_retrieval() -> None:
    """Step two is not an `:online` run, so its own `annotations` can only ever
    be "not a grounded run" — the state that skips both evidence checks. The
    subject has to be the run that actually retrieved, exactly as
    `_aggregate_research_annotations` does for synthesis (#90). Getting this
    wrong would switch off `UngroundedRecommendationError` without deleting a
    line of it."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)
    first = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )
    # The failure the live run produced: a plan resting entirely on the
    # positioning's carried-over sources, from a run whose own annotations are
    # `None` because it never grounded.
    gateway.complete(
        first.started_plan_run_id or "",
        messages=_plan_call(_channel("google_search_paid", _PARENT_URL)),
        annotations=None,
    )

    with pytest.raises(pipeline.UngroundedRecommendationError) as excinfo:
        await pipeline.advance_channel_plan(
            gateway,
            evidence_run_id=evidence_run_id,
            plan_run_id=first.started_plan_run_id,
            taxonomy=_TAXONOMY,
            plan_input=_plan_input(),
            causation_id=causation_id,
            allowed_source_urls=[_PARENT_URL],
        )

    assert "google_search_paid" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_plan_citing_what_the_grounding_run_retrieved_is_accepted() -> None:
    """The path that has never yet completed on a live campaign, and the one
    the whole change is for."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)
    first = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
    )
    gateway.complete(
        first.started_plan_run_id or "",
        messages=_plan_call(_channel("google_search_paid", _PARENT_URL, _RETRIEVED_URL)),
        annotations=None,
    )

    advance = await pipeline.advance_channel_plan(
        gateway,
        evidence_run_id=evidence_run_id,
        plan_run_id=first.started_plan_run_id,
        taxonomy=_TAXONOMY,
        plan_input=_plan_input(),
        causation_id=causation_id,
        allowed_source_urls=[_PARENT_URL],
    )

    assert advance.plan is not None
    assert advance.plan.channels[0].channel_key == "google_search_paid"
    assert advance.started_plan_run_id is None


@pytest.mark.asyncio
async def test_a_failed_grounding_run_fails_the_stage_without_starting_a_plan() -> None:
    """Paying for a planning run over evidence that never arrived is the one
    thing worse than failing here."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(
        gateway, annotations=_ANNOTATIONS, status="failed"
    )

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_channel_plan(
            gateway,
            evidence_run_id=evidence_run_id,
            taxonomy=_TAXONOMY,
            plan_input=_plan_input(),
            causation_id=causation_id,
        )

    assert len(gateway.requested) == 1


@pytest.mark.asyncio
async def test_a_grounding_run_that_never_called_its_tool_is_a_malformed_run() -> None:
    """Step one's notes are what make step two able to write a rationale from
    the page rather than from the URL. A run that returned prose instead is the
    #159 shape, and it is reported as such rather than silently handing over
    URLs with no readings."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await pipeline.start_channel_evidence(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )
    gateway.complete(evidence_run_id, messages=[], annotations=_ANNOTATIONS)

    with pytest.raises(pipeline.MalformedOutputError) as excinfo:
        await pipeline.advance_channel_plan(
            gateway,
            evidence_run_id=evidence_run_id,
            taxonomy=_TAXONOMY,
            plan_input=_plan_input(),
            causation_id=causation_id,
        )

    assert CHANNEL_EVIDENCE_TOOL_NAME in str(excinfo.value)


# ── 4. The breadth line is measured once per run, not once per poll ─────────


def _breadth_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith("channel plan retrieval breadth")
    ]


@pytest.mark.asyncio
async def test_the_breadth_line_is_emitted_once_per_run_not_once_per_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Measured on the live run: the same measurement at 13:26:22, :24, :26,
    :28 and :30 — once per poll of the advance route, not once per run.
    Harmless to the campaign, and actively misleading to anyone counting runs
    from the logs, which is the one thing this instrument exists to be read
    for."""
    gateway = _FakeGateway()
    causation_id, evidence_run_id = await _start_and_ground(gateway, annotations=_ANNOTATIONS)

    with caplog.at_level("INFO"):
        for _ in range(3):
            await pipeline.advance_channel_plan(
                gateway,
                evidence_run_id=evidence_run_id,
                taxonomy=_TAXONOMY,
                plan_input=_plan_input(),
                causation_id=causation_id,
            )

    (record,) = _breadth_records(caplog)
    assert "2 distinct URLs across 1 grounded run(s)" in record.getMessage()
    assert record.causation_id == causation_id
