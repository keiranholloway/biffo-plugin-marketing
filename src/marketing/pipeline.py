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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from aws_lambda_powertools import Logger
from pydantic import BaseModel, ValidationError

from .definitions import (
    CHANNEL_EVIDENCE_AGENT_NAME,
    CHANNEL_EVIDENCE_INSTRUCTIONS,
    CHANNEL_EVIDENCE_TOOL_NAME,
    CHANNEL_PLAN_AGENT_NAME,
    CHANNEL_PLAN_INSTRUCTIONS,
    CHANNEL_PLAN_TOOL_NAME,
    COPY_AGENT_NAME,
    COPY_INSTRUCTIONS,
    COPY_LENGTH_BUDGET,
    COPY_LENGTH_CEILING_MULTIPLE,
    COPY_TOOL_NAME,
    DEFAULT_CHANNEL_EVIDENCE_MODEL,
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
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    RESEARCH_SYNTHESIS_TOOL_NAME,
    ChannelCopy,
    ChannelEvidenceSet,
    ChannelPlan,
    CopySet,
    LengthOverage,
    Positioning,
    ResearchFindingSet,
    ResearchSynthesis,
    Source,
    channel_evidence_definition,
    channel_evidence_tool_schema,
    channel_plan_definition,
    channel_plan_search_query,
    channel_plan_tool_schema,
    copy_definition,
    copy_tool_schema,
    findings_tool_schema,
    positioning_definition,
    positioning_tool_schema,
    research_definition,
    research_search_query,
    research_synthesis_definition,
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


class UngroundedRecommendationError(PipelineError):
    """A channel recommendation cited nothing this run itself retrieved
    (issue #65) — every one of its sources was carried over from the approved
    positioning it was handed.

    **A different failure from :class:`UncitedSourceError`, and the one that
    check cannot see.** Provenance asks "could this source have been read";
    carried-over positioning sources pass that trivially, because they
    demonstrably were. This asks the question the citation count never did:
    was the recommendation grounded in evidence gathered for the CHANNEL
    question, or in evidence gathered to answer a different one.

    That distinction is the whole of issue #65. Research asks who this
    audience is and what competitors say to them; channel planning asks where
    that audience converts. A source about a competitor's pricing page can
    legitimately support "recommend Google Search ads" under a count-based
    guard, and nothing detects that the evidence is about the wrong question —
    while channel choice is what every downstream artefact is generated *per*,
    so a wrong one is not one wrong artefact, it is every artefact after it.

    Raised per recommendation rather than per plan, deliberately: a plan-level
    check passes the moment one channel is researched, and the rest ride along
    on it.

    Only askable when the run's own retrieval is **known** — see
    :func:`extract_channel_plan` for the tri-state, which is the same one
    ``annotations`` and ``allowed_source_urls`` already carry.
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


class MotionNotAllowedError(PipelineError):
    """A channel-plan run returned a recommendation whose motion is outside
    the campaign's own motion (#67) — an organic campaign being handed a paid
    channel, or the reverse.

    Distinct from :class:`UnknownChannelError` because the two say different
    things to an operator: that one means the agent invented a key, this one
    means it ignored a decision the operator had already taken. Fatal to the
    whole plan for the same reason that one is: this pipeline's artefacts are
    all-or-nothing, and dropping the offending entry silently would leave an
    approved plan that is missing something nobody can see was ever there.

    Structurally, this fires only for a ``suggested_label`` proposal — the one
    place the agent still asserts a motion of its own — plus a defence-in-depth
    check on the taxonomy snapshot itself. A ``channel_key`` cannot reach here
    with the wrong motion: the taxonomy the run was shown was already narrowed
    to the campaign's motion before it started, and every key is checked against
    that snapshot (:class:`UnknownChannelError`).
    """


class StaleChannelPlanError(PipelineError):
    """A copy run was started against an approved channel plan that predates
    the taxonomy migration (#76 increment 2) — every one of its entries is
    the old free-text shape, with no ``channel_key`` at all, so there is
    structurally nothing for a copy run to reference. Distinct from
    :class:`UnknownChannelError` (which fires when the *agent* invents a key)
    because the cause here is upstream data, not agent behaviour: the fix is
    re-running channel planning on this campaign, not retrying copy."""


class CopyTooLongError(PipelineError):
    """A copy run returned a field more than
    :data:`~marketing.definitions.COPY_LENGTH_CEILING_MULTIPLE` times its
    :data:`~marketing.definitions.COPY_LENGTH_BUDGET` (issue #128) — the copy
    equivalent of :class:`UnknownChannelError`: not a judgement call about
    style, but evidence the constraint was not applied at all.

    Deliberately the *only* fatal half of the length rule, and deliberately
    not a ``max_length`` on the model — see
    :func:`measure_copy_length` for where the line is drawn and why."""


# ── Length: measured, recorded, and only fatal at the far end (issue #128) ───


def measure_copy_length(channels: Sequence[ChannelCopy]) -> None:
    """Record every field over :data:`COPY_LENGTH_BUDGET` on its own
    :class:`ChannelCopy`, and raise :class:`CopyTooLongError` for anything
    past :data:`COPY_LENGTH_CEILING_MULTIPLE` times that budget. Mutates in
    place, exactly as :func:`extract_copy`'s ``motion`` carry-over does.

    ## Why over-budget is recorded rather than rejected

    Issue #128 asks for brevity as a constraint rather than a preference, and
    the obvious reading of that is a hard limit — a ``max_length`` on the
    field, or this check raising on the first character over. Both are worse
    than what they replace, for two separate reasons:

    1. **Truncating a wordy headline gives you a wordy headline with the end
       cut off.** The paid pack trims to platform limits (`_fit_to_limit`)
       because an Ads Manager field genuinely cannot hold more; that is
       fitting, not writing. Nothing equivalent is available here, because the
       thing being asked for is a different sentence, not the same sentence
       shortened.
    2. **The run is expensive and the failure is cheap to survive.** One long
       headline failing ``model_validate`` discards every other channel's copy
       with it, from an agent already running close to its 240s ceiling
       (#131), to fix something an operator can read and judge in seconds.

    So the budget is enforced where a length problem is actually decidable:
    the approval gate this artefact already has to pass. ``over_budget`` rides
    into ``marketing_artefact.body`` and the admin UI renders it against the
    offending line, so the operator approving the copy sees "94 characters,
    budget 60" beside the headline itself. That is what stops it being
    advisory — not that the model was asked nicely, but that its drift is
    measured, attributed per field, and on screen at the moment someone
    decides whether to ship it.

    ## Why the ceiling is fatal anyway

    Past ``COPY_LENGTH_CEILING_MULTIPLE`` there is nothing left to judge, so
    handing it to an operator wastes their time instead of saving the run's.
    An operator who hits this sees the :class:`CopyTooLongError` message —
    which names the channel, the field, its length and its budget, and says to
    run copy generation again — as the ``detail`` of a 502 from the advance
    route, exactly like an uncited source or an unknown channel
    (``admin_app._pipeline_error_to_http``, which maps the BASE class so a new
    error type cannot fall through as a bare 500).
    """
    for channel_copy in channels:
        overages: list[LengthOverage] = []
        for field_name, budget in COPY_LENGTH_BUDGET.items():
            length = len(getattr(channel_copy, field_name))
            if length > budget:
                overages.append(LengthOverage(field=field_name, length=length, budget=budget))
        channel_copy.over_budget = overages

    fatal = [
        (channel_copy, overage)
        for channel_copy in channels
        for overage in channel_copy.over_budget
        if overage.length > overage.budget * COPY_LENGTH_CEILING_MULTIPLE
    ]
    if fatal:
        detail = "; ".join(
            f"{channel_copy.channel_key} {overage.field} is {overage.length} characters "
            f"(budget {overage.budget})"
            for channel_copy, overage in fatal
        )
        raise CopyTooLongError(
            f"The copy run returned copy more than {COPY_LENGTH_CEILING_MULTIPLE}x over its "
            f"length budget, so it was not written to the brief at all: {detail}. Nothing "
            "was produced — try running copy generation again."
        )


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


def annotation_source_urls(annotations: list[dict[str, Any]] | None) -> list[str] | None:
    """Every URL the runtime recorded this run retrieving, deduplicated in
    first-seen order — or ``None`` when there is no record to read.

    The tri-state of :attr:`AgentRunView.annotations` is preserved exactly,
    because collapsing it is the mistake every check in this module is careful
    not to make: ``None`` is "not known", ``[]`` is "retrieval genuinely
    returned nothing", and those two lead to opposite decisions in
    :func:`extract_channel_plan` — skip the check, versus fail the run.

    Reads both annotation shapes via :func:`_annotation_url`, for the same
    reason the breadth instrument does.
    """
    if annotations is None:
        return None
    urls: list[str] = []
    for entry in annotations:
        url = _annotation_url(entry)
        if url and url not in urls:
            urls.append(url)
    return urls


#: How much of a retrieved page's recorded text is carried into the planning
#: run's payload, per page.
#:
#: Bounded for the reason ``SEARCH_QUERY_BRIEF_CHARS`` is: the planning run has
#: to fit ten pages, the whole positioning body and the taxonomy inside one
#: payload and still answer inside ``AGENT_TIMEOUT_SECONDS``. The excerpt is a
#: fallback, not the main channel — the grounding run's own ``note`` is what
#: carries the reading — so it is sized to identify the page and support a
#: figure, not to reproduce it.
EVIDENCE_EXCERPT_CHARS = 1200


def extract_channel_evidence(messages: list[dict[str, Any]]) -> ChannelEvidenceSet:
    """The grounding run's per-page notes, or :class:`MalformedOutputError`.

    No citation guard of its own, deliberately, and the two reasons are worth
    separating:

    - **This is not an artefact.** Nothing here is persisted, approved or shown
      to an operator; it is an intermediate reading handed to the planning run.
      The guards exist to stop an unevidenced *artefact* reaching a human, and
      the artefact this stage produces is the plan, which is guarded exactly as
      before.
    - **Its `url`s are not trusted anyway.** :func:`retrieved_evidence` keeps
      only the notes whose URL the runtime independently recorded, so a page
      the model invented here cannot reach the planning run at all — let alone
      be citable by it.

    A missing tool call IS fatal, for the reason issue #159 records: without
    the notes the planning run would be handed URLs and titles and asked to
    write a rationale about pages it has no reading of, which is a fabrication
    invitation dressed as grounding. Better to fail with the #159 sentence an
    operator can act on.
    """
    data = _tool_call_arguments(messages, CHANNEL_EVIDENCE_TOOL_NAME)
    if data is None:
        raise MalformedOutputError(
            f"the channel-evidence run produced no {CHANNEL_EVIDENCE_TOOL_NAME} tool call"
        )
    try:
        return ChannelEvidenceSet.model_validate(data)
    except ValidationError as exc:
        raise MalformedOutputError(str(exc)) from exc


def retrieved_evidence(
    annotations: list[dict[str, Any]] | None, *, notes: Mapping[str, str] | None = None
) -> list[dict[str, str]] | None:
    """The enumerated evidence set the planning run is handed (issue #65), or
    ``None`` when there is no record of what was retrieved.

    **This is the whole two-step, in one function: which URLs the planning run
    may cite is decided by the runtime, not by the model.** The spine is
    ``annotations`` — Core's record of what ``:online`` actually returned,
    written independently of anything the model said (:class:`AgentRunView`,
    issue #82). ``notes`` is the grounding run's own reading, keyed by
    :func:`_url_key` and merged onto that spine, so:

    - a page the runtime recorded and the model wrote nothing about is still
      handed over — it was retrieved, so it is citable, it simply arrives with
      an empty reading;
    - a page the model wrote about but the runtime never recorded is **not**
      handed over at all, so it cannot be cited, and cannot then satisfy the
      per-recommendation grounding check by having been invented one step
      earlier. Trusting the model's account of what it read would reintroduce
      exactly the trust every other check in this module refuses.

    ``None`` in, ``None`` out: the tri-state is preserved rather than collapsed
    into an empty list, because "the runtime recorded nothing" and "the runtime
    recorded that retrieval found nothing" lead to opposite decisions in
    :func:`extract_channel_plan`.
    """
    if annotations is None:
        return None
    by_url = notes or {}
    handed: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in annotations:
        fields = _annotation_fields(entry)
        url = fields.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        item = {"url": url, "what_it_says": by_url.get(_url_key(url), "")}
        title = fields.get("title")
        if isinstance(title, str) and title:
            item["title"] = title
        content = fields.get("content")
        if isinstance(content, str) and content:
            item["excerpt"] = content[:EVIDENCE_EXCERPT_CHARS]
        handed.append(item)
    return handed


def _require_retrieved_evidence(
    plan: ChannelPlan, *, retrieved_source_urls: Collection[str]
) -> None:
    """Every recommendation must cite at least one URL **this run retrieved**
    (issue #65), or :class:`UngroundedRecommendationError`.

    See that class for why this is a different question from provenance and
    why it is asked per recommendation. Two things about the shape:

    - The comparison is on :func:`_url_key`, not the raw string, exactly as
      provenance is: a model that retypes a retrieved URL with a trailing
      slash has cited what it retrieved, and failing that would make the guard
      fire on honest runs — which is how a guard ends up switched off.
    - ``retrieved_source_urls`` being empty is not a special case here. It
      falls through to "every recommendation is ungrounded", which is exactly
      right — a run whose retrieval returned nothing cannot have grounded
      anything in it — and the message says which of the two happened, since
      "search found nothing" and "the search results never reached the plan"
      need different responses from an operator.
    """
    allowed = {_url_key(url) for url in retrieved_source_urls}
    ungrounded = [
        recommendation.channel_key or recommendation.suggested_label or "(unnamed channel)"
        for recommendation in plan.channels
        if not any(_url_key(source.url) in allowed for source in recommendation.sources)
    ]
    if not ungrounded:
        return

    named = ", ".join(ungrounded[:_MAX_REPORTED_UNCITED])
    if len(ungrounded) > _MAX_REPORTED_UNCITED:
        named += f" (and {len(ungrounded) - _MAX_REPORTED_UNCITED} more)"
    cause = (
        "this run's own search returned nothing, so there is no conversion evidence "
        "behind any of it"
        if not allowed
        else "they rest entirely on sources carried over from the approved positioning, "
        "which was researched to answer a different question"
    )
    raise UngroundedRecommendationError(
        f"The channel-plan run recommended {len(ungrounded)} channel"
        f"{'s' if len(ungrounded) != 1 else ''} with no evidence it retrieved itself "
        f"({named}): {cause}. Channel choice decides where the whole campaign goes, so "
        "nothing was produced — try running channel planning again."
    )


def extract_channel_plan(
    messages: list[dict[str, Any]],
    *,
    taxonomy: dict[str, Literal["organic", "paid"]],
    allowed_motions: Collection[str] | None = None,
    allowed_source_urls: Collection[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> ChannelPlan:
    """The channel-plan agent's output, or a hard failure — the same failure
    modes as :func:`extract_positioning`, for the same reason (M4,
    issue #3): a channel recommendation is exactly as fabricable as a
    positioning claim, and arguably the most confident-sounding one in the
    whole pipeline, because channel advice reads as generic wisdom whether or
    not anyone researched it.

    ``annotations`` here is the **grounding run's**, not the planning run's —
    see :func:`advance_channel_plan` for why that distinction is the whole
    guard rather than a detail of plumbing. Otherwise as
    :func:`extract_research_synthesis`.

    ## What provenance means for a stage that retrieves (issue #65 vs #22/#113)

    Since #65 this stage performs its own live retrieval, which breaks the
    assumption the provenance check was built on. ``positioning``,
    ``channel_plan`` and ``copy`` were guarded because their legitimate source
    set was **closed and already known**: exactly the approved parent's
    ``citations``. Citing evidence the parent did not contain was, by
    definition, fabrication. #65's whole purpose is to make this stage cite
    evidence the parent does not contain, so carrying that rule over unchanged
    would reject every grounded run — and simply dropping it would switch off
    the fabrication guard at the stage where fabrication is least visible.

    Neither. **The set stays closed; it stops being the parent alone.** A
    channel-plan run can legitimately have read exactly two things: the
    approved positioning it was handed, and whatever its own retrieval
    returned. The runtime records the second independently of the model, on
    ``annotations`` — that is the point of the column (#82) — so the union of
    the two is a set this repo can check against without trusting the model
    for any of it. A URL in neither was not read by anything, which is the
    same sentence :class:`UncitedSourceError` always meant.

    The tri-state is preserved and matters more here than anywhere else:

    - ``annotations`` non-empty — allowed set is ``allowed_source_urls`` ∪
      retrieved.
    - ``annotations == []`` — retrieval genuinely returned nothing, so the
      allowed set is the parent's alone, exactly as before #65.
    - ``annotations is None`` — there is **no record of what this run
      retrieved**, so no closed set exists to check against and the check is
      skipped rather than guessed at. Enforcing the parent-only set here would
      fail a genuinely grounded run whose annotations were not recorded, which
      is how a guard gets switched off for real; the module already treats
      "not known" this way for ``allowed_source_urls`` itself.

    Provenance alone is then *necessary and not sufficient*, which is the
    other half of #65: carried-over positioning sources satisfy it perfectly
    while answering the wrong question. So each recommendation must also cite
    at least one URL this run retrieved — :class:`UngroundedRecommendationError`,
    :func:`_require_retrieved_evidence` — asked only when retrieval is known,
    for the same reason.

    ``allowed_source_urls`` is the approved **positioning** artefact's
    citation URLs (issue #22) — see :func:`extract_positioning` for the full
    reasoning; this is that same check one stage further down, which is where
    the issue was filed from, now widened as described above.

    ``taxonomy`` is ``{channel_key: motion}`` for exactly the taxonomy this
    run was shown (#76 increment 2) — stored on the artefact at start time,
    not re-fetched, so this validates against what the agent actually saw
    rather than whatever the taxonomy happens to be *now*. Every
    recommendation naming a ``channel_key`` is checked against it
    (:class:`UnknownChannelError` on a miss — the agent inventing or
    mangling a key) and has its ``motion`` **overwritten** from the taxonomy:
    the agent stops asserting motion independently for a real channel, full
    stop, regardless of what it put in the field.

    ``allowed_motions`` is the campaign's own motion widened to channel
    motions (#67) — ``{"organic"}``, ``{"paid"}`` or both, from
    :func:`~marketing.definitions.motions_allowed_by`, stashed on the pending
    artefact at start time for exactly the reason ``taxonomy`` is. Anything
    outside it raises :class:`MotionNotAllowedError`. Two things are checked,
    for two different reasons:

    - a ``suggested_label`` proposal's own asserted motion — the ONE place
      the model still decides a motion, and therefore the only place the
      campaign's motion needs enforcing against the model at all;
    - the motion the taxonomy snapshot gives a ``channel_key`` — defence in
      depth against a caller that filtered its taxonomy wrongly, not against
      the model.

    ``None`` means "this run was not constrained", which is what every run
    started before #67 shipped actually was. It is deliberately not treated as
    "allow nothing" or defaulted to a motion: retro-fitting a rule onto a run
    that was never shown it would reject artefacts for a constraint that did
    not exist when they were produced — the same distinction
    ``allowed_source_urls=None`` already carries for #22.
    """
    retrieved = annotation_source_urls(annotations)
    if retrieved is None:
        # No record of this run's retrieval: the legitimate set is unknowable,
        # so neither evidence check can be adjudicated. Said out loud rather
        # than passed over in silence — a stage that is meant to ground and
        # cannot be shown to have grounded is exactly what #65 was filed
        # about, and the failure mode is invisible by construction.
        widened: Collection[str] | None = None
        logger.warning(
            "channel plan evidence checks skipped: the run reported no annotations, so "
            "there is no record of what it retrieved to check its sources against"
        )
    else:
        widened = [*(allowed_source_urls or []), *retrieved]

    plan = _extract_cited_artefact(
        messages,
        annotations=annotations,
        allowed_source_urls=widened,
        parent_description=(
            "the evidence this run actually had — the approved positioning it was "
            "given, plus what its own search returned —"
        ),
        tool_name=CHANNEL_PLAN_TOOL_NAME,
        model_cls=ChannelPlan,
        citation_groups=lambda r: [c.sources for c in r.channels],
        malformed_message=f"the channel-plan run produced no {CHANNEL_PLAN_TOOL_NAME} tool call",
        no_citations_message=(
            "The channel-plan run cited nothing at all — neither its own search results "
            "nor the approved positioning. Nothing was produced — try running channel "
            "planning again."
        ),
        retry_hint="channel planning",
    )
    if retrieved is not None:
        _require_retrieved_evidence(plan, retrieved_source_urls=retrieved)
    permitted = None if allowed_motions is None else frozenset(allowed_motions)
    for recommendation in plan.channels:
        if recommendation.channel_key is None:
            # A `suggested_label` proposal. `motion` is the agent's own — the
            # model validator already requires it — so this is where the
            # campaign's motion has to be enforced against the model.
            if permitted is not None and recommendation.motion not in permitted:
                raise MotionNotAllowedError(
                    f"The channel-plan run proposed {recommendation.suggested_label!r} as a "
                    f"{recommendation.motion} channel, but this campaign runs "
                    f"{'/'.join(sorted(permitted))} channels only."
                )
            continue
        if recommendation.channel_key not in taxonomy:
            raise UnknownChannelError(
                f"The channel-plan run referenced channel_key {recommendation.channel_key!r}, "
                "which was not in the taxonomy it was given."
            )
        motion = taxonomy[recommendation.channel_key]
        if permitted is not None and motion not in permitted:
            raise MotionNotAllowedError(
                f"The channel-plan run recommended {recommendation.channel_key!r}, a {motion} "
                f"channel, but this campaign runs {'/'.join(sorted(permitted))} channels only."
            )
        recommendation.motion = motion  # derived, not asserted
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
    # Last, and after the citation guards on purpose: length is the least
    # serious thing that can be wrong with a piece of copy, and a run that
    # fabricated a source should be reported as having fabricated a source, not
    # as having written a long headline. See :func:`measure_copy_length` for why
    # only the far end of this is fatal (issue #128).
    measure_copy_length(result.channels)
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


# ── Element identity + operator selection (issue #145) ──────────────────────
#
# Every artefact body is a LIST of proposed items — research findings,
# positioning segments/pillars/CTAs, channel recommendations, copy entries —
# and `approve_artefact_route` used to flip the whole body `proposed ->
# approved` in one move. An operator who wanted 8 of 9 channels had to accept
# the ninth or reject the whole run. The functions below are the machinery
# that makes a PARTIAL approval real: a stable id per element, a way to narrow
# a body down to an approved subset, and a way to recompute the closed
# citation set that subset actually supports.
#
# **Ids are server-assigned here, at persist time — never model-generated and
# never positional.** A model asked to invent a stable id will not reliably
# produce one; a positional id (an index into the list) silently reassigns an
# operator's earlier choice to whatever content now sits at that index the
# moment the list is reordered — the exact failure #94 already recorded once
# for pack link ordering. `with_element_ids` is called on every body this
# plugin writes (see `admin_app.py`, `channel_plan_routes.py`,
# `copy_routes.py` — every `"body": json.dumps(...)` site wraps its argument
# in this, `tests/test_marketing_element_id_call_sites.py` enforces it
# statically), so a future write site cannot forget to stamp ids just by
# following the pattern already on screen.

#: The list fields, across every artefact kind, that hold operator-selectable
#: elements. Field-name based rather than keyed by `kind`, deliberately: a
#: pending-state placeholder body (`{"channel_taxonomy": ..., ...}`) simply
#: has none of these keys, so every function below is a no-op on it, and a
#: stage added later that carries a new list of selectable things needs only
#: to be named here — not to teach every call site about a new `kind`.
ELEMENT_LIST_KEYS: tuple[str, ...] = ("findings", "segments", "pillars", "ctas", "channels")


def with_element_ids(body: Mapping[str, Any]) -> dict[str, Any]:
    """`body`, with a server-assigned, stable `id` stamped onto every
    selectable element it carries (issue #145).

    Idempotent: an element that already carries a truthy `id` keeps it, so
    calling this twice — or on a body that already went through it — never
    reassigns anything. Every other key in `body`, and every other key in
    each element dict, passes through unchanged; this only ever adds `id`.
    """
    stamped = dict(body)
    for key in ELEMENT_LIST_KEYS:
        items = stamped.get(key)
        if not isinstance(items, list):
            continue
        stamped[key] = [
            {**item, "id": item.get("id") or uuid.uuid4().hex} if isinstance(item, dict) else item
            for item in items
        ]
    return stamped


def known_element_ids(body: Mapping[str, Any]) -> set[str]:
    """Every element `id` present in `body` — what an approval route's
    requested `element_ids` is validated against (issue #145), so a stale
    page selecting a since-regenerated element is rejected rather than
    silently approving nothing."""
    ids: set[str] = set()
    for key in ELEMENT_LIST_KEYS:
        items = body.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            element_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(element_id, str) and element_id:
                ids.add(element_id)
    return ids


def selected_body(body: Mapping[str, Any], element_ids: Collection[str] | None) -> dict[str, Any]:
    """`body`, narrowed to only the elements named in `element_ids` — the
    downstream-facing read of an artefact's approval (issue #145).

    `element_ids=None` means "everything was approved"
    (`marketing_artefact.approved_selection` is `NULL`) — the
    backwards-compatible reading, and the one every artefact approved before
    this change has, since nothing on this deployment has ever written
    anything else. `body` is returned unfiltered in that case: byte-for-byte
    what every downstream stage already did.

    A non-`None` `element_ids` filters every known list of selectable
    elements (:data:`ELEMENT_LIST_KEYS`) down to the items whose `id` is in
    it. An item with no `id` at all — a legacy element, from a body persisted
    before this change and never re-approved since — cannot have been named
    in the selection, so it is dropped rather than kept.
    """
    if element_ids is None:
        return dict(body)
    wanted = set(element_ids)
    narrowed = dict(body)
    for key in ELEMENT_LIST_KEYS:
        items = narrowed.get(key)
        if not isinstance(items, list):
            continue
        narrowed[key] = [
            item for item in items if isinstance(item, dict) and item.get("id") in wanted
        ]
    return narrowed


def source_urls_from_body(body: Mapping[str, Any]) -> list[str]:
    """Every citation URL the elements actually present in `body` carry,
    first-seen order, deduplicated (issue #145) — the closed source set the
    NEXT stage down may cite from, recomputed fresh from whatever survived a
    selection rather than read off the parent's unfiltered `citations`
    column.

    **This is what makes provenance narrow WITH a selection rather than
    independently of it.** Called on a body already passed through
    :func:`selected_body`, a URL that was only ever cited by a dropped
    element is not in the result — but a URL a *surviving* element also
    cites still is, because this is a fresh union over what `body` actually
    contains, never a subtraction of the dropped element's own sources from
    the parent's full citation set. Subtracting would be wrong the moment two
    elements cite the same source: dropping one must not revoke a citation a
    kept element still legitimately relies on.
    """
    urls: list[str] = []
    for key in ELEMENT_LIST_KEYS:
        items = body.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for source in item.get("sources") or []:
                url = source.get("url") if isinstance(source, dict) else None
                if isinstance(url, str) and url and url not in urls:
                    urls.append(url)
    return urls


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

    ``definition_snapshot`` is Core's ``AgentRunResponse.definition_snapshot``:
    the configuration this run **actually ran with**, as opposed to the one
    this repo declares. For every stage this plugin starts itself the two are
    the same thing by construction. For the one stage it does not — research
    synthesis, fired by the orchestration engine from a config frozen into the
    seeded workflow — they are two independent documents that drifted apart
    for two days without anything noticing (issue #160), which is why the
    snapshot is carried here at all. ``None`` means "not known": a view built
    from a list row rather than the detail endpoint, or a Core too old to
    return the field. It never means "matches".
    """

    id: str
    status: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] | None = None
    definition_snapshot: dict[str, Any] | None = None

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


def _annotation_fields(entry: Any) -> Mapping[str, Any]:
    """The fields of one ``annotations`` entry, whichever shape it arrived in.

    Core stores what the runtime recorded, and OpenRouter's ``url_citation``
    annotation has been seen both flattened (``{"type": "url_citation",
    "url": ...}`` — the shape every fixture in this repo uses and the shape
    Core writes today) and nested (``{"url_citation": {"url": ...}}``, the
    upstream OpenAI-compatible form). Reading only one of them would make the
    breadth measurement below silently report zero the day the other appears,
    which is exactly the kind of quiet blindness issue #101 was filed about.

    Split out from :func:`_annotation_url` when the two-step started carrying
    ``title`` and page text forward as well (issue #65): one place that knows
    which shape an annotation is in, rather than one per field.
    """
    if not isinstance(entry, dict):
        return {}
    if isinstance(entry.get("url"), str) and entry["url"]:
        return entry
    nested = entry.get("url_citation")
    return nested if isinstance(nested, dict) else entry


def _annotation_url(entry: Any) -> str | None:
    """The URL inside one ``annotations`` entry — see :func:`_annotation_fields`
    for why the entry's shape is not assumed."""
    url = _annotation_fields(entry).get("url")
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


#: The run sets whose breadth this process has already reported (issue #65
#: follow-up), and the bound past which it forgets the oldest of them.
#:
#: **The line is once per run; the function that emits it is called once per
#: poll.** Reading is what advances this pipeline, so a terminal run that
#: cannot be advanced past — a channel plan whose recommendations were refused,
#: a research chain that failed — is re-read, and re-measured, for as long as
#: anyone leaves the page open. Measured live on 2026-08-14: the identical
#: breadth line at 13:26:22, :24, :26, :28 and :30. Nothing was wrong with the
#: numbers; the count of lines simply stopped meaning the count of runs, which
#: is the one thing an instrument like this is read for.
#:
#: What this does and does not promise, stated plainly because it is process
#: state in a Lambda: within one warm container, one line per run set. A cold
#: start, or a run set evicted by the bound below, produces a second line for a
#: run already reported — a duplicate, never a wrong measurement. The
#: alternative (persisting "already logged" on the artefact) would buy exactness
#: for a Core write on a failure path, which is a worse trade for a log line.
_BREADTH_REPORTED: set[str] = set()
_BREADTH_REPORTED_MAX = 1024


def _breadth_already_reported(
    *, chain_id: str | None, stage: str, views: Collection[AgentRunView | None]
) -> bool:
    """True when this exact run set's breadth has already been logged here.

    Keyed on the runs measured, not on the chain alone: a stage measures its
    own runs, and two stages of one campaign legitimately report separately.
    """
    key = f"{stage}:{chain_id}:" + ",".join(sorted(v.id for v in views if v is not None))
    if key in _BREADTH_REPORTED:
        return True
    if len(_BREADTH_REPORTED) >= _BREADTH_REPORTED_MAX:
        _BREADTH_REPORTED.clear()
    _BREADTH_REPORTED.add(key)
    return False


def _log_evidence_profile(
    *, chain_id: str | None, views: Collection[AgentRunView | None], stage: str = "research"
) -> None:
    """Record this chain's retrieval breadth where an operator investigating a
    thin artefact will find it, without failing the run over it.

    Not a guard, on purpose. Thin, homepage-only retrieval is a property of
    what the provider returned, not of anything the model did wrong, so
    failing on it would strand a campaign on a condition re-running cannot
    fix — see this module's docstring on why the citation guards fail closed
    and this one does not.

    "Without failing the run over it" is load-bearing on the failure paths
    this is also called from (issue #101 follow-up): a report that raises
    while reporting on a failure replaces a diagnosable error with a spurious
    one, so every step here is wrapped. Nothing this function can do is worth
    more than the exception the caller is already carrying.

    ``stage`` names which stage's retrieval is being measured (issue #65).
    It defaults to ``research`` because that is the stage this instrument was
    built for and every existing filter is written against — the message
    prefix and the structured keys are unchanged for it, byte for byte.
    Channel planning retrieves too now, and "is the channel plan grounded?" is
    the same question about the same kind of evidence: #101 was settled only
    because somebody measured research's retrieval by hand and then
    instrumented it, and asking the new question without the instrument buys
    the same day of archaeology a second time. One implementation rather than
    a near-copy, so a fix to the tri-state reasoning below cannot apply to one
    stage and not the other.

    Emitted **once per run set**, not once per call: this function is reached
    from a polled advance, and a terminal run that cannot be advanced past is
    re-read for as long as an operator leaves the page open. See
    :data:`_BREADTH_REPORTED` for what that de-duplication does and does not
    promise.
    """
    if _breadth_already_reported(chain_id=chain_id, stage=stage, views=views):
        return
    label = stage.replace("_", " ")
    try:
        profile = evidence_profile(views)
    except Exception:  # pragma: no cover - defensive; a report must not raise
        logger.warning(
            "%s retrieval breadth: not measured — the profile could not be computed",
            label,
            exc_info=True,
            extra={"causation_id": chain_id},
        )
        return

    if profile.runs_measured == 0:
        # No view carried `annotations` at all. That is "we do not know what
        # was retrieved", not "nothing was retrieved" — the same tri-state
        # `evidence_profile` keeps, and worth keeping here because this is the
        # branch the all-runs-failed path usually lands on. Emitting
        # `0 distinct URLs across 0 grounded run(s)` would read as a measured,
        # catastrophic zero and send an operator hunting a retrieval outage
        # that never happened. The counts are `None`, not `0`, for the same
        # reason: a filter on this field must not be able to average unknowns
        # in as zeroes.
        logger.info(
            "%s retrieval breadth: not measured — no %s run reported "
            "annotations (%d run view(s) seen)",
            label,
            label,
            len(views),
            extra={
                "causation_id": chain_id,
                f"{stage}_runs_measured": 0,
                f"{stage}_distinct_urls": None,
                f"{stage}_shared_urls": None,
                f"{stage}_deep_pages": None,
                f"{stage}_site_roots": None,
            },
        )
        return

    logger.info(
        "%s retrieval breadth: %d distinct URLs across %d grounded run(s) "
        "(%d shared by more than one, %d deep pages, %d site roots)",
        label,
        profile.distinct_urls,
        profile.runs_measured,
        profile.shared_urls,
        profile.deep_pages,
        profile.site_roots,
        extra={
            "causation_id": chain_id,
            f"{stage}_runs_measured": profile.runs_measured,
            f"{stage}_distinct_urls": profile.distinct_urls,
            f"{stage}_shared_urls": profile.shared_urls,
            f"{stage}_deep_pages": profile.deep_pages,
            f"{stage}_site_roots": profile.site_roots,
        },
    )


async def _log_evidence_profile_for(
    gateway: AgentGateway, *, chain_id: str, research_run_ids: Collection[str]
) -> None:
    """Fetch the research run views purely to report breadth, and swallow
    anything that goes wrong fetching them.

    Only for the paths that are already raising. On the success path the views
    are fetched anyway (the citation guard needs them) and a gateway error
    there is a real error worth propagating; here the caller is about to raise
    a `RunNotSucceededError` that describes the actual problem, and losing it
    to a gateway hiccup while gathering a *log line* would be a strictly worse
    outcome than having no log line.
    """
    try:
        views = [await gateway.get_agent_run(run_id=rid) for rid in research_run_ids]
    except Exception:  # pragma: no cover - defensive; a report must not raise
        logger.warning(
            "research retrieval breadth: not measured — the research run views could not be read",
            exc_info=True,
            extra={"causation_id": chain_id},
        )
        return
    _log_evidence_profile(chain_id=chain_id, views=views)


#: The exact command that re-seeds the workflow definition, quoted verbatim
#: anywhere this repo reports drift. Anyone reading a drift report is one
#: command away from fixing it and should never have to go and find which one.
RESEED_COMMAND = "uv run python scripts/seed_fan_in_workflow.py --replace"

#: The keys of the frozen copy this plugin can compare a *run* against.
#:
#: Not "the model", which is how #62 recorded the lesson and why the timeout
#: went unnoticed for two more days: **the whole snapshot is frozen**, so the
#: subject of the check is every key of the run definition, enumerated from
#: ``research_synthesis_definition``'s own output rather than listed here.
#: ``tools`` and ``output_tools`` are compared too — a stale tool schema is
#: the same class of defect and would be even quieter.
SYNTHESIS_DRIFT_IGNORED_KEYS = frozenset({"output_tools"})


def _drift_summary(value: Any) -> Any:
    """A long string reduced to something a log line can carry.

    The synthesis instructions are thousands of characters; a drift report
    that pasted two copies of them would be unreadable and would push the
    thing that actually differs off the end of the line.
    """
    if isinstance(value, str) and len(value) > 80:
        return f"{value[:60]}… ({len(value)} chars)"
    return value


def synthesis_config_drift(
    snapshot: Mapping[str, Any] | None,
    *,
    synthesis_model: str = DEFAULT_SYNTHESIS_MODEL,
) -> dict[str, tuple[Any, Any]]:
    """What the research-synthesis run **actually ran with**, against what this
    repo declares — ``{key: (deployed, declared)}``, empty when they agree.

    This is the guard for issue #160, and the document it reads matters more
    than the comparison it makes. The synthesis stage is the one stage this
    plugin does not start: the orchestration engine fires it from a copy of
    the run definition that ``scripts/seed_fan_in_workflow.py`` froze into the
    workflow's ``action_config`` at seed time. A test that reads
    ``definitions.py`` and asserts something about ``definitions.py`` proves
    nothing about that copy — it is biffo-template#1362's class, a guard
    reading a different document from the one that acts, and it is exactly the
    check that would have stayed green through all of #160.

    So the subject here is ``AgentRunView.definition_snapshot``: the
    configuration Core recorded on the run it created, which is what the
    runtime read and billed. If that disagrees with
    ``research_synthesis_definition``, the deployed workflow is stale and the
    only fix is :data:`RESEED_COMMAND`.

    ``None`` means the snapshot is not known (an older Core, or a view built
    from a list row) and yields ``{}`` — the caller must not read that as
    "in step"; it is "not checked". A key missing from the snapshot is a
    drift with a deployed value of ``None``, because absent is precisely how
    ``timeout_seconds`` produced the 120s clock #160 measured: the runtime
    silently substitutes its own default for a key nobody wrote.
    """
    if snapshot is None:
        return {}
    declared = research_synthesis_definition(
        model=synthesis_model, instructions=RESEARCH_SYNTHESIS_INSTRUCTIONS
    )
    drift: dict[str, tuple[Any, Any]] = {}
    for key, declared_value in declared.items():
        if key in SYNTHESIS_DRIFT_IGNORED_KEYS:
            continue
        deployed_value = snapshot.get(key)
        if deployed_value != declared_value:
            drift[key] = (_drift_summary(deployed_value), _drift_summary(declared_value))
    return drift


def _report_synthesis_config_drift(
    view: AgentRunView, *, chain_id: str, synthesis_model: str
) -> dict[str, tuple[Any, Any]]:
    """Log — never raise — when the run the engine fired was configured from a
    stale copy of this repo.

    Deliberately not an exception. The run has already happened and has
    already been paid for; refusing its output over a configuration mismatch
    would turn a reporting problem into an outage, and the operator would lose
    a campaign to a stale workflow rather than merely running one on the wrong
    model. It is logged at ``error`` because it is a deployment defect that
    needs a person, not a condition the pipeline can recover from.

    Reported on **every terminal path**, including failure — a run that died
    on a 120s clock its declaration says should be 240s is the case where this
    line is worth the most, and that run is a failure, not a success.
    """
    try:
        drift = synthesis_config_drift(view.definition_snapshot, synthesis_model=synthesis_model)
    except Exception:  # pragma: no cover — defensive; a report must not raise
        logger.warning(
            "synthesis config drift: not checked — the run's definition snapshot could not be read",
            exc_info=True,
            extra={"causation_id": chain_id},
        )
        return {}
    if not drift:
        return {}
    logger.error(
        "The seeded fan-in workflow is STALE: the research-synthesis run ran a "
        "configuration this repo no longer declares. The engine fires this stage "
        "from a copy frozen into the workflow's action_config at seed time, so "
        "nothing in a deploy updates it. Re-seed with: %s",
        RESEED_COMMAND,
        extra={
            "causation_id": chain_id,
            "agent_run_id": view.id,
            "reseed_command": RESEED_COMMAND,
            "synthesis_config_drift": {
                key: {"deployed": deployed, "declared": declared}
                for key, (deployed, declared) in drift.items()
            },
        },
    )
    return drift


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
    result. ``synthesis_model`` used to be accepted for parity with the other
    stage starters and then discarded (``del synthesis_model``), because this
    stage never *starts* a run — the engine does. It is now the model this
    stage is **supposed** to run on, and the run's own
    ``definition_snapshot`` is checked against it
    (:func:`synthesis_config_drift`): the engine fires synthesis from a copy
    of that definition frozen into the seeded workflow, and for two days that
    copy ran ``claude-opus-4.8`` on a 120s clock while this repo declared
    ``claude-opus-5`` on 240s with nothing anywhere reporting it (#160). A
    parameter that is accepted and discarded is how a stage ends up with no
    guard at all.

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

    It is emitted on **every terminal path**, not just the successful one: the
    synthesis run having failed, and every research run having failed, are the
    two states in which an operator most needs it. "The research was thin" and
    "the synthesis failed" produce the same dead artefact, and this line is the
    only thing that separates them — so logging it only on success withheld it
    exactly when it was being asked for. It is *not* emitted while the chain is
    still in flight, because this function is polled and the line would then be
    a stream of partial snapshots rather than one measurement per chain.
    """
    synthesis_run = await gateway.find_chain_run(
        chain_id=chain_id, agent_name=RESEARCH_SYNTHESIS_AGENT_NAME
    )
    if synthesis_run is not None:
        if not synthesis_run.is_terminal:
            return None  # synthesis is running; nothing to do yet
        # The engine chose this run's configuration, from a copy of ours frozen
        # at seed time — so `synthesis_model` is not the model that ran, it is
        # the model that *should* have (issue #160). Compare them and say so.
        _report_synthesis_config_drift(
            synthesis_run, chain_id=chain_id, synthesis_model=synthesis_model
        )
        if not synthesis_run.succeeded:
            await _log_evidence_profile_for(
                gateway, chain_id=chain_id, research_run_ids=research_run_ids
            )
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
    # Terminal, and none succeeded. The views are already in hand, so report
    # breadth off them directly rather than re-fetching: a failed run can still
    # have retrieved before it died, and "what did retrieval return?" is the
    # whole question on this path.
    _log_evidence_profile(chain_id=chain_id, views=views)
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


def channel_plan_input(
    *,
    positioning_body: dict[str, Any],
    taxonomy: list[dict[str, Any]],
    campaign_motion: str,
) -> dict[str, Any]:
    """Everything the planning run needs beyond the evidence itself.

    One definition of the shape, two users: :func:`start_channel_evidence`'s
    caller stashes this on the pending artefact at start time, and
    :func:`advance_channel_plan` spreads it into the planning run's payload one
    or more polls later. Stashed rather than re-fetched for the reason
    ``channel_taxonomy`` already is — it must be what THIS stage was started
    against, not whatever the positioning or the taxonomy has been changed to
    by the time the grounding run finishes.
    """
    return {
        "positioning": positioning_body,
        "channel_taxonomy": taxonomy,
        "campaign_motion": campaign_motion,
    }


@dataclass(frozen=True)
class ChannelPlanAdvance:
    """What one poll of the two-step channel stage did (issue #65).

    Three states, and the caller has to be able to tell them apart because one
    of them requires it to persist something:

    - ``plan is None``, ``started_plan_run_id is None`` — nothing to do yet.
      The grounding run or the planning run is still in flight.
    - ``started_plan_run_id`` set — the grounding run finished and its evidence
      has just been handed to a freshly-started planning run. **The caller must
      record that id**, or the next poll starts a second planning run and pays
      for it: "the grounding run is terminal" is true on every subsequent poll,
      and only the recorded id makes the transition happen once.
    - ``plan`` set — the stage is finished and this is the artefact.
    """

    plan: ChannelPlan | None = None
    started_plan_run_id: str | None = None


async def start_channel_evidence(
    gateway: AgentGateway,
    *,
    positioning_body: dict[str, Any],
    taxonomy: list[dict[str, Any]],
    campaign_motion: str,
    channel_evidence_model: str = DEFAULT_CHANNEL_EVIDENCE_MODEL,
) -> tuple[str, str]:
    """Start the channel stage by starting its **grounding** run (M4, issues
    #3/#65), given the *approved* positioning artefact's body AND the channels
    this campaign may plan against as input.

    ## Why the stage starts with a run that recommends nothing

    Until now this stage was one ``:online`` run that retrieved and decided in
    the same turn, and on tabsii dev, 2026-08-14, that run retrieved **ten deep
    pages, zero site roots** — and then justified all six of its
    recommendations with sources carried over from the approved positioning.
    :class:`UngroundedRecommendationError` refused them, correctly.

    The gap that leaves is citation behaviour, not grounding. ``:online``
    injects retrieved pages into the context and records them on
    ``annotations``, but nothing in the payload ever presented those URLs to
    the model *as a set to cite from* — while the positioning's sources arrive
    as structured ``Source`` objects it can see and copy. It cited the list it
    could see.

    So retrieval and structuring are two runs. This one grounds and reads;
    :func:`advance_channel_plan` hands what the **runtime** recorded to a
    second run as an enumerated list. Sequential rather than fanned out, and
    deliberately so: research's fan-out exists to wait for N siblings and fans
    in through the orchestration engine, which passes the children's *outputs*
    — it has no way to pass ``annotations``, which is the one input here that
    is worth having precisely because the model did not write it. There is also
    only one sibling to wait for. Reading is what advances this pipeline
    already (``admin_app._advance_artefact``), so the second run is started
    from the poll that observes the first finishing — no second orchestration
    pattern, no second seeded workflow, and one ``causation_id`` across both so
    spend and the breadth line still join.

    ``taxonomy`` (#76 increment 2) is a list of
    ``{channel_key, label, motion, category}`` dicts — the tenant's
    ``marketing_channel`` rows, fetched by the caller — so the agent can pick
    real, stable ids rather than inventing free text the copy stage and
    ``marketing_link`` would then have to trust exactly (#75). Since #67 the
    caller narrows those rows to the operator's own selection for this
    campaign, intersected with the campaign's motion, BEFORE passing them
    here: the selection and the motion are constraints on what the run may
    produce, and the cheapest place to enforce a constraint on output is the
    input, where the excluded options are simply not present to be chosen.
    The caller is also responsible for keeping a ``{channel_key: motion}``
    copy of this same narrowed taxonomy to validate against later
    (:func:`extract_channel_plan`'s ``taxonomy`` parameter) — passed there
    rather than re-derived, because it must be exactly what THIS run was
    shown, not whatever the taxonomy or the selection has grown to by the
    time the run completes.

    ``campaign_motion`` is passed to the agent as well as applied to the
    taxonomy, so it does not spend output on recommendations that would be
    rejected. That is an efficiency, not the enforcement — see
    :func:`extract_channel_plan` for where the motion is actually enforced.

    Mirrors :func:`start_positioning` otherwise: its own chain, started here
    rather than discovered, because nothing fans in on it.
    """
    causation_id = str(uuid.uuid4())
    run_id = await gateway.request_agent_run(
        agent_name=CHANNEL_EVIDENCE_AGENT_NAME,
        definition=channel_evidence_definition(
            model=channel_evidence_model,
            instructions=CHANNEL_EVIDENCE_INSTRUCTIONS,
        ),
        output_tool=channel_evidence_tool_schema(),
        # `search_query` FIRST, exactly as `start_research`'s payload is and
        # for exactly the same reason (issue #101, now #65): this is an
        # `:online` run, so the provider searches from the payload BEFORE the
        # model is invoked, and the payload is therefore the only place this
        # stage can decide what its retrieval is about. Leading with
        # `positioning` would retrieve the positioning question all over
        # again — the audience/competitive evidence this stage already has
        # too much of — instead of the conversion question it exists to ask.
        input_payload={
            "search_query": channel_plan_search_query(
                positioning_body=positioning_body,
                taxonomy=taxonomy,
                campaign_motion=campaign_motion,
            ),
            **channel_plan_input(
                positioning_body=positioning_body,
                taxonomy=taxonomy,
                campaign_motion=campaign_motion,
            ),
        },
        causation_id=causation_id,
    )
    return causation_id, run_id


async def advance_channel_plan(
    gateway: AgentGateway,
    *,
    evidence_run_id: str,
    taxonomy: dict[str, Literal["organic", "paid"]],
    plan_run_id: str | None = None,
    plan_input: Mapping[str, Any] | None = None,
    causation_id: str | None = None,
    allowed_motions: Collection[str] | None = None,
    allowed_source_urls: Collection[str] | None = None,
    channel_plan_model: str = DEFAULT_CHANNEL_PLAN_MODEL,
) -> ChannelPlanAdvance:
    """Drive the two-step channel stage forward by one poll (issue #65).

    Three things can happen, and :class:`ChannelPlanAdvance` says which:
    nothing yet, the planning run has just been started (**the caller must
    persist its id**), or the plan is finished.

    ``evidence_run_id`` is the grounding run :func:`start_channel_evidence`
    started; ``plan_run_id`` is the planning run this function started on an
    earlier poll, passed back from wherever the caller stashed it.
    ``plan_input`` is :func:`channel_plan_input`'s dict, stashed at start time
    for the reason that function's docstring gives. ``taxonomy`` is
    ``{channel_key: motion}`` for exactly the taxonomy the stage was started
    against, and ``allowed_motions`` the campaign motion it was started under
    (#67) — see :func:`extract_channel_plan` for how they and
    ``allowed_source_urls`` are used.

    ## The guard's subject is the run that RETRIEVED

    ``annotations`` is read off the **grounding** run and handed to
    :func:`extract_channel_plan`, never off the planning run. This is not a
    convenience: the planning run is not an ``:online`` run, so its own
    ``annotations`` can only ever be the "not a grounded run" state — which
    skips both evidence checks. Reading it would leave
    :class:`UngroundedRecommendationError` structurally unable to fire, without
    a line of it being deleted. That is exactly the defect issue #90 found in
    :func:`advance_research`, where the guard was reading the synthesis run
    rather than the research runs that retrieved, and
    :func:`_aggregate_research_annotations` is the same fix one stage up.

    The grounding run's breadth is measured and logged on every terminal path,
    including the failed one, which is where the measurement is worth most:
    "the retrieval was thin" and "the run died" produce the same dead artefact
    and this line is the only thing that separates them. It is a report, never
    a gate. ``causation_id`` is carried only so it can be joined to the rest of
    the campaign's runs, and is what the planning run is started on.
    """
    evidence_view = await gateway.get_agent_run(run_id=evidence_run_id)
    if evidence_view is None or not evidence_view.is_terminal:
        return ChannelPlanAdvance()
    # Once, here, before any branch: the grounding run is the only run in this
    # stage that retrieves, so it is the only one there is a breadth to measure
    # for — and it is measured on the success and failure paths alike, never
    # while it is still in flight.
    _log_evidence_profile(chain_id=causation_id, views=[evidence_view], stage="channel_plan")
    if not evidence_view.succeeded:
        raise RunNotSucceededError("The channel-plan evidence run did not complete successfully.")

    if plan_run_id is None:
        evidence_set = extract_channel_evidence(evidence_view.messages)
        handed = retrieved_evidence(
            evidence_view.annotations,
            notes={_url_key(item.url): item.note for item in evidence_set.evidence},
        )
        if handed is None:
            # No record of what was retrieved. The planning run still runs —
            # stranding a campaign because the runtime did not write a column
            # is the failure mode this module refuses everywhere else — but it
            # is handed nothing to cite, and `extract_channel_plan` will say
            # out loud that it cannot adjudicate the result.
            logger.warning(
                "channel plan evidence hand-over is empty: the grounding run reported no "
                "annotations, so there is no record of what it retrieved to hand on",
                extra={"causation_id": causation_id},
            )
            handed = []
        started = await gateway.request_agent_run(
            agent_name=CHANNEL_PLAN_AGENT_NAME,
            definition=channel_plan_definition(
                model=channel_plan_model, instructions=CHANNEL_PLAN_INSTRUCTIONS
            ),
            output_tool=channel_plan_tool_schema(),
            # `retrieved_evidence` FIRST, for the reason `search_query` leads
            # the grounding run's payload: JSON key order is preserved, and the
            # evidence is the thing this run decides and cites from. Leading
            # with `positioning` is how the single-run stage came to justify
            # every recommendation from the positioning's sources.
            input_payload={"retrieved_evidence": handed, **(plan_input or {})},
            causation_id=causation_id or str(uuid.uuid4()),
        )
        return ChannelPlanAdvance(started_plan_run_id=started)

    plan_view = await gateway.get_agent_run(run_id=plan_run_id)
    if plan_view is None or not plan_view.is_terminal:
        return ChannelPlanAdvance()
    if not plan_view.succeeded:
        raise RunNotSucceededError("The channel-plan run did not complete successfully.")
    return ChannelPlanAdvance(
        plan=extract_channel_plan(
            plan_view.messages,
            taxonomy=taxonomy,
            allowed_motions=allowed_motions,
            allowed_source_urls=allowed_source_urls,
            annotations=evidence_view.annotations,
        )
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


def channel_key_motions(
    channel_plan_body: dict[str, Any],
) -> dict[str, Literal["organic", "paid"]]:
    """``{channel_key: motion}`` for the entries that carry a key. Never raises.

    Split out of :func:`channel_plan_channel_map` for issue #152. That function
    answers TWO questions at once — "is this plan stale?" and "what are its
    keyed channels?" — and #145 made answering both from the same body wrong.

    Staleness is a fact about the plan **as generated**: a pre-taxonomy plan
    (#76) has entries and none carry a ``channel_key``. That was a sound proxy
    while callers always passed the whole approved plan. Once an operator can
    approve a SUBSET, a perfectly current plan narrowed to only a
    ``suggested_label``-only proposal has exactly the same shape — entries, no
    keys — and the operator was told their valid plan "predates channel
    taxonomy ids" and to re-run it. Wrong advice, and no route to copy for the
    entry they actually chose.

    So callers ask the staleness question of the full body and take the mapping
    from the narrowed one. This helper is the second half, with no opinion
    about staleness.
    """
    return {
        c["channel_key"]: c["motion"]
        for c in (channel_plan_body.get("channels") or [])
        if c.get("channel_key")
    }


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
    mapping = channel_key_motions(channel_plan_body)
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
