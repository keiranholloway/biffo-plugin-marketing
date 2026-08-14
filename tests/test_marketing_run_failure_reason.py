"""A failed agent run must tell an operator WHY, not only that it failed
(issue #164).

## The defect this closes

On 2026-08-14 the positioning stage began failing on a fresh campaign in
**0.048 seconds**. The runtime logged no `ERROR` — it had not failed itself, it
had faithfully reported someone else's failure — and the plugin rendered:

    The positioning run did not complete successfully. (502)

That sentence is true of a malformed definition, an unregistered tool, a
wall-clock timeout, a provider outage and an exhausted OpenRouter balance
alike. So the diagnosis went to CloudWatch, then to the three PRs that had
merged since the last success, then towards a bisect — while Core had held the
answer on the run row the whole time:

    LLM call failed on turn 1: OpenRouter returned 402: This request requires
    more credits, or fewer max_tokens. You requested up to 65536 tokens, but
    can only afford 57975.

Nothing in this plugin read `AgentRunResponse.error`. `AgentRunView` had no
field for it, so it could not have.

## What is guarded here, and what is NOT

**Is:** the reason survives the trip from Core's JSON onto the view and into
the message an operator is shown, for **every** stage — swept from the source
rather than listed, because "adopted at some call sites and not others" is this
repo's most-repeated defect class (`test_marketing_helper_adoption_sweep.py`'s
docstring; #72, #49/#111).

**Is not:** the 402 itself. No unit test can produce a real provider's
credit refusal — that needs a real run against a real account with a real
balance. What a test *can* guarantee is that when it happens again, the person
looking at the screen is told which of the five candidate causes it was. That
is the difference between an afternoon and a minute, and it is the whole of
what ships here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Literal

import pytest

from marketing import admin_app, pipeline

#: The real thing, verbatim from run `64ee7319-c623-4137-89db-ac7017260e2c` on
#: tabsii dev (truncated by the runtime before it was stored, as every provider
#: body is). Used rather than `"boom"` so the assertions below are about the
#: string an operator will actually meet.
_REAL_402 = (
    "LLM call failed on turn 1: OpenRouter returned 402: "
    '{"error":{"message":"This request requires more credits, or fewer max_tokens. '
    "You requested up to 65536 tokens, but can only afford 57975. To increase, visit "
    'https://openrouter.ai/settings/credits and add more credits","code":402}}'
)

_PIPELINE_SOURCE = Path(pipeline.__file__)


class _FailingGateway:
    """Every run this gateway is asked about is terminal, failed, and carries
    `error`. Enough for the advancers below, which all read the same seam."""

    def __init__(self, error: str | None = _REAL_402) -> None:
        self._error = error
        self.requested: list[str] = []

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        del definition, output_tool, input_payload, causation_id
        self.requested.append(agent_name)
        return "run-started"

    async def find_chain_run(
        self, *, chain_id: str, agent_name: str
    ) -> pipeline.AgentRunView | None:
        del chain_id
        if agent_name != pipeline.RESEARCH_SYNTHESIS_AGENT_NAME:
            return None
        return self._view("synthesis-run")

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
        return self._view(run_id)

    def _view(self, run_id: str) -> pipeline.AgentRunView:
        return pipeline.AgentRunView(id=run_id, status="failed", messages=[], error=self._error)


class _NoSynthesisGateway(_FailingGateway):
    """Research's other terminal path: the engine never fired a synthesis run
    because every research run had already failed (#164 touches this one too —
    two runs that died differently are two diagnoses)."""

    async def find_chain_run(
        self, *, chain_id: str, agent_name: str
    ) -> pipeline.AgentRunView | None:
        del chain_id, agent_name
        return None


# ── Every stage surfaces the reason ───────────────────────────────────────────
#
# One case per advancer, named by the stage, so a failure says which surface
# regressed. The sweep further down is what stops a *seventh* stage being added
# without one.


async def _advance_research(gateway: Any) -> None:
    await pipeline.advance_research(
        gateway, chain_id="chain-1", research_run_ids=["research-1", "research-2"]
    )


async def _advance_positioning(gateway: Any) -> None:
    await pipeline.advance_positioning(gateway, run_id="positioning-1")


async def _advance_channel_evidence(gateway: Any) -> None:
    taxonomy: dict[str, Literal["organic", "paid"]] = {"linkedin_organic": "organic"}
    await pipeline.advance_channel_plan(gateway, evidence_run_id="evidence-1", taxonomy=taxonomy)


async def _advance_copy(gateway: Any) -> None:
    channels: dict[str, Literal["organic", "paid"]] = {"linkedin_organic": "organic"}
    await pipeline.advance_copy(gateway, run_id="copy-1", channel_plan_channels=channels)


_STAGES = {
    "research-synthesis": _advance_research,
    "positioning": _advance_positioning,
    "channel-plan evidence": _advance_channel_evidence,
    "copy": _advance_copy,
}


@pytest.mark.parametrize("stage", sorted(_STAGES))
@pytest.mark.asyncio
async def test_every_stage_reports_the_reason_core_recorded(stage: str) -> None:
    """The provider's own words reach the exception message — the sentence
    that would have ended #164's diagnosis on sight."""
    with pytest.raises(pipeline.RunNotSucceededError) as raised:
        await _STAGES[stage](_FailingGateway())

    message = str(raised.value)
    assert "402" in message
    assert "more credits" in message
    # The stage is still named: the reason is added to the existing sentence,
    # never substituted for it — an operator needs both "which stage" and "why".
    assert "did not complete successfully" in message or "research agent failed" in message


@pytest.mark.asyncio
async def test_research_reports_the_reason_when_no_synthesis_run_ever_fired() -> None:
    """Research's second terminal path: no synthesis run exists because every
    research run failed. The reasons come off the research runs themselves."""
    with pytest.raises(pipeline.RunNotSucceededError) as raised:
        await _advance_research(_NoSynthesisGateway())

    assert "402" in str(raised.value)


@pytest.mark.asyncio
async def test_the_channel_plan_run_reports_its_own_reason() -> None:
    """The planning run, not the grounding run — the only advancer with two
    runs of its own, and the one a `*_view` mix-up would silently skip."""

    class _EvidenceSucceeded(_FailingGateway):
        async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
            if run_id == "evidence-1":
                return pipeline.AgentRunView(
                    id=run_id,
                    status="completed",
                    messages=[],
                    annotations=[{"type": "url_citation", "url": "https://example.com/a"}],
                )
            return self._view(run_id)

    taxonomy: dict[str, Literal["organic", "paid"]] = {"linkedin_organic": "organic"}
    with pytest.raises(pipeline.RunNotSucceededError) as raised:
        await pipeline.advance_channel_plan(
            _EvidenceSucceeded(),
            evidence_run_id="evidence-1",
            plan_run_id="plan-1",
            taxonomy=taxonomy,
        )

    assert "402" in str(raised.value)


# ── "No reason recorded" is said out loud ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_with_no_recorded_error_says_so_rather_than_going_quiet() -> None:
    """`None` means "Core recorded nothing", which is a different problem from
    "nobody looked" and must not read like the old bare sentence — otherwise
    the next reader cannot tell whether this fix is even deployed."""
    with pytest.raises(pipeline.RunNotSucceededError) as raised:
        await pipeline.advance_positioning(_FailingGateway(error=None), run_id="p-1")

    assert "Core recorded no reason" in str(raised.value)


def test_the_reason_is_bounded_and_collapsed_onto_one_line() -> None:
    """A provider that answers with an HTML error page must not fill the
    operator's screen, and a multi-line body must not break the message."""
    built = pipeline.run_not_succeeded(
        "The positioning run did not complete successfully.",
        pipeline.AgentRunView(id="r", status="failed", error="a\nb   c" + "x" * 5000),
    )

    message = str(built)
    assert "\n" not in message
    assert "a b cxxx" in message
    assert len(message) < 200 + pipeline.RUN_ERROR_EXCERPT_CHARS
    assert message.endswith("…")


def test_two_runs_that_failed_identically_report_one_reason() -> None:
    """Both research angles killed by the same exhausted balance is one fact.
    Repeating it twice is how a useful message becomes an ignored one."""
    views = [
        pipeline.AgentRunView(id="a", status="failed", error=_REAL_402),
        pipeline.AgentRunView(id="b", status="failed", error=_REAL_402),
    ]

    message = str(pipeline.run_not_succeeded("Every research agent failed.", *views))

    assert message.count("can only afford 57975") == 1


def test_two_runs_that_failed_differently_report_both_reasons() -> None:
    """…and two different deaths are two diagnoses. Collapsing them would hide
    the interesting one behind the boring one."""
    views = [
        pipeline.AgentRunView(id="a", status="failed", error=_REAL_402),
        pipeline.AgentRunView(
            id="b", status="failed", error="Wall-clock limit of 240s reached before turn 1"
        ),
    ]

    message = str(pipeline.run_not_succeeded("Every research agent failed.", *views))

    assert "402" in message
    assert "Wall-clock limit" in message


# ── The seam: Core's JSON -> the view ─────────────────────────────────────────


class _FakeClient:
    """`_CoreAgentGateway` only ever calls `.get`/`.post` on its client —
    see `test_marketing_agent_gateway.py`'s docstring for why a duck type is
    enough here."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        del path, params
        return self._response


@pytest.mark.asyncio
async def test_get_agent_run_carries_cores_error_onto_the_view() -> None:
    """The field the whole fix rests on. Inert unless it survives this hop —
    exactly the failure mode `annotations` and `definition_snapshot` each have
    their own test for in `test_marketing_agent_gateway.py`."""
    run = {"id": "run-1", "status": "failed", "messages": [], "error": _REAL_402}
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.error == _REAL_402


@pytest.mark.asyncio
async def test_get_agent_run_defaults_a_missing_error_to_none() -> None:
    """Absent stays `None` ("not known"), never `""` — `run_not_succeeded`
    reports "Core recorded no reason" only when there genuinely was none."""
    run = {"id": "run-1", "status": "completed", "messages": []}
    gateway = admin_app._CoreAgentGateway(_FakeClient(run))  # type: ignore[arg-type]

    view = await gateway.get_agent_run(run_id="run-1")

    assert view is not None
    assert view.error is None


# ── The sweep: no stage may bypass the helper ─────────────────────────────────


def test_no_stage_constructs_run_not_succeeded_error_directly() -> None:
    """`RunNotSucceededError(...)` is built in exactly one place.

    Derived from the source, not from a list of stages, for the reason
    `test_marketing_helper_adoption_sweep.py` exists: a helper adopted at some
    call sites and not others leaves the defect in place behind a closed issue.
    A seventh stage added next month gets this for free — or fails here.
    """
    tree = ast.parse(_PIPELINE_SOURCE.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunNotSucceededError"
    ]
    inside_helper = {
        node.lineno
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef) and func.name == "run_not_succeeded"
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
    }

    bypassed = sorted(set(offenders) - inside_helper)
    assert not bypassed, (
        f"{_PIPELINE_SOURCE.name} constructs RunNotSucceededError directly at line(s) "
        f"{bypassed}. Build it with `run_not_succeeded(message, view)` so the reason "
        "Core recorded reaches the operator (issue #164)."
    )
