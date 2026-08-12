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

**Counting citations is only half of it (issue #22).** "Cites something" and
"cites something it was actually given" are different questions, and only the
first was ever asked. An agent that invents one plausible URL per claim — a
well-known marketing-statistics domain, say — satisfies ``total > 0`` exactly
as well as one that read the research, and the artefact is then proposed to an
operator as evidenced when it is not. For ``positioning``, ``channel_plan`` and
``copy`` the legitimate source set is closed and already known: it is exactly
the approved parent artefact's ``citations`` column. Those three therefore also
check **provenance** — every cited URL must appear in the set the run was
started against (:class:`UncitedSourceError` otherwise), stashed on the pending
artefact at start time for the same reason ``channel_taxonomy`` is: it must be
what THIS run was shown, not whatever the parent has been re-run to since.
``research`` deliberately has no such check — the web is its source, so there
is no closed prior set to check a citation against.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from aws_lambda_powertools import Logger
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
    research_search_query,
)

logger = Logger(child=True)


class PipelineError(Exception):
    """Base for errors the admin routes map to HTTP statuses."""


class NoCitationsError(PipelineError):
    """A research or positioning run produced content with zero citations —
    zero *in the model's structured output*, i.e. ``sources`` across the
    artefact. That is a fact about what the model transcribed, not about what
    retrieval actually returned, and issue #82 is precisely the gap between
    the two: a model can be shown real search results and simply not retype
    them into its tool call.

    The milestone's guard is still right to fail in that case — an artefact
    with no per-claim attribution is not something a human should be shown as
    evidenced, whatever retrieval found — but the message now grounds itself
    in ``AgentRunView.annotations`` (the runtime's own record of what
    retrieval returned, biffo-template#1528/#1530) so the three distinguishable
    causes read as different problems needing different responses:

    - ``sources`` empty, ``annotations`` non-empty — retrieval worked, the
      model didn't transcribe it. Transient; retrying the stage is likely to
      produce a citable result without anything else changing.
    - ``sources`` empty, ``annotations == []`` — retrieval genuinely found
      nothing. Retrying will not help; the brief needs to change.
    - ``sources`` empty, ``annotations is None`` — this run predates the
      annotations column or was never ``:online``, so whether retrieval
      happened at all is simply not known. Said plainly rather than guessed.
    """


class UncitedSourceError(PipelineError):
    """A downstream run cited a URL its own approved input never contained
    (issue #22) — a *different* failure from :class:`NoCitationsError`, and
    the more dangerous one, because it looks like success: the artefact has
    citations, they are well-formed, and every count-based check passes.

    This is only askable of a stage whose legitimate source set is closed.
    ``positioning``, ``channel_plan`` and ``copy`` are each started against an
    approved parent artefact whose ``citations`` column is exactly the set of
    URLs the run was shown, so a source outside it cannot have been read — it
    was invented, or copied from the model's own training data, and either way
    nothing in this run establishes it.

    **The whole artefact fails, and nothing is silently dropped.** Discarding
    the offending :class:`Source` and keeping the claim would leave a
    fabricated statement standing with someone else's evidence beside it,
    which is worse than failing — and would break ``Field(min_length=1)`` the
    moment a claim's only source was the invented one. Same reasoning as the
    refusal to salvage ``annotations`` into ``sources`` in
    :func:`_no_citations_message`: the honest response to "this attribution is
    not real" is to say so, not to tidy it away.
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


class UnknownChannelError(PipelineError):
    """A channel-plan or copy run referenced a ``channel_key`` outside the set
    it was actually given (#76 increment 2) — the agent inventing or altering
    a key rather than copying one from the taxonomy/plan it was shown, or a
    copy run referencing a channel the approved plan did not include. This is
    the structural half of the join #75/#67 exist to make real: "must match
    exactly" used to be prose nobody enforced; this is the enforcement.
    """


class StaleChannelPlanError(PipelineError):
    """A copy run was started against an approved channel plan that predates
    the taxonomy migration (#76 increment 2) — every one of its entries is
    the old free-text shape, with no ``channel_key`` at all, so there is
    structurally nothing for a copy run to reference. Distinct from
    :class:`UnknownChannelError` (which fires when the *agent* invents a key)
    because the cause here is upstream data, not agent behaviour: the fix is
    re-running channel planning on this campaign, not retrying copy."""


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


def citation_source_urls(citations: Any) -> list[str]:
    """Every URL in an artefact's ``citations`` column, first-seen order,
    deduplicated — the closed source set the stage below it may cite from
    (issue #22).

    Accepts all three shapes a caller can be holding, because the caller is a
    route that has just read a row and should not have to care which:
    ``citations`` is *written* as a JSON string
    (:func:`~marketing.admin_app._advance_artefact` does
    ``json.dumps(flatten_citations(...))``), Core may hand it back already
    parsed, and a legacy row may hold ``None``.

    Anything unreadable degrades to ``[]`` rather than raising. ``[]`` means
    "the approved set is not known", which the guard treats as *cannot check* —
    never as *nothing is allowed*. That distinction is the difference between
    shipping this check and stranding every campaign whose artefacts predate
    it; see :func:`_extract_cited_artefact` for the other end of it.
    """
    if isinstance(citations, str):
        try:
            citations = json.loads(citations)
        except json.JSONDecodeError:
            return []
    if not isinstance(citations, list):
        return []
    urls: list[str] = []
    for entry in citations:
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return urls


def _url_key(url: str) -> str:
    """A comparison key for a citation URL: same document, same key.

    Provenance is a question about the document, not about the byte string.
    A model that re-types a source with a trailing slash, a ``#section``
    fragment, or a differently-cased host has cited the parent's source, and
    failing those would make the guard fire on honest runs — which is how a
    guard ends up switched off. So the host is lower-cased (DNS is
    case-insensitive), a trailing ``/`` is dropped, and the fragment is
    discarded (it addresses part of a document, not another one).

    Nothing looser than that. The **path is kept case-sensitively and in
    full**, and the query string is kept, so a real publisher's home page
    cited for a statistic that lives nowhere in the parent is still a
    fabrication — "same domain" is not provenance.
    """
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


#: How many offending URLs an :class:`UncitedSourceError` message names before
#: it summarises the rest. An operator needs to see the shape of the problem,
#: not a wall of URLs — and a wholly-fabricated artefact can carry dozens.
_MAX_REPORTED_UNCITED = 5


def _uncited_message(*, urls: list[str], parent_description: str, retry_hint: str) -> str:
    shown = ", ".join(urls[:_MAX_REPORTED_UNCITED])
    if len(urls) > _MAX_REPORTED_UNCITED:
        shown += f" (and {len(urls) - _MAX_REPORTED_UNCITED} more)"
    n = len(urls)
    return (
        f"The {retry_hint} run cited {n} source{'s' if n != 1 else ''} that {parent_description} "
        f"does not contain: {shown}. A source this run was never given cannot have been read, "
        f"so the claims resting on it are unevidenced and nothing was produced — try running "
        f"{retry_hint} again."
    )


def _no_citations_message(
    *, base_message: str, retry_hint: str, annotations: list[dict[str, Any]] | None
) -> str:
    """Ground :class:`NoCitationsError`'s message in what the run's
    ``annotations`` (issue #82) say about retrieval, so the three cases in
    that class's docstring read as distinguishable problems rather than the
    identical, and for one of them false, "fetched zero URLs" sentence.

    ``base_message`` is the case-specific "cited nothing" sentence each
    caller already had — unchanged wording, and still the correct message
    for the ``annotations == []`` case, since that IS a genuine zero-URL
    retrieval. The other two cases replace it entirely rather than append to
    it: appending "but retrieval actually succeeded" after a sentence that
    opens by asserting the opposite reads as contradictory, not corrective.
    """
    if annotations:
        # Deliberately NOT salvaged into `sources` here or anywhere upstream
        # (see this module's own docstring / the PR that added this check):
        # an annotation carries no claim mapping, so attaching one to a
        # finding/segment/channel would fabricate the very attribution this
        # guard exists to guarantee. The only honest response to "retrieval
        # worked, the model didn't transcribe it" is to fail and say so.
        n = len(annotations)
        return (
            f"Retrieval succeeded — {n} source{'s' if n != 1 else ''} were found — but "
            f"the model did not cite any of them in its output. This is a transcription "
            f"failure, not a failed search: try running {retry_hint} again."
        )
    if annotations is None:
        return (
            f"{base_message} (This run predates citation tracking, or was not a "
            "grounded run, so whether retrieval itself found anything is not known — "
            "do not read this as a proven zero-URL retrieval.)"
        )
    return base_message  # annotations == []: retrieval genuinely found nothing


def _extract_cited_artefact[T: BaseModel](
    messages: list[dict[str, Any]],
    *,
    annotations: list[dict[str, Any]] | None,
    tool_name: str,
    model_cls: type[T],
    citation_groups: Callable[[T], list[list[Source]]],
    malformed_message: str,
    no_citations_message: str,
    retry_hint: str,
    allowed_source_urls: Collection[str] | None = None,
    parent_description: str = "this run's approved input",
) -> T:
    """The shared skeleton behind :func:`extract_research_synthesis`,
    :func:`extract_positioning`, :func:`extract_channel_plan` and
    :func:`extract_copy`: fetch the last call to ``tool_name``, validate it
    against ``model_cls``, and enforce **both** citation guards over
    ``citation_groups(result)``.

    1. *Quantity* — a run whose entire output cites nothing raises
       :class:`NoCitationsError`, grounded in ``annotations``, the run's own
       record of what retrieval returned (issue #82), not just what the
       model's tool call repeated of it.
    2. *Provenance* — every cited URL must appear in ``allowed_source_urls``,
       or :class:`UncitedSourceError` (issue #22). Only stages with a closed
       source set pass one: ``research`` does not, since the web is its
       source.

    ``allowed_source_urls`` is tri-state, exactly like ``annotations``, and
    the states must not be collapsed: ``None`` is "not known" (an artefact
    started before this check existed carries no stashed set, and must still
    advance), an empty collection is treated the same way (an approved parent
    with zero citations cannot exist — it would have failed guard 1 before it
    could be proposed — so an empty set means legacy or hand-edited data), and
    a non-empty one is enforced. Failing closed on the first two would strand
    every in-flight run at deploy time on evidence nobody recorded.

    Kept as one implementation rather than four near-identical copies
    precisely because the guard is "the milestone, not a detail" (this
    module's docstring) — four copies is four places a change to the guard
    itself (or a fix to it) can drift out of step.
    """
    data = _tool_call_arguments(messages, tool_name)
    if data is None:
        raise MalformedOutputError(malformed_message)
    try:
        result = model_cls.model_validate(data)
    except ValidationError as exc:
        raise MalformedOutputError(str(exc)) from exc

    groups = citation_groups(result)
    total = _total_citations(*groups)
    if total == 0:
        raise NoCitationsError(
            _no_citations_message(
                base_message=no_citations_message, retry_hint=retry_hint, annotations=annotations
            )
        )

    if allowed_source_urls:
        allowed = {_url_key(url) for url in allowed_source_urls}
        uncited: list[str] = []
        for sources in groups:
            for source in sources:
                if _url_key(source.url) not in allowed and source.url not in uncited:
                    uncited.append(source.url)
        if uncited:
            raise UncitedSourceError(
                _uncited_message(
                    urls=uncited,
                    parent_description=parent_description,
                    retry_hint=retry_hint,
                )
            )
    return result


def extract_research_synthesis(
    messages: list[dict[str, Any]], *, annotations: list[dict[str, Any]] | None = None
) -> ResearchSynthesis:
    """The research-synthesis agent's output, or a hard failure.

    Raises :class:`MalformedOutputError` when the tool call is missing or
    invalid, and :class:`NoCitationsError` — the milestone's guard — when it
    is well-formed but cites nothing at all. Unlike a single research angle
    finding little, there is no redundancy at the synthesis step: nothing else
    produces the ``research`` artefact, so there is nothing to degrade to.

    ``annotations`` is the run's own record of what retrieval returned
    (:attr:`AgentRunView.annotations`, issue #82) — defaulted to ``None`` so
    a direct call (as every existing test makes) still works, reading as "not
    known" rather than "definitely nothing", which is the correct default.
    """
    return _extract_cited_artefact(
        messages,
        annotations=annotations,
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
        retry_hint="research",
    )


def extract_positioning(
    messages: list[dict[str, Any]],
    *,
    allowed_source_urls: Collection[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> Positioning:
    """The positioning agent's output, or a hard failure — the same failure
    modes as :func:`extract_research_synthesis`, for the same reason: a
    positioning claim is exactly as fabricable as a research finding, and an
    operator reviewing it needs the same guarantee. ``annotations`` — see
    :func:`extract_research_synthesis`.

    ``allowed_source_urls`` is the approved **research** artefact's citation
    URLs — the closed set this run was actually shown (issue #22), stashed on
    the pending artefact by ``start_positioning_route`` at start time so it is
    what this run saw rather than whatever research has since been re-run to.
    A segment/pillar/CTA citing anything outside it raises
    :class:`UncitedSourceError`; ``None`` means the set is not known and the
    check is skipped (see :func:`_extract_cited_artefact`)."""
    return _extract_cited_artefact(
        messages,
        annotations=annotations,
        allowed_source_urls=allowed_source_urls,
        parent_description="the approved research it was given",
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
        retry_hint="positioning",
    )


def extract_channel_plan(
    messages: list[dict[str, Any]],
    *,
    taxonomy: dict[str, Literal["organic", "paid"]],
    allowed_source_urls: Collection[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> ChannelPlan:
    """The channel-plan agent's output, or a hard failure — the same failure
    modes as :func:`extract_positioning`, for the same reason (M4,
    issue #3): a channel recommendation is exactly as fabricable as a
    positioning claim, and arguably the most confident-sounding one in the
    whole pipeline, because channel advice reads as generic wisdom whether or
    not anyone researched it. ``annotations`` — see
    :func:`extract_research_synthesis`.

    ``allowed_source_urls`` is the approved **positioning** artefact's
    citation URLs (issue #22) — see :func:`extract_positioning` for the full
    reasoning; this is that same check one stage further down, which is where
    the issue was filed from.

    ``taxonomy`` is ``{channel_key: motion}`` for exactly the taxonomy this
    run was shown (#76 increment 2) — stored on the artefact at start time,
    not re-fetched, so this validates against what the agent actually saw
    rather than whatever the taxonomy happens to be *now*. Every
    recommendation naming a ``channel_key`` is checked against it
    (:class:`UnknownChannelError` on a miss — the agent inventing or
    mangling a key) and has its ``motion`` **overwritten** from the taxonomy:
    the agent stops asserting motion independently for a real channel, full
    stop, regardless of what it put in the field.
    """
    plan = _extract_cited_artefact(
        messages,
        annotations=annotations,
        allowed_source_urls=allowed_source_urls,
        parent_description="the approved positioning it was given",
        tool_name=CHANNEL_PLAN_TOOL_NAME,
        model_cls=ChannelPlan,
        citation_groups=lambda r: [c.sources for c in r.channels],
        malformed_message=f"the channel-plan run produced no {CHANNEL_PLAN_TOOL_NAME} tool call",
        no_citations_message=(
            "The channel-plan run cited nothing from the approved positioning. Nothing "
            "was produced — try running channel planning again."
        ),
        retry_hint="channel planning",
    )
    for recommendation in plan.channels:
        if recommendation.channel_key is None:
            continue  # a suggested_label proposal — motion is the agent's own, required already
        if recommendation.channel_key not in taxonomy:
            raise UnknownChannelError(
                f"The channel-plan run referenced channel_key {recommendation.channel_key!r}, "
                "which was not in the taxonomy it was given."
            )
        recommendation.motion = taxonomy[recommendation.channel_key]  # derived, not asserted
    return plan


def extract_copy(
    messages: list[dict[str, Any]],
    *,
    channel_plan_channels: dict[str, Literal["organic", "paid"]],
    allowed_source_urls: Collection[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> CopySet:
    """The copy agent's output, or a hard failure — the same failure modes as
    :func:`extract_channel_plan` (M5, issue #4): copy is exactly as fabricable
    as a channel recommendation, and arguably the most publishable-looking one
    in the whole pipeline, since prose reads as correct whether or not any
    pillar or CTA actually backs it. ``annotations`` — see
    :func:`extract_research_synthesis`.

    ``allowed_source_urls`` is the **union** of the approved positioning's and
    approved channel plan's citation URLs (issue #22). Issue #22 named only
    positioning and channel plan because M5 did not exist when it was filed;
    copy has the identical closed source set, so it gets the identical check
    rather than inheriting the gap one stage further down.

    ``channel_plan_channels`` is ``{channel_key: motion}`` for exactly the
    approved channel plan entries that themselves carried a ``channel_key``
    (#76 increment 2) — a proposal with only a ``suggested_label`` is
    excluded, since it is not yet a real channel. This is the join #67/#75
    exist to make structural: every copy channel must be one this run was
    actually given (:class:`UnknownChannelError` otherwise — "the copy stage
    cannot reference a channel the plan did not include"), and ``motion`` is
    **overwritten** from the plan rather than trusted from the copy agent,
    same reasoning as :func:`extract_channel_plan`.
    """
    result = _extract_cited_artefact(
        messages,
        annotations=annotations,
        allowed_source_urls=allowed_source_urls,
        parent_description="the approved positioning and channel plan it was given",
        tool_name=COPY_TOOL_NAME,
        model_cls=CopySet,
        citation_groups=lambda r: [c.sources for c in r.channels],
        malformed_message=f"the copy run produced no {COPY_TOOL_NAME} tool call",
        no_citations_message=(
            "The copy run cited nothing from the approved positioning. Nothing "
            "was produced — try running copy generation again."
        ),
        retry_hint="copy generation",
    )
    for channel_copy in result.channels:
        if channel_copy.channel_key not in channel_plan_channels:
            raise UnknownChannelError(
                f"The copy run referenced channel_key {channel_copy.channel_key!r}, which the "
                "approved channel plan did not include."
            )
        channel_copy.motion = channel_plan_channels[channel_copy.channel_key]  # derived
    return result


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
    whether it is done and to extract its output-tool call.

    ``annotations`` is Core's ``AgentRunResponse.annotations`` (biffo-template
    #1528/#1530): the ``:online`` grounding citations the *runtime* recorded on
    the run, independent of whatever the model chose to retype into its
    structured tool call. It is a fact about the run, not an inference from
    ``messages`` — which is exactly why the citation guard (#82) reads it
    instead of, or as well as, ``messages``. Three states, each meaning
    something different and none collapsible into another:

    - ``None`` — this run predates the annotations column, or was never an
      ``:online`` run. Whether retrieval happened is simply not known.
    - ``[]`` — the run WAS asked to ground, and retrieval genuinely returned
      nothing to cite.
    - non-empty — retrieval succeeded; the run has evidence, whatever the
      model's tool call did or didn't repeat of it.
    """

    id: str
    status: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] | None = None

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
            # `search_query` FIRST, and per agent (issue #101). An `:online`
            # run's retrieval is derived from this payload — the provider
            # searches before the model is invoked — so the payload, not the
            # instructions, is the only place an angle can affect what is
            # retrieved. Both agents used to be sent the identical
            # `{"brief": brief}` and duly received the same pages (4 of 5
            # shared on campaign `ed7c5bc2`). `brief` is unchanged and still
            # carries the full brief for the model to read; the query line is
            # additive, and first because JSON key order is preserved and the
            # angle should lead the text a query is derived from.
            input_payload={
                "search_query": research_search_query(agent_name=agent_name, brief=brief),
                "brief": brief,
            },
            causation_id=chain_id,
        )
        research_run_ids.append(run_id)
    return chain_id, research_run_ids


def _aggregate_research_annotations(
    views: list[AgentRunView | None],
) -> list[dict[str, Any]] | None:
    """Aggregate ``annotations`` across the runs that actually performed
    retrieval — the **research** runs, not the synthesis run (issue #90).

    ``DEFAULT_RESEARCH_MODEL`` is ``:online``; ``DEFAULT_SYNTHESIS_MODEL`` is
    not. The synthesis run fans in over the research runs' output and never
    grounds itself, so its own ``.annotations`` can never be anything but the
    "not a grounded run" state — reading it, as :func:`advance_research` used
    to, makes the zero-citations guard structurally blind to what retrieval
    actually found. The research runs named in ``research_run_ids`` are the
    ones that retrieved, so they are the ones this aggregates over.

    Three-way result, matching :class:`AgentRunView.annotations`'s own
    tri-state so the guard's existing None/[]/non-empty branches keep their
    meaning one level up:

    - **Union, if any run has non-empty annotations.** A chain where one
      research angle retrieved and the other found nothing is not treated as
      a distinct case from one where both retrieved: either way, retrieval
      demonstrably happened somewhere in the chain, so the guard's "retrieval
      succeeded but wasn't transcribed" message is the right one, and its
      wording already just reports a total count — it does not claim every
      angle succeeded.
    - **``None``, if no run has non-empty annotations and at least one is
      unresolved** — either the view itself is missing (a vanished run) or
      its ``annotations`` is ``None`` (predates the column, or somehow
      wasn't a grounded run). The aggregate must not assert a proven
      zero-URL retrieval when part of the chain's status is unknown.
    - **``[]``, only when every run resolved and every one reported ``[]``.**
      This is the one case where "retrieval genuinely found nothing" is true
      of the whole chain, not just the run the guard used to look at.
    """
    saw_unknown = False
    union: list[dict[str, Any]] = []
    for view in views:
        if view is None or view.annotations is None:
            saw_unknown = True
        elif view.annotations:
            union.extend(view.annotations)
    if union:
        return union
    if saw_unknown:
        return None
    return []


def _annotation_url(entry: Any) -> str | None:
    """The URL inside one ``annotations`` entry, whichever shape it arrived in.

    Core stores what the runtime recorded, and OpenRouter's ``url_citation``
    annotation has been seen both flattened (``{"type": "url_citation",
    "url": ...}`` — the shape every fixture in this repo uses and the shape
    Core writes today) and nested (``{"url_citation": {"url": ...}}``, the
    upstream OpenAI-compatible form). Reading only one of them would make the
    breadth measurement below silently report zero the day the other appears,
    which is exactly the kind of quiet blindness issue #101 was filed about.
    """
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        nested = entry.get("url_citation")
        url = nested.get("url") if isinstance(nested, dict) else None
    return url if isinstance(url, str) and url else None


def _is_site_root(url: str) -> bool:
    """True for a bare origin — ``https://example.com`` or
    ``https://example.com/`` — and false for anything carrying a path or a
    query. The distinction issue #101 measured by hand: a homepage citation
    cannot substantiate a claim about a price on a pricing page."""
    parts = urlsplit(url.strip())
    return parts.path.strip("/") == "" and not parts.query


@dataclass(frozen=True)
class EvidenceProfile:
    """How broad and how deep one research chain's retrieval actually was.

    Every number here was measured **by hand**, off provider annotations, to
    file issue #101 — 5 URLs per agent, 4 shared, 6 distinct, 0 deep pages.
    That is the argument for computing it on every run instead: the evidence
    base being thin is not visible in any artefact an operator reads, and
    nothing failed, so the only way to notice was for somebody to go looking.

    ``runs_measured`` is deliberately separate from the count of runs asked
    about: a run whose ``annotations`` are ``None`` (not a grounded run, or a
    vanished view) contributes nothing and must not be read as one that
    retrieved zero URLs — the same tri-state distinction
    :func:`_aggregate_research_annotations` keeps one level up.
    """

    runs_measured: int
    distinct_urls: int
    shared_urls: int
    deep_pages: int
    site_roots: int


def evidence_profile(views: Collection[AgentRunView | None]) -> EvidenceProfile:
    """Measure the retrieval pool the research runs actually drew on.

    ``shared_urls`` counts distinct URLs returned to more than one run — the
    fan-out's redundancy, and the number that says whether running two angles
    bought two evidence pools or one. ``site_roots`` counts distinct URLs with
    no path at all, which is the depth question: a pool of home pages cannot
    support a finding that quotes a figure, however many home pages it holds.
    """
    per_run: list[set[str]] = []
    for view in views:
        if view is None or view.annotations is None:
            continue
        urls = {u for u in (_annotation_url(a) for a in view.annotations) if u}
        per_run.append({_url_key(u) for u in urls})
    distinct: set[str] = set().union(*per_run) if per_run else set()
    shared = {url for url in distinct if sum(url in run for run in per_run) > 1}
    roots = {url for url in distinct if _is_site_root(url)}
    return EvidenceProfile(
        runs_measured=len(per_run),
        distinct_urls=len(distinct),
        shared_urls=len(shared),
        deep_pages=len(distinct) - len(roots),
        site_roots=len(roots),
    )


def _log_evidence_profile(*, chain_id: str, views: Collection[AgentRunView | None]) -> None:
    """Record this chain's retrieval breadth where an operator investigating a
    thin artefact will find it, without failing the run over it.

    Not a guard, on purpose. Thin, homepage-only retrieval is a property of
    what the provider returned, not of anything the model did wrong, so
    failing on it would strand a campaign on a condition re-running cannot
    fix — see this module's docstring on why the citation guards fail closed
    and this one does not.
    """
    profile = evidence_profile(views)
    logger.info(
        "research retrieval breadth: %d distinct URLs across %d grounded run(s) "
        "(%d shared by more than one, %d deep pages, %d site roots)",
        profile.distinct_urls,
        profile.runs_measured,
        profile.shared_urls,
        profile.deep_pages,
        profile.site_roots,
        extra={
            "causation_id": chain_id,
            "research_runs_measured": profile.runs_measured,
            "research_distinct_urls": profile.distinct_urls,
            "research_shared_urls": profile.shared_urls,
            "research_deep_pages": profile.deep_pages,
            "research_site_roots": profile.site_roots,
        },
    )


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

    The citation guard is grounded in the **research** runs' annotations
    (:func:`_aggregate_research_annotations`), not the synthesis run's own —
    issue #90: the synthesis run is never a grounded (``:online``) run, so its
    ``.annotations`` can never answer "did retrieval find anything"; the two
    research runs named in ``research_run_ids`` are the ones that retrieved.

    Those same annotations are also measured and logged
    (:func:`_log_evidence_profile`, issue #101) — how many distinct URLs the
    chain retrieved, how many both angles were handed, and how many were bare
    home pages. That is a report, not a gate: it never changes what this
    function returns or raises.
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
        research_views = [await gateway.get_agent_run(run_id=rid) for rid in research_run_ids]
        _log_evidence_profile(chain_id=chain_id, views=research_views)
        return extract_research_synthesis(
            synthesis_run.messages,
            annotations=_aggregate_research_annotations(research_views),
        )

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


async def advance_positioning(
    gateway: AgentGateway,
    *,
    run_id: str,
    allowed_source_urls: Collection[str] | None = None,
) -> Positioning | None:
    """Read the positioning run, advancing nothing else — it is a single run,
    not a chain, so there is no fan-in to discover.

    Returns ``None`` while still running or vanished-but-plausibly-not-yet-
    visible; raises the same failure family as :func:`advance_research` on a
    run that finished without succeeding, without citing anything, or citing
    something it was never given (``allowed_source_urls`` — see
    :func:`extract_positioning`).
    """
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The positioning run did not complete successfully.")
    return extract_positioning(
        view.messages, allowed_source_urls=allowed_source_urls, annotations=view.annotations
    )


async def start_channel_plan(
    gateway: AgentGateway,
    *,
    positioning_body: dict[str, Any],
    taxonomy: list[dict[str, Any]],
    channel_plan_model: str = DEFAULT_CHANNEL_PLAN_MODEL,
) -> tuple[str, str]:
    """Request the single channel-plan agent (M4), given the *approved*
    positioning artefact's body AND the tenant's channel taxonomy as input.

    ``taxonomy`` (#76 increment 2) is a list of
    ``{channel_key, label, motion, category}`` dicts — the tenant's
    ``marketing_channel`` rows, fetched by the caller — so the agent can pick
    real, stable ids rather than inventing free text the copy stage and
    ``marketing_link`` would then have to trust exactly (#75). The caller is
    also responsible for keeping a ``{channel_key: motion}`` copy of this same
    taxonomy to validate against later (:func:`extract_channel_plan`'s
    ``taxonomy`` parameter) — passed here rather than re-derived, because it
    must be exactly what THIS run was shown, not whatever the taxonomy has
    grown to by the time the run completes.

    Mirrors :func:`start_positioning` otherwise, one stage further down the
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
        input_payload={"positioning": positioning_body, "channel_taxonomy": taxonomy},
        causation_id=causation_id,
    )
    return causation_id, run_id


async def advance_channel_plan(
    gateway: AgentGateway,
    *,
    run_id: str,
    taxonomy: dict[str, Literal["organic", "paid"]],
    allowed_source_urls: Collection[str] | None = None,
) -> ChannelPlan | None:
    """Read the channel-plan run, advancing nothing else — mirrors
    :func:`advance_positioning`: a single run, not a chain, so there is no
    fan-in to discover. ``taxonomy`` is ``{channel_key: motion}`` for exactly
    what :func:`start_channel_plan` gave this run — see
    :func:`extract_channel_plan` for how it and ``allowed_source_urls`` are
    used."""
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The channel-plan run did not complete successfully.")
    return extract_channel_plan(
        view.messages,
        taxonomy=taxonomy,
        allowed_source_urls=allowed_source_urls,
        annotations=view.annotations,
    )


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


async def advance_copy(
    gateway: AgentGateway,
    *,
    run_id: str,
    channel_plan_channels: dict[str, Literal["organic", "paid"]],
    allowed_source_urls: Collection[str] | None = None,
) -> CopySet | None:
    """Read the copy run, advancing nothing else — mirrors
    :func:`advance_channel_plan`: a single run, not a chain, so there is no
    fan-in to discover. ``channel_plan_channels`` is ``{channel_key: motion}``
    for exactly the approved channel plan's real (non-proposal) entries — see
    :func:`extract_copy` for how it and ``allowed_source_urls`` are used."""
    view = await gateway.get_agent_run(run_id=run_id)
    if view is None or not view.is_terminal:
        return None
    if not view.succeeded:
        raise RunNotSucceededError("The copy run did not complete successfully.")
    return extract_copy(
        view.messages,
        channel_plan_channels=channel_plan_channels,
        allowed_source_urls=allowed_source_urls,
        annotations=view.annotations,
    )


def channel_plan_channel_map(
    channel_plan_body: dict[str, Any],
) -> dict[str, Literal["organic", "paid"]]:
    """``{channel_key: motion}`` for an approved channel plan's real entries
    (#76 increment 2) — those carrying a ``channel_key``. A
    ``suggested_label``-only proposal is excluded: it is not a real channel
    until an operator accepts it, so copy must not be written for it yet.

    Raises :class:`StaleChannelPlanError` when the plan has entries but NONE
    of them carry a ``channel_key`` — every entry is the pre-#76 free-text
    shape (``{"channel": ..., "motion": ...}``, no id at all). This is the
    explicit answer to "what happens to dev's existing approved plans/copy":
    rather than silently building a copy run with nothing to reference, or a
    bare ``KeyError``, this campaign needs channel planning re-run before
    copy can be generated for it. An empty plan (``channels: []``, or no
    ``channels`` key at all) is not this case — a plan can legitimately
    recommend nothing yet — so it returns ``{}`` rather than raising.
    """
    channels = channel_plan_body.get("channels") or []
    mapping = {c["channel_key"]: c["motion"] for c in channels if c.get("channel_key")}
    if channels and not mapping:
        raise StaleChannelPlanError(
            "This campaign's approved channel plan predates channel taxonomy ids (#76) "
            "— none of its entries carry a channel_key. Re-run channel planning (and then "
            "copy) for this campaign before generating copy."
        )
    return mapping


def require_channel_keyed_copy(channels: list[dict[str, Any]]) -> None:
    """Raise :class:`StaleChannelPlanError` when an approved copy artefact's
    ``channels`` has entries but NONE carry a ``channel_key`` — the same
    pre-#76-increment-2 free-text shape :func:`channel_plan_channel_map`
    detects, checked here at **pack-assembly** time.

    This is a distinct call site from that function's own guard because a
    copy artefact approved before this migration existed never passed through
    :func:`start_copy_route`'s check at all — it predates the code that
    checks it. A campaign whose plan AND copy were both approved on dev
    before #76 increment 2, including the 121-character free-text channel
    name that overflowed ``marketing_link.channel`` in #75, reaches this
    check the first time anyone opens its pack, and gets a named, actionable
    error instead of a ``KeyError``/bare 500 out of ``_ensure_links``.
    """
    if channels and not any(c.get("channel_key") for c in channels):
        raise StaleChannelPlanError(
            "This campaign's approved copy predates channel taxonomy ids (#76) — none of "
            "its channels carry a channel_key. Re-run channel planning and copy generation "
            "for this campaign before assembling a pack."
        )


def require_approved(status: str, *, what: str) -> None:
    """Raise :class:`ArtefactNotApprovedError` unless ``status`` is
    ``"approved"``. The gate enforcement itself: ``proposed`` output must not
    be usable by the next stage, so this is the one place that rule is
    checked before a later stage's input is built from it."""
    if status != "approved":
        raise ArtefactNotApprovedError(
            f"{what} must be approved before this can proceed (status: {status})."
        )
