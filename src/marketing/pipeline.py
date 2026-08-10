"""Research + positioning + channel-plan orchestration (M3 + M4) — fan-out/fan-in
over Core's agent-run seam, and the citation-enforcement guard that turns a run
with no evidence into a hard failure rather than a beautifully formatted
document.

Modelled directly on ``biffo-plugin-idea-scout``'s ``service.py`` (the
fan-out/fan-in shape, ``service.py:226-331``) and ``ports.py`` (the port it
runs behind, ``ports.py:107-128``). This module is deliberately smaller than
idea-scout's ``IdeaScoutService``: a campaign is tenant-admin data, not
founder-owned, so there is no ``owner_sub`` to thread through and no forwarded
user token to sign with — see ``admin_app.py`` for how the gateway below is
actually built (a plain ``SignedCoreClient``, ADR-0009/ADR-0021 §1a).

**The zero-citation guard is the milestone, not a detail.** Biffo's agent
``web_search`` is silently unavailable on dev (an empty Brave key), and an
agent given a tool it cannot use does not error — it fabricates. Structurally,
a ``ResearchFinding``/``Segment``/``MessagePillar``/``CallToAction``/
``ChannelRecommendation`` cannot be built with an empty ``sources`` list
(``definitions.py``'s ``Field(min_length=1)``) — but an agent can still
honestly report *zero* findings, or Core's transcript can carry no usable tool
call at all, and both of those are still "nothing to show an operator".
``extract_research_synthesis``, ``extract_positioning`` and
``extract_channel_plan`` catch that aggregate case: a run whose entire output
cites nothing raises :class:`NoCitationsError` rather than returning an
artefact body an admin route could propose. M4 (issue #3) is that same guard
applied one stage further down the chain, for the same reason stated there: a
channel recommendation with no evidence is the most confident-sounding
fabrication in the pipeline, because channel advice reads as generic wisdom
whether or not anyone researched it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .definitions import (
    CHANNEL_PLAN_AGENT_NAME,
    CHANNEL_PLAN_INSTRUCTIONS,
    CHANNEL_PLAN_TOOL_NAME,
    COPY_AGENT_NAME,
    COPY_INSTRUCTIONS,
    COPY_TOOL_NAME,
    DEFAULT_CHANNEL_PLAN_MODEL,
    DEFAULT_COPY_MODEL,
    DEFAULT_POSITIONING_MODEL,
    DEFAULT_RESEARCH_MODEL,
    DEFAULT_SYNTHESIS_MODEL,
    FINDINGS_TOOL_NAME,
    POSITIONING_AGENT_NAME,
    POSITIONING_INSTRUCTIONS,
    POSITIONING_TOOL_NAME,
    RESEARCH_AGENT_NAMES,
    RESEARCH_INSTRUCTIONS,
    RESEARCH_SYNTHESIS_AGENT_NAME,
    RESEARCH_SYNTHESIS_TOOL_NAME,
    ChannelPlan,
    CopySet,
    Positioning,
    ResearchFindingSet,
    ResearchSynthesis,
    Source,
    channel_plan_definition,
    channel_plan_tool_schema,
    copy_definition,
    copy_tool_schema,
    findings_tool_schema,
    positioning_definition,
    positioning_tool_schema,
    research_definition,
)


class PipelineError(Exception):
    """Base for errors the admin routes map to HTTP statuses."""


class NoCitationsError(PipelineError):
    """A research or positioning run produced content with zero citations.

    The milestone's guard. A research or positioning agent whose live web
    results (or whose grounding research) is unavailable does not error — it
    fabricates a plausible, confident, well-structured document with nothing
    behind it. A run that names zero URLs across its **entire** output fails
    outright rather than being proposed to a human as if it were evidenced.
    """


class MalformedOutputError(PipelineError):
    """An agent run finished without a valid structured tool call — a
    different failure from :class:`NoCitationsError`: the model broke, rather
    than honestly reporting that it found nothing."""


class RunNotSucceededError(PipelineError):
    """An agent run this stage was waiting on finished without succeeding."""


class ArtefactNotApprovedError(PipelineError):
    """A stage that requires an approved input artefact was asked to proceed
    against one that is not ``approved``. ``proposed`` output must never be
    usable by the next stage, or the gate is decorative."""


def _tool_call_arguments(messages: list[dict[str, Any]], tool_name: str) -> Any:
    """The parsed arguments of the **last** call to ``tool_name`` in a
    transcript, or ``None``. Copied verbatim from idea-scout's
    ``service.py``: last, not first, because a model that retries a malformed
    tool call means the later attempt is the one it meant."""
    found: Any = None
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != tool_name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    found = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            else:
                found = arguments
    return found


def extract_research_findings(messages: list[dict[str, Any]]) -> Any | None:
    """One research agent's findings, or ``None`` if the tool call is missing
    or invalid. A single angle degrading is tolerated the way idea-scout
    tolerates a thin research angle — what must **not** be tolerated is the
    synthesised run as a whole citing nothing, checked separately below."""
    data = _tool_call_arguments(messages, FINDINGS_TOOL_NAME)
    if data is None:
        return None
    try:
        return ResearchFindingSet.model_validate(data)
    except ValidationError:
        return None


def _total_citations(*source_lists: list[Source]) -> int:
    return sum(len(sources) for sources in source_lists)


def _extract_cited_artefact[T: BaseModel](
    messages: list[dict[str, Any]],
    *,
    tool_name: str,
    model_cls: type[T],
    citation_groups: Callable[[T], list[list[Source]]],
    malformed_message: str,
    no_citations_message: str,
) -> T:
    """The shared skeleton behind :func:`extract_research_synthesis`,
    :func:`extract_positioning` and :func:`extract_channel_plan`: fetch the
    last call to ``tool_name``, validate it against ``model_cls``, and
    enforce the zero-citation guard over ``citation_groups(result)``.

    Kept as one implementation rather than three near-identical copies
    precisely because the guard is "the milestone, not a detail" (this
    module's docstring) — three copies is three places a change to the guard
    itself (or a fix to it) can drift out of step.
    """
    data = _tool_call_arguments(messages, tool_name)
    if data is None:
        raise MalformedOutputError(malformed_message)
    try:
        result = model_cls.model_validate(data)
    except ValidationError as exc:
        raise MalformedOutputError(str(exc)) from exc

    total = _total_citations(*citation_groups(result))
    if total == 0:
        raise NoCitationsError(no_citations_message)
    return result


def extract_research_synthesis(messages: list[dict[str, Any]]) -> ResearchSynthesis:
    """The research-synthesis agent's output, or a hard failure.

    Raises :class:`MalformedOutputError` when the tool call is missing or
    invalid, and :class:`NoCitationsError` — the milestone's guard — when it
    is well-formed but cites nothing at all. Unlike a single research angle
    finding little, there is no redundancy at the synthesis step: nothing else
    produces the ``research`` artefact, so there is nothing to degrade to.
    """
    return _extract_cited_artefact(
        messages,
        tool_name=RESEARCH_SYNTHESIS_TOOL_NAME,
        model_cls=ResearchSynthesis,
        citation_groups=lambda r: [f.sources for f in r.findings],
        malformed_message=(
            f"the research-synthesis run produced no {RESEARCH_SYNTHESIS_TOOL_NAME} tool call"
        ),
        no_citations_message=(
            "The research run fetched zero URLs. Nothing was found to cite, so no "
            "artefact was produced — try running research again."
        ),
    )


def extract_positioning(messages: list[dict[str, Any]]) -> Positioning:
    """The positioning agent's output, or a hard failure — the same two
    failure modes as :func:`extract_research_synthesis`, for the same reason:
    a positioning claim is exactly as fabricable as a research finding, and an
    operator reviewing it needs the same guarantee."""
    return _extract_cited_artefact(
        messages,
        tool_name=POSITIONING_TOOL_NAME,
        model_cls=Positioning,
        citation_groups=lambda r: [
            *(s.sources for s in r.segments),
            *(p.sources for p in r.pillars),
            *(c.sources for c in r.ctas),
        ],
        malformed_message=f"the positioning run produced no {POSITIONING_TOOL_NAME} tool call",
        no_citations_message=(
            "The positioning run cited nothing from the approved research. Nothing "
            "was produced — try running positioning again."
        ),
    )


def extract_channel_plan(messages: list[dict[str, Any]]) -> ChannelPlan:
    """The channel-plan agent's output, or a hard failure — the same two
    failure modes as :func:`extract_positioning`, for the same reason (M4,
    issue #3): a channel recommendation is exactly as fabricable as a
    positioning claim, and arguably the most confident-sounding one in the
    whole pipeline, because channel advice reads as generic wisdom whether or
    not anyone researched it."""
    return _extract_cited_artefact(
        messages,
        tool_name=CHANNEL_PLAN_TOOL_NAME,
        model_cls=ChannelPlan,
        citation_groups=lambda r: [c.sources for c in r.channels],
        malformed_message=f"the channel-plan run produced no {CHANNEL_PLAN_TOOL_NAME} tool call",
        no_citations_message=(
            "The channel-plan run cited nothing from the approved positioning. Nothing "
            "was produced — try running channel planning again."
        ),
    )


def extract_copy(messages: list[dict[str, Any]]) -> CopySet:
    """The copy agent's output, or a hard failure — the same two failure
    modes as :func:`extract_channel_plan` (M5, issue #4): copy is exactly as
    fabricable as a channel recommendation, and arguably the most
    publishable-looking one in the whole pipeline, since prose reads as
    correct whether or not any pillar or CTA actually backs it."""
    return _extract_cited_artefact(
        messages,
        tool_name=COPY_TOOL_NAME,
        model_cls=CopySet,
        citation_groups=lambda r: [c.sources for c in r.channels],
        malformed_message=f"the copy run produced no {COPY_TOOL_NAME} tool call",
        no_citations_message=(
            "The copy run cited nothing from the approved positioning. Nothing "
            "was produced — try running copy generation again."
        ),
    )


def flatten_citations(
    output: ResearchSynthesis | Positioning | ChannelPlan | CopySet,
) -> list[dict[str, Any]]:
    """Every :class:`Source` across an artefact's structured output,
    deduplicated by URL in first-seen order — what is written to
    ``marketing_artefact.citations``.

    Kept as a column of its own, separate from ``body``, precisely so "this
    artefact cites nothing" is a structural question a query can ask rather
    than a paragraph someone has to read (``biffo.plugin.json``'s own
    rationale for the column).
    """
    if isinstance(output, ResearchSynthesis):
        groups: list[list[Source]] = [f.sources for f in output.findings]
    elif isinstance(output, Positioning):
        groups = [
            *(s.sources for s in output.segments),
            *(p.sources for p in output.pillars),
            *(c.sources for c in output.ctas),
        ]
    elif isinstance(output, ChannelPlan):
        groups = [c.sources for c in output.channels]
    elif isinstance(output, CopySet):
        groups = [c.sources for c in output.channels]
    else:
        # Exhaustive over this function's own type hint — a new artefact type
        # reaching here without a branch of its own must fail loudly rather
        # than silently reusing another stage's `.channels`/`.segments`
        # shape (issue found in review: the pre-M4 version of this function
        # had exactly one `else`, quietly assumed to mean "positioning").
        raise TypeError(f"flatten_citations: unsupported artefact output type {type(output)!r}")
    seen: set[str] = set()
    flat: list[dict[str, Any]] = []
    for sources in groups:
        for source in sources:
            if source.url in seen:
                continue
            seen.add(source.url)
            flat.append(source.model_dump())
    return flat


# ── The agent-run port ────────────────────────────────────────────────────────


#: Terminal states of an agent run (Core's AgentRun, ADR-0014) — mirrors
#: idea-scout's ``models.py``.
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_TERMINAL = frozenset({RUN_COMPLETED, RUN_FAILED})


@dataclass(frozen=True)
class AgentRunView:
    """A read of an async agent run — just what this pipeline needs to decide
    whether it is done and to extract its output-tool call."""

    id: str
    status: str
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in RUN_TERMINAL

    @property
    def succeeded(self) -> bool:
        return self.status == RUN_COMPLETED


class AgentGateway(Protocol):
    """Everything this pipeline needs from Core's agent-run seam
    (``/api/v1/internal/agent-runs``, ADR-0009/ADR-0014). SigV4-signed, no
    owner token to forward — a campaign is tenant-admin data, not
    founder-owned, unlike idea-scout's ``CoreGateway``."""

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        """Request one async agent run; returns its id. ``causation_id`` is
        required, not optional — it is what makes the two research runs
        siblings the orchestration engine's fan-in can recognise as a set
        (idea-scout ``ports.py``'s ``request_agent_run`` docstring)."""
        ...

    async def find_chain_run(self, *, chain_id: str, agent_name: str) -> AgentRunView | None:
        """The run of ``agent_name`` in this causation chain, if one exists
        yet. How this pipeline discovers the research-synthesis run the
        **orchestration engine** created on its behalf — nothing tells the
        plugin its id directly."""
        ...

    async def get_agent_run(self, *, run_id: str) -> AgentRunView | None:
        """Read an agent run's state and transcript. ``None`` if Core has no
        such run — treated as a failure by the caller, not as "still
        running", so a vanished run cannot hang a pipeline forever."""
        ...


# ── Orchestration ─────────────────────────────────────────────────────────────


async def start_research(
    gateway: AgentGateway,
    *,
    brief: dict[str, Any],
    research_model: str = DEFAULT_RESEARCH_MODEL,
) -> tuple[str, list[str]]:
    """Fan out the two research agents on one fresh causation chain.

    Returns ``(chain_id, research_run_ids)``. Nothing polls afterward — the
    engine's ``agent_fan_in`` fires the research-synthesis agent itself once
    both research runs terminate. ``research_run_ids`` is returned so the
    caller can detect the "every research agent failed, so the engine never
    fired synthesis" case in :func:`advance_research` — the run would
    otherwise sit ``pending`` forever with nothing to distinguish it from
    "still researching".
    """
    chain_id = str(uuid.uuid4())
    research_run_ids: list[str] = []
    for agent_name in RESEARCH_AGENT_NAMES:
        run_id = await gateway.request_agent_run(
            agent_name=agent_name,
            definition=research_definition(
                model=research_model, instructions=RESEARCH_INSTRUCTIONS[agent_name]
            ),
            output_tool=findings_tool_schema(),
            input_payload={"brief": brief},
            causation_id=chain_id,
        )
        research_run_ids.append(run_id)
    return chain_id, research_run_ids


async def advance_research(
    gateway: AgentGateway,
    *,
    chain_id: str,
    research_run_ids: list[str],
    synthesis_model: str = DEFAULT_SYNTHESIS_MODEL,
) -> ResearchSynthesis | None:
    """Discover the research-synthesis run the engine fired, once it has.

    Returns ``None`` while research is genuinely still in flight. Raises
    :class:`RunNotSucceededError` when the synthesis run (or, if it never
    fired, every research run) finished without succeeding — the same
    reasoning as idea-scout's ``_advance_research``: a research set that
    failed outright never produces a synthesis run, so without this check the
    artefact would sit ``pending`` forever with nothing to explain why.
    Raises :class:`MalformedOutputError` / :class:`NoCitationsError` via
    :func:`extract_research_synthesis` on a malformed or citation-less
    result. ``synthesis_model`` is accepted for parity with the other stage
    starters even though this stage never *starts* a run — the engine does —
    so callers have one place to read every model this pipeline can use.
    """
    del synthesis_model  # the engine chooses the synthesis run's model, not us

    synthesis_run = await gateway.find_chain_run(
        chain_id=chain_id, agent_name=RESEARCH_SYNTHESIS_AGENT_NAME
    )
    if synthesis_run is not None:
        if not synthesis_run.is_terminal:
            return None  # synthesis is running; nothing to do yet
        if not synthesis_run.succeeded:
            raise RunNotSucceededError("The research-synthesis run did not complete successfully.")
        return extract_research_synthesis(synthesis_run.messages)

    # No synthesis run yet: either research is still in flight (the normal
    # case), or every research run has already failed and the engine correctly
    # declined to fire one.
    views = [await gateway.get_agent_run(run_id=rid) for rid in research_run_ids]
    if any(v is not None and not v.is_terminal for v in views):
        return None  # still researching
    if any(v is not None and v.succeeded for v in views):
        # Terminal and at least one succeeded: give the engine a moment to
        # react to the completion event rather than racing it to a false
        # failure.
        return None
    raise RunNotSucceededError(
        "Every research agent failed to return usable findings. Nothing was found "
        "to synthesise — try running research again."
    )


async def start_positioning(
    gateway: AgentGateway,
    *,
    research_body: dict[str, Any],
    positioning_model: str = DEFAULT_POSITIONING_MODEL,
) -> tuple[str, str]:
    """Request the single positioning agent, given the *approved* research
    artefact's body as its whole input.

    Returns ``(causation_id, run_id)``. Its own one-run chain — not fanned
    out, so the caller tracks ``run_id`` directly via :func:`advance_positioning`
    rather than searching a chain the way research does. A ``causation_id`` is
    still required and generated fresh: idea-scout's ``ports.py`` is explicit
    that a run sent without one is a chain root, which is fine here (nothing
    fans in on it), and it is what lets per-campaign spend be assembled from
    ``agent_runs`` later.
    """
    causation_id = str(uuid.uuid4())
    run_id = await gateway.request_agent_run(
        agent_name=POSITIONING_AGENT_NAME,
        definition=positioning_definition(
            model=positioning_model,
            instructions=POSITIONING_INSTRUCTIONS,
        ),
        output_tool=positioning_tool_schema(),
        input_payload={"research": research_body},
        causation_id=causation_id,
    )
    return causation_id, run_id


async def advance_positioning(gateway: AgentGateway, *, run_id: str) -> Positioning | None:
    """Read the positioning run, advancing nothing else — it is a single run,
    not a chain, so there is no fan-in to discover.

    Returns ``None`` while still running or vanished-but-plausibly-not-yet-
    visible; raises the same failure family as :func:`advance_research` on a
    run that finished without succeeding or without citing anything.
    """
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The positioning run did not complete successfully.")
    return extract_positioning(view.messages)


async def start_channel_plan(
    gateway: AgentGateway,
    *,
    positioning_body: dict[str, Any],
    channel_plan_model: str = DEFAULT_CHANNEL_PLAN_MODEL,
) -> tuple[str, str]:
    """Request the single channel-plan agent (M4), given the *approved*
    positioning artefact's body as its whole input.

    Mirrors :func:`start_positioning` exactly, one stage further down the
    chain: a single-run chain, not fanned out, because nothing fans in on it.
    """
    causation_id = str(uuid.uuid4())
    run_id = await gateway.request_agent_run(
        agent_name=CHANNEL_PLAN_AGENT_NAME,
        definition=channel_plan_definition(
            model=channel_plan_model,
            instructions=CHANNEL_PLAN_INSTRUCTIONS,
        ),
        output_tool=channel_plan_tool_schema(),
        input_payload={"positioning": positioning_body},
        causation_id=causation_id,
    )
    return causation_id, run_id


async def advance_channel_plan(gateway: AgentGateway, *, run_id: str) -> ChannelPlan | None:
    """Read the channel-plan run, advancing nothing else — mirrors
    :func:`advance_positioning`: a single run, not a chain, so there is no
    fan-in to discover."""
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The channel-plan run did not complete successfully.")
    return extract_channel_plan(view.messages)


async def start_copy(
    gateway: AgentGateway,
    *,
    positioning_body: dict[str, Any],
    channel_plan_body: dict[str, Any],
    copy_model: str = DEFAULT_COPY_MODEL,
) -> tuple[str, str]:
    """Request the single copy agent (M5, issue #4), given the *approved*
    positioning AND the *approved* channel-plan artefacts' bodies as its
    input — positioning for the message pillars/CTAs copy is grounded in,
    channel plan for which channels to write for.

    Mirrors :func:`start_channel_plan` exactly, one stage further down the
    chain: a single-run chain, not fanned out, because nothing fans in on it.
    """
    causation_id = str(uuid.uuid4())
    run_id = await gateway.request_agent_run(
        agent_name=COPY_AGENT_NAME,
        definition=copy_definition(model=copy_model, instructions=COPY_INSTRUCTIONS),
        output_tool=copy_tool_schema(),
        input_payload={"positioning": positioning_body, "channel_plan": channel_plan_body},
        causation_id=causation_id,
    )
    return causation_id, run_id


async def advance_copy(gateway: AgentGateway, *, run_id: str) -> CopySet | None:
    """Read the copy run, advancing nothing else — mirrors
    :func:`advance_channel_plan`: a single run, not a chain, so there is no
    fan-in to discover."""
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The copy run did not complete successfully.")
    return extract_copy(view.messages)


def require_approved(status: str, *, what: str) -> None:
    """Raise :class:`ArtefactNotApprovedError` unless ``status`` is
    ``"approved"``. The gate enforcement itself: ``proposed`` output must not
    be usable by the next stage, so this is the one place that rule is
    checked before a later stage's input is built from it."""
    if status != "approved":
        raise ArtefactNotApprovedError(
            f"{what} must be approved before this can proceed (status: {status})."
        )
