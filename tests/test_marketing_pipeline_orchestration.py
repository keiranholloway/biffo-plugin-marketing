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
    COPY_AGENT_NAME,
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
        self,
        run_id: str,
        *,
        status: str = "completed",
        messages: list | None = None,
        annotations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or [], annotations=annotations
        )

    def fire_chain_run(
        self,
        *,
        chain_id: str,
        agent_name: str,
        status: str = "completed",
        messages: list | None = None,
        annotations: list[dict[str, Any]] | None = None,
    ) -> str:
        """Simulate the engine's `agent_fan_in` firing a joining agent."""
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or [], annotations=annotations
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


def _empty_synthesis_call() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_research_synthesis",
                        "arguments": {"summary": "Nothing usable.", "findings": []},
                    }
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_advance_research_reads_annotations_from_research_runs_not_synthesis_run() -> None:
    """Issue #90: the synthesis run is never `:online`
    (`DEFAULT_SYNTHESIS_MODEL` has no `:online` suffix) — only the two
    research runs actually retrieve. The guard must be grounded in THEIR
    annotations, not the synthesis run's own.

    Proven adversarially: the synthesis run here is given `annotations=[]`
    (the value the old, buggy code read) while the research runs are given
    real, non-empty annotations. If `advance_research` were still reading the
    synthesis run's own annotations — the pre-#90-fix code, and exactly the
    shape issue #90 was filed against — this would see `[]` and raise the
    unconditional "fetched zero URLs" message. It must not: the research
    runs retrieved, so the failure must read as a transcription failure."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(
            run_id,
            status="completed",
            annotations=[{"type": "url_citation", "url": "https://example.com/z", "title": "Z"}],
        )
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_empty_synthesis_call(),
        annotations=[],  # what the synthesis run itself reports — must be ignored
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    detail = str(excinfo.value)
    assert "fetched zero URLs" not in detail
    assert "transcription failure" in detail


@pytest.mark.asyncio
async def test_advance_research_logs_how_broad_the_retrieval_actually_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #101: the evidence base being thin and duplicated across the two
    angles was invisible — nothing failed, and the numbers existed only in
    provider annotations somebody had to go and read. Every research chain
    now states its own breadth, and this asserts it is a *report*: the run
    still succeeds, and the synthesis still comes back."""
    logged: list[dict[str, Any]] = []

    class _CapturingLogger:
        def info(self, *args: Any, **kwargs: Any) -> None:
            logged.append(kwargs.get("extra") or {})

    monkeypatch.setattr(pipeline, "logger", _CapturingLogger())

    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    shared = {"type": "url_citation", "url": "https://example.com/", "title": "Root"}
    gateway.complete(run_ids[0], status="completed", annotations=[shared])
    gateway.complete(
        run_ids[1],
        status="completed",
        annotations=[shared, {"type": "url_citation", "url": "https://example.com/pricing"}],
    )
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_findings_call("audience"),
    )

    result = await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    assert isinstance(result, pipeline.ResearchSynthesis)  # a report, not a gate
    assert logged == [
        {
            "causation_id": chain_id,
            "research_runs_measured": 2,
            "research_distinct_urls": 2,
            "research_shared_urls": 1,
            "research_deep_pages": 1,
            "research_site_roots": 1,
        }
    ]


# ── The breadth line reaches a real log record, on every terminal path ────────
#
# The tests below use `caplog` rather than a substituted `logger`, because the
# thing being guarded is that an operator can *find this in CloudWatch*. A
# monkeypatched logger proves `_log_evidence_profile` was called; it cannot
# prove the powertools `Logger` actually emitted a record, at INFO, carrying
# the structured keys somebody has been told to filter on. (The autouse
# `_reenable_disabled_loggers` fixture in `conftest.py` is what makes `caplog`
# see anything at all once this plugin is vendored — see commit 657de60.)


BREADTH_PREFIX = "research retrieval breadth"


def _breadth_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.getMessage().startswith(BREADTH_PREFIX)]


async def _chain_with_retrieval(gateway: _FakeGateway) -> tuple[str, list[str]]:
    """Two research runs that both succeeded and both retrieved: one shared
    site root, one deep page seen by a single angle."""
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    shared = {"type": "url_citation", "url": "https://example.com/", "title": "Root"}
    gateway.complete(run_ids[0], status="completed", annotations=[shared])
    gateway.complete(
        run_ids[1],
        status="completed",
        annotations=[shared, {"type": "url_citation", "url": "https://example.com/pricing"}],
    )
    return chain_id, run_ids


@pytest.mark.asyncio
async def test_breadth_line_reaches_the_real_log_on_the_success_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = _FakeGateway()
    chain_id, run_ids = await _chain_with_retrieval(gateway)
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_findings_call("audience"),
    )

    with caplog.at_level("INFO"):
        result = await pipeline.advance_research(
            gateway, chain_id=chain_id, research_run_ids=run_ids
        )

    assert isinstance(result, pipeline.ResearchSynthesis)
    (record,) = _breadth_records(caplog)  # exactly one, never doubled
    assert "2 distinct URLs across 2 grounded run(s)" in record.getMessage()
    assert record.causation_id == chain_id
    assert record.research_runs_measured == 2
    assert record.research_distinct_urls == 2
    assert record.research_shared_urls == 1
    assert record.research_deep_pages == 1
    assert record.research_site_roots == 1


@pytest.mark.asyncio
async def test_breadth_line_is_logged_when_the_synthesis_run_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure path an operator most needs this on. "The research was
    thin" and "the synthesis failed" produce the same dead artefact, and the
    breadth line is the only thing that tells them apart — so logging it only
    on success withholds it exactly when it is being asked for."""
    gateway = _FakeGateway()
    chain_id, run_ids = await _chain_with_retrieval(gateway)
    gateway.fire_chain_run(
        chain_id=chain_id, agent_name=RESEARCH_SYNTHESIS_AGENT_NAME, status="failed"
    )

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    (record,) = _breadth_records(caplog)
    assert "2 distinct URLs across 2 grounded run(s)" in record.getMessage()
    assert record.causation_id == chain_id
    assert record.research_distinct_urls == 2
    assert record.research_site_roots == 1


@pytest.mark.asyncio
async def test_breadth_line_is_logged_when_every_research_run_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No synthesis run was ever fired, so the success path is never reached —
    and this is the chain where "what did retrieval actually return?" is the
    whole question. A failed run can still have retrieved before it died."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    gateway.complete(
        run_ids[0],
        status="failed",
        annotations=[{"type": "url_citation", "url": "https://example.com/pricing"}],
    )
    gateway.complete(run_ids[1], status="failed", annotations=[])

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    (record,) = _breadth_records(caplog)
    assert "1 distinct URLs across 2 grounded run(s)" in record.getMessage()
    assert record.causation_id == chain_id
    assert record.research_runs_measured == 2
    assert record.research_distinct_urls == 1


@pytest.mark.asyncio
async def test_breadth_line_says_unmeasured_rather_than_reporting_a_false_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`annotations is None` means "we do not know what this run retrieved",
    not "it retrieved nothing" — the tri-state `evidence_profile` is careful
    to keep. Logging `0 distinct URLs across 0 grounded run(s)` here would
    throw that distinction away at the only point a human reads it, and would
    read as a measured, catastrophic zero."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="failed", annotations=None)

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    (record,) = _breadth_records(caplog)
    message = record.getMessage()
    assert "not measured" in message
    assert "distinct URLs across" not in message  # not a measurement
    assert record.causation_id == chain_id
    assert record.research_runs_measured == 0
    assert record.research_distinct_urls is None  # unknown, not zero


@pytest.mark.asyncio
async def test_breadth_logging_never_replaces_the_real_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A report that throws while reporting on a failure turns a diagnosable
    error into a spurious one. Whatever the gateway does, the caller must
    still see `RunNotSucceededError`."""

    class _BrokenGateway(_FakeGateway):
        async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
            raise RuntimeError("run view unavailable")

    gateway = _BrokenGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    gateway.fire_chain_run(
        chain_id=chain_id, agent_name=RESEARCH_SYNTHESIS_AGENT_NAME, status="failed"
    )

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)


@pytest.mark.asyncio
async def test_no_breadth_line_while_the_chain_is_still_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`advance_research` is polled. Logging breadth on a non-terminal chain
    would emit the line once per poll, so the number an operator finds in
    CloudWatch would be whichever partial snapshot they scrolled to."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})

    with caplog.at_level("INFO"):
        assert (
            await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)
            is None
        )

    assert _breadth_records(caplog) == []


@pytest.mark.asyncio
async def test_advance_research_unions_annotations_when_only_one_research_angle_retrieved() -> None:
    """Decision (issue #90): a chain where one research angle retrieved and
    the other found nothing is not distinguished from "both retrieved" — the
    aggregate is a union, and any non-empty result reads as "retrieval
    succeeded somewhere in the chain", which is what the message already
    says (a total count, not a per-angle claim)."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    gateway.complete(run_ids[0], status="completed", annotations=[])
    gateway.complete(
        run_ids[1],
        status="completed",
        annotations=[{"type": "url_citation", "url": "https://example.com/only-one", "title": "Z"}],
    )
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_empty_synthesis_call(),
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    detail = str(excinfo.value)
    assert "transcription failure" in detail
    assert "1 source" in detail


@pytest.mark.asyncio
async def test_advance_research_reports_genuine_zero_only_when_every_research_run_is_empty() -> (
    None
):
    """The base "fetched zero URLs" message is only honest when EVERY
    research run resolved and reported `[]` — the one case in the aggregate
    that actually matches what the message asserts."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    for run_id in run_ids:
        gateway.complete(run_id, status="completed", annotations=[])
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_empty_synthesis_call(),
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    detail = str(excinfo.value)
    assert detail == (
        "The research run fetched zero URLs. Nothing was found to cite, so no "
        "artefact was produced — try running research again."
    )


@pytest.mark.asyncio
async def test_advance_research_treats_mixed_none_and_empty_chain_as_unknown_not_proven_zero() -> (
    None
):
    """Decision (issue #90): if one research run predates the annotations
    column (or was otherwise never graded) and reports `None`, while the
    other reports a genuine `[]`, the aggregate must not claim a proven
    zero-URL retrieval — part of the chain's status is simply not known."""
    gateway = _FakeGateway()
    chain_id, run_ids = await pipeline.start_research(gateway, brief={})
    gateway.complete(run_ids[0], status="completed", annotations=None)
    gateway.complete(run_ids[1], status="completed", annotations=[])
    gateway.fire_chain_run(
        chain_id=chain_id,
        agent_name=RESEARCH_SYNTHESIS_AGENT_NAME,
        messages=_empty_synthesis_call(),
        # Deliberately the opposite of the correct aggregate (`None`): if
        # `advance_research` were still reading the synthesis run's own
        # annotations instead of the research runs', this `[]` would produce
        # the bare "fetched zero URLs" message with no disclaimer — the wrong
        # answer, and a different string from the one asserted below.
        annotations=[],
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_research(gateway, chain_id=chain_id, research_run_ids=run_ids)

    detail = str(excinfo.value)
    assert detail == (
        "The research run fetched zero URLs. Nothing was found to cite, so no "
        "artefact was produced — try running research again. (This run predates "
        "citation tracking, or was not a grounded run, so whether retrieval itself "
        "found anything is not known — do not read this as a proven zero-URL "
        "retrieval.)"
    )


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


@pytest.mark.asyncio
async def test_advance_positioning_propagates_the_runs_annotations_into_the_error() -> None:
    """Issue #82, positioning half: a run whose retrieval genuinely found
    nothing (`annotations=[]`) must keep the "retrying will not help" reading
    — proven through `advance_positioning`, not the extractor directly."""
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
                            "arguments": {"segments": [], "pillars": [], "ctas": []},
                        }
                    }
                ],
            }
        ],
        annotations=[],
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_positioning(gateway, run_id=run_id)

    assert str(excinfo.value) == (
        "The positioning run cited nothing from the approved research. Nothing "
        "was produced — try running positioning again."
    )


# ── start_channel_plan / advance_channel_plan (M4) ───────────────────────────


@pytest.mark.asyncio
async def test_start_channel_plan_requests_one_run_carrying_the_positioning_body() -> None:
    gateway = _FakeGateway()
    positioning_body = {"segments": [], "pillars": [], "ctas": []}
    taxonomy = [{"channel_key": "instagram_organic", "label": "Instagram", "motion": "organic"}]

    causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body=positioning_body, taxonomy=taxonomy, campaign_motion="both"
    )

    assert len(gateway.requested) == 1
    requested = gateway.requested[0]
    assert requested.agent_name == CHANNEL_PLAN_AGENT_NAME
    assert requested.causation_id == causation_id
    assert requested.input_payload == {
        "positioning": positioning_body,
        "channel_taxonomy": taxonomy,
        # #67: told the campaign's motion as well as shown a taxonomy already
        # narrowed to it — see `start_channel_plan` for why the telling is an
        # efficiency and the narrowing is the enforcement.
        "campaign_motion": "both",
    }
    assert run_id  # a real id was returned


@pytest.mark.asyncio
async def test_advance_channel_plan_returns_none_while_running() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body={}, taxonomy=[], campaign_motion="both"
    )

    result = await pipeline.advance_channel_plan(gateway, run_id=run_id, taxonomy={})

    assert result is None


@pytest.mark.asyncio
async def test_advance_channel_plan_returns_the_plan_once_succeeded() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body={}, taxonomy=[], campaign_motion="both"
    )
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
                                        "channel_key": "instagram_organic",
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

    result = await pipeline.advance_channel_plan(
        gateway, run_id=run_id, taxonomy={"instagram_organic": "organic"}
    )

    assert isinstance(result, pipeline.ChannelPlan)
    assert result.channels[0].channel_key == "instagram_organic"
    assert result.channels[0].motion == "organic"  # derived from taxonomy, not the agent


@pytest.mark.asyncio
async def test_advance_channel_plan_raises_when_the_run_failed() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body={}, taxonomy=[], campaign_motion="both"
    )
    gateway.complete(run_id, status="failed")

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_channel_plan(gateway, run_id=run_id, taxonomy={})


@pytest.mark.asyncio
async def test_advance_channel_plan_propagates_null_annotations_into_the_error() -> None:
    """Issue #82, channel-plan half: `annotations=None` (a pre-upgrade run, or
    a non-`:online` model) must NOT be reported as a confirmed zero-URL
    retrieval — proven through `advance_channel_plan`."""
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body={}, taxonomy=[], campaign_motion="both"
    )
    gateway.complete(
        run_id,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "submit_channel_plan", "arguments": {"channels": []}}}
                ],
            }
        ],
        annotations=None,
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_channel_plan(gateway, run_id=run_id, taxonomy={})

    assert "not known" in str(excinfo.value)


# ── start_copy / advance_copy (M5, issue #4) ─────────────────────────────────


@pytest.mark.asyncio
async def test_start_copy_requests_one_run_carrying_both_upstream_bodies() -> None:
    gateway = _FakeGateway()
    positioning_body = {"segments": [], "pillars": [], "ctas": []}
    channel_plan_body = {"channels": []}

    causation_id, run_id = await pipeline.start_copy(
        gateway, positioning_body=positioning_body, channel_plan_body=channel_plan_body
    )

    assert len(gateway.requested) == 1
    requested = gateway.requested[0]
    assert requested.agent_name == COPY_AGENT_NAME
    assert requested.causation_id == causation_id
    assert requested.input_payload == {
        "positioning": positioning_body,
        "channel_plan": channel_plan_body,
    }
    assert run_id  # a real id was returned


@pytest.mark.asyncio
async def test_advance_copy_returns_none_while_running() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_copy(
        gateway, positioning_body={}, channel_plan_body={}
    )

    result = await pipeline.advance_copy(gateway, run_id=run_id, channel_plan_channels={})

    assert result is None


@pytest.mark.asyncio
async def test_advance_copy_returns_the_copy_once_succeeded() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_copy(
        gateway, positioning_body={}, channel_plan_body={}
    )
    gateway.complete(
        run_id,
        messages=[
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
                                        "headline": "h",
                                        "body": "b",
                                        "cta": "c",
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

    result = await pipeline.advance_copy(
        gateway, run_id=run_id, channel_plan_channels={"instagram_organic": "organic"}
    )

    assert isinstance(result, pipeline.CopySet)
    assert result.channels[0].channel_key == "instagram_organic"
    assert result.channels[0].motion == "organic"  # derived from the plan, not the agent


@pytest.mark.asyncio
async def test_advance_copy_raises_when_the_run_failed() -> None:
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_copy(
        gateway, positioning_body={}, channel_plan_body={}
    )
    gateway.complete(run_id, status="failed")

    with pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_copy(gateway, run_id=run_id, channel_plan_channels={})


@pytest.mark.asyncio
async def test_advance_copy_propagates_the_runs_annotations_into_the_error() -> None:
    """Issue #82, copy half: retrieval succeeding while the model's tool call
    cites nothing must read as transient — proven through `advance_copy`."""
    gateway = _FakeGateway()
    _causation_id, run_id = await pipeline.start_copy(
        gateway, positioning_body={}, channel_plan_body={}
    )
    gateway.complete(
        run_id,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "submit_copy", "arguments": {"channels": []}}}
                ],
            }
        ],
        annotations=[{"type": "url_citation", "url": "https://example.com/z", "title": "Z"}],
    )

    with pytest.raises(pipeline.NoCitationsError) as excinfo:
        await pipeline.advance_copy(gateway, run_id=run_id, channel_plan_channels={})

    assert "transcription failure" in str(excinfo.value)


# ── require_approved ──────────────────────────────────────────────────────────


def test_require_approved_passes_on_approved() -> None:
    pipeline.require_approved("approved", what="Research")  # must not raise


@pytest.mark.parametrize("status", ["pending", "proposed", "rejected", ""])
def test_require_approved_raises_on_anything_else(status: str) -> None:
    with pytest.raises(pipeline.ArtefactNotApprovedError):
        pipeline.require_approved(status, what="Research")
