"""The campaign studio's vocabulary, in one place.

Every constant here is a value the manifest, the service layer, the admin UI and
the eventual renderer all have to agree on. Defining them once and serving them
to the UI from ``admin_app.config`` is what stops a form offering a placement the
renderer cannot produce — a mismatch that is invisible until somebody submits.

Deliberately plain tuples rather than enums: these travel into ``Text`` columns
(plugin tables have no enum type) and into JSON for the UI, and an enum would be
converted at every boundary for no gain.
"""

from __future__ import annotations

#: What an operator can ask a campaign to produce.
#:
#: Selected per campaign rather than fixed, because the choice drives which
#: agents run, how long the campaign takes and what it costs — video especially.
#: ``copy`` is always available and effectively free; the rest are opt-in.
MEDIA_KINDS: tuple[str, ...] = ("copy", "image", "video", "audio")

#: The form factors a channel actually wants.
#:
#: These are produced by **deterministic render** from one approved creative,
#: never by generating again. Regenerating per placement makes the variants
#: drift from each other, costs N times as much, and reintroduces a
#: hallucination surface on every crop. Agents decide; renderers produce.
PLACEMENTS: tuple[str, ...] = ("feed_1x1", "portrait_4x5", "story_9x16")

#: The campaign lifecycle.
#:
#: Each agentic stage ends at a human approval gate, which is not ceremony: the
#: operator's name goes on the post, and agentic output that reaches publish
#: unreviewed is how brands get embarrassed. The gates are also what remove the
#: need for a long-running orchestrator — each is a natural suspend point, so the
#: pipeline is a resumable state machine over completed agent runs.
PIPELINE_STAGES: tuple[str, ...] = (
    "draft",
    "researching",
    "planned",
    "generating",
    "ready",
    "live",
    "archived",
)

#: Artefact kinds, in the order the pipeline produces them.
ARTEFACT_KINDS: tuple[str, ...] = ("research", "positioning", "channel_plan")

#: Approval states for an artefact.
#:
#: ``proposed`` and ``approved`` are distinct on purpose: an agent's output that
#: nobody has looked at must never be usable by the next stage, or the gates are
#: decorative.
ARTEFACT_STATUSES: tuple[str, ...] = ("pending", "proposed", "approved", "rejected")
