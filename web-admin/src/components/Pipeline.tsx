import { useEffect, useState, type ReactNode } from 'react'

import {
  approveArtefact,
  getArtefact,
  parseArtefactBody,
  rejectArtefact,
  startChannelPlan,
  startCopy,
  startPositioning,
  startResearch,
  StageRunFailedError,
  StaleArtefactError,
  type Artefact,
  type ArtefactKind,
  type ChannelPlanBody,
  type CopySetBody,
  type PositioningBody,
  type ResearchSynthesisBody,
} from '../lib/api'
import { elementsOf, type ElementSelection } from '../lib/elementSelection'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { POLL_CEILING_MS, usePendingPolling } from '../lib/usePendingPolling'
import { ChannelPlanArtefact, CopyArtefact, PositioningArtefact, ResearchArtefact } from './ArtefactBody'
import { PipelineStage } from './PipelineStage'

interface StageState {
  artefact: Artefact | null
  loading: boolean
  error: string | null
  /** See `PipelineStage`'s own doc — true only for an unknown-element-id 422
   * from `approveArtefact` (issue #145), never for any other failure this
   * stage can report. */
  stale: boolean
  /** True only for a `StageRunFailedError` — the advance route's 502 (issue
   * #159), meaning this stage's agent run reached a terminal state and what
   * it produced cannot be turned into an artefact. Distinct from `error`,
   * which is any failure at all: a stage can legitimately show an error
   * (a 403, a network blip during a poll) while its run is still perfectly
   * alive, and offering to re-run in that case would bill for a second agent
   * run to fix something no agent caused. */
  failed: boolean
  busy: boolean
  /** Element ids the operator has unchecked for this stage's current
   * `proposed` artefact (issue #145) — reset to empty the moment a NEW
   * artefact (a different `id`) arrives for this stage, so a selection made
   * against a previous run's elements can never silently carry over onto a
   * fresh one's. */
  deselected: Set<string>
}

interface Gate {
  canStart: boolean
  blockedReason: string | null
}

/** `ok`, and the one reason to show when it is not — collapses what would
 * otherwise be a parallel `x ? null : '...'` ternary at every gate. */
function reason(ok: boolean, blockedReason: string): Gate {
  return { canStart: ok, blockedReason: ok ? null : blockedReason }
}

/** Whether each stage's artefact has cleared its approval gate — the only
 * thing every `gate()` below reads. */
interface Approved {
  research: boolean
  positioning: boolean
  channel_plan: boolean
}

interface StageConfig {
  kind: ArtefactKind
  title: string
  start: (campaignId: string) => Promise<Artefact>
  /** The stage's real output, once it has one — never called for a `null`
   * or still-`pending` artefact (see `Pipeline`'s render loop). `channelLookup`
   * is only read by the channel-plan and copy stages; research and
   * positioning's renderers simply ignore the second argument. `selection`
   * (issue #145) is passed straight through to the `ArtefactBody` renderer,
   * which is the one place that actually knows how to draw a checkbox next
   * to each of ITS shape's elements. */
  renderBody: (artefact: Artefact, channelLookup: ChannelLookup, selection: ElementSelection) => ReactNode
  gate: (approved: Approved, ready: Readiness) => Gate
}

/** The campaign-level facts a gate reads that are not another stage's
 * approval — both of them decisions the operator has to have taken before
 * the stage's own route will accept a start. */
interface Readiness {
  hasBrief: boolean
  /** #67: the campaign has a motion AND at least one target channel.
   * `start_channel_plan_route` 422s without them, so offering the button
   * would only ever produce that error. */
  hasTargets: boolean
}

/** Everything that varies per stage, in one place — a fifth stage is one new
 * entry here, not four disjoint edits across a starter map, a body-render
 * switch and a gate map with nothing enforcing they stay in step. */
const STAGES: StageConfig[] = [
  {
    kind: 'research',
    title: 'Research',
    start: startResearch,
    renderBody: (artefact, _channelLookup, selection) => {
      const body = parseArtefactBody<ResearchSynthesisBody>(artefact)
      return body !== null ? (
        <ResearchArtefact body={body} selection={selection} />
      ) : (
        <p className="empty">No content to show.</p>
      )
    },
    gate: (_approved, ready) =>
      reason(
        ready.hasBrief,
        'This campaign has no brief yet — add one above before starting research.',
      ),
  },
  {
    kind: 'positioning',
    title: 'Positioning',
    start: startPositioning,
    renderBody: (artefact, _channelLookup, selection) => {
      const body = parseArtefactBody<PositioningBody>(artefact)
      return body !== null ? (
        <PositioningArtefact body={body} selection={selection} />
      ) : (
        <p className="empty">No content to show.</p>
      )
    },
    gate: (approved) => reason(approved.research, 'Approve the research stage first.'),
  },
  {
    kind: 'channel_plan',
    title: 'Channel plan',
    start: startChannelPlan,
    renderBody: (artefact, channelLookup, selection) => {
      const body = parseArtefactBody<ChannelPlanBody>(artefact)
      return body !== null ? (
        <ChannelPlanArtefact body={body} channelLookup={channelLookup} selection={selection} />
      ) : (
        <p className="empty">No content to show.</p>
      )
    },
    gate: (approved, ready) => {
      // Targeting is named FIRST when both are outstanding: it is the one
      // the operator can act on right now, without waiting for a run.
      if (!ready.hasTargets) {
        return reason(
          false,
          'Choose this campaign\u2019s motion and target channels above before planning.',
        )
      }
      return reason(approved.positioning, 'Approve the positioning stage first.')
    },
  },
  {
    kind: 'copy',
    title: 'Copy',
    start: startCopy,
    renderBody: (artefact, channelLookup, selection) => {
      const body = parseArtefactBody<CopySetBody>(artefact)
      return body !== null ? (
        <CopyArtefact body={body} channelLookup={channelLookup} selection={selection} />
      ) : (
        <p className="empty">No content to show.</p>
      )
    },
    gate: (approved) => {
      // Names only the stage(s) actually still unapproved — a caller with
      // channel-plan already approved and only positioning outstanding must
      // not be told to redo channel-plan too.
      const outstanding = [
        !approved.positioning ? 'positioning' : null,
        !approved.channel_plan ? 'channel plan' : null,
      ].filter((s): s is string => s !== null)
      return reason(
        outstanding.length === 0,
        `Approve the ${outstanding.join(' and ')} stage${outstanding.length > 1 ? 's' : ''} first.`,
      )
    },
  },
]

function initialState(): Record<ArtefactKind, StageState> {
  const empty = (): StageState => ({
    artefact: null,
    loading: true,
    error: null,
    stale: false,
    failed: false,
    busy: false,
    deselected: new Set<string>(),
  })
  return { research: empty(), positioning: empty(), channel_plan: empty(), copy: empty() }
}

/** The pipeline (M3/M4/M5): research → positioning → channel plan → copy,
 * each ending at a human approval gate. Without this, none of these agent
 * runs can ever be reviewed, so the pipeline could not advance past its
 * first stage from a browser at all.
 *
 * `hasBrief` gates the very first start: `start_research_route` 422s on a
 * campaign with no brief, and nothing else in this pipeline can run before
 * research does.
 */
export function Pipeline({
  campaignId,
  hasBrief,
  hasTargets,
  channelLookup,
}: {
  campaignId: string
  hasBrief: boolean
  hasTargets: boolean
  channelLookup: ChannelLookup
}) {
  const [state, setState] = useState<Record<ArtefactKind, StageState>>(initialState)

  function patch(kind: ArtefactKind, next: Partial<StageState>) {
    setState((prev) => ({ ...prev, [kind]: { ...prev[kind], ...next } }))
  }

  /** Set a stage's freshly-read/mutated `artefact`, and reset its selection
   * (issue #145) the moment the artefact's own `id` changes — a new pipeline
   * run, which must never inherit a previous run's deselections onto
   * whatever now sits at the same position in a regenerated list (`with_
   * element_ids`' own reasoning against positional ids, one level up: the
   * SAME failure shape — an earlier choice silently reattaching to different
   * content — is just as real for the OPERATOR'S selection as it is for the
   * element ids themselves). Always clears `stale`: whatever the previous
   * error meant, a fresh artefact read is the resolution to it. */
  function applyArtefact(kind: ArtefactKind, artefact: Artefact | null, extra: Partial<StageState> = {}) {
    setState((prev) => {
      const idChanged = prev[kind].artefact?.id !== artefact?.id
      return {
        ...prev,
        [kind]: {
          ...prev[kind],
          artefact,
          stale: false,
          // A successful read is the resolution to a previous failure too
          // (issue #159): whatever the last 502 said, this artefact is what
          // the stage is now.
          failed: false,
          ...(idChanged ? { deselected: new Set<string>() } : {}),
          ...extra,
        },
      }
    })
  }

  function toggleElement(kind: ArtefactKind, id: string) {
    setState((prev) => {
      const next = new Set(prev[kind].deselected)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return { ...prev, [kind]: { ...prev[kind], deselected: next } }
    })
  }

  /** Read a stage's artefact.
   *
   * `quiet` is the difference between a read somebody ASKED for and one the
   * page is doing on its own, and it exists because `loading` means two
   * different things to the two files that use it (#147).
   *
   * `PipelineStage` renders `loading` as "there is nothing to show yet" — it
   * swaps out the ENTIRE stage, badge, body and buttons, for `Loading…`.
   * That is right on first load. This function had been treating `loading`
   * as "a request is in flight", which was harmlessly the same thing while
   * `load` was only called on mount and from the manual button. #144 made it
   * the poll's read too, every 2s, and the two meanings stopped agreeing:
   * the whole stage tore down and rebuilt on every tick, which is the
   * reported flicker.
   *
   * So a background poll passes `quiet` and never touches `loading`. It
   * still reports failures — #144 requires that, and `/artefacts/{kind}`
   * 502s with a real sentence an operator needs — but it shows them BESIDE
   * the stage rather than in place of it.
   */
  async function load(kind: ArtefactKind, { quiet = false }: { quiet?: boolean } = {}) {
    if (!quiet) patch(kind, { loading: true, error: null, stale: false, failed: false })
    try {
      const artefact = await getArtefact(campaignId, kind)
      applyArtefact(kind, artefact, quiet ? {} : { loading: false })
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e)
      // `failed` (issue #159) is set from the error's TYPE, not from the fact
      // that one happened — `StageRunFailedError` is the advance route's 502,
      // the one failure another read can never clear. It is written on both
      // the quiet and the loud path because the poll is how an operator
      // actually finds out (#144); a failure only the manual button could
      // reveal would leave the stage stuck for the two minutes nobody clicks.
      const failed = e instanceof StageRunFailedError
      patch(
        kind,
        quiet
          ? { error: message, failed }
          : { loading: false, error: message, stale: false, failed },
      )
    }
  }

  useEffect(() => {
    setState(initialState())
    for (const { kind } of STAGES) {
      load(kind)
    }
    // campaignId is the only thing this effect should re-run on — `load`
    // closes over it fresh on every render, so it is deliberately not in
    // this dependency list. No react-hooks lint plugin is installed in this
    // package, so there is no exhaustive-deps rule to satisfy or suppress.
  }, [campaignId])

  async function runMutation(kind: ArtefactKind, mutate: () => Promise<Artefact | null>) {
    patch(kind, { busy: true, error: null, stale: false, failed: false })
    try {
      const artefact = await mutate()
      applyArtefact(kind, artefact, { busy: false })
    } catch (e: unknown) {
      // `StaleArtefactError` (issue #145) is `approveArtefact`'s "unknown
      // element id" 422 — the artefact changed under the operator, so
      // `PipelineStage` offers "Reload" instead of leaving them to resubmit
      // the exact selection that just failed.
      patch(kind, {
        busy: false,
        error: e instanceof Error ? e.message : String(e),
        stale: e instanceof StaleArtefactError,
        failed: e instanceof StageRunFailedError,
      })
    }
  }

  const approved: Approved = {
    research: state.research.artefact?.status === 'approved',
    positioning: state.positioning.artefact?.status === 'approved',
    channel_plan: state.channel_plan.artefact?.status === 'approved',
  }

  // #144: a `pending` stage otherwise only ever updates when someone presses
  // "Check for result" — a research/synthesis run is measured at 2-4 minutes
  // and everything after at 30-90s, so that is minutes of clicking a button.
  // `load(kind)` (not `runMutation`) is deliberate: it is the same read this
  // component already does on mount and via the manual button, so a poll
  // that lands on a real failure (`getArtefact` throwing on a non-404,
  // non-ok response — `/artefacts/{kind}`'s 502 with a real sentence, per
  // #144) surfaces through the exact `error` plumbing the button already
  // has, without a second failure path to keep in step with it. Excluding a
  // stage whose manual refresh is already in flight (`s.busy`) avoids two
  // requests racing for the same stage.
  usePendingPolling(
    STAGES.map((stage) => {
      const s = state[stage.kind]
      return { kind: stage.kind, pending: s.artefact?.status === 'pending' && !s.busy }
    }),
    (kind) => {
      // Quiet: this read is the page's own, not the operator's. See `load`.
      void load(kind, { quiet: true })
    },
    (kind) => {
      patch(kind, {
        error:
          `Still pending after ${Math.round(POLL_CEILING_MS / 60_000)} minutes — longer than the ` +
          "agent's own timeout, so this run will not complete on its own. Press \"Check for result\" " +
          'to confirm, or check CloudWatch for what happened.',
      })
    },
  )

  return (
    <div className="pipeline">
      {STAGES.map((stage) => {
        const s = state[stage.kind]
        const gate = stage.gate(approved, { hasBrief, hasTargets })
        // Every operator-selectable element in this stage's CURRENT
        // artefact (issue #145) — `[]` while there is nothing proposed yet,
        // computed fresh every render rather than cached in state, since it
        // is a pure read of `s.artefact.body` and keeping a second copy in
        // sync with it is exactly the kind of drift this file elsewhere
        // avoids (see `StageConfig` itself).
        const elements = elementsOf(s.artefact)
        const selection: ElementSelection = {
          isSelected: (id) => !s.deselected.has(id),
          toggle: (id) => toggleElement(stage.kind, id),
        }
        // `pending`'s body is bookkeeping (e.g. research's in-flight run
        // ids), not the real stage output — see `Artefact`'s own doc
        // comment. Nothing to render until the gate has actually produced
        // something.
        const body =
          s.artefact !== null && s.artefact.status !== 'pending'
            ? stage.renderBody(s.artefact, channelLookup, selection)
            : null
        return (
          <PipelineStage
            key={stage.kind}
            kind={stage.kind}
            title={stage.title}
            artefact={s.artefact}
            loading={s.loading}
            error={s.error}
            stale={s.stale}
            failed={s.failed}
            canStart={gate.canStart}
            blockedReason={gate.blockedReason}
            busy={s.busy}
            onStart={() => runMutation(stage.kind, () => stage.start(campaignId))}
            onRefresh={() => runMutation(stage.kind, () => getArtefact(campaignId, stage.kind))}
            onApprove={(elementIds) =>
              runMutation(stage.kind, () => approveArtefact(campaignId, stage.kind, elementIds))
            }
            onReject={() => runMutation(stage.kind, () => rejectArtefact(campaignId, stage.kind))}
            elements={elements}
            deselectedIds={s.deselected}
          >
            {body}
          </PipelineStage>
        )
      })}
    </div>
  )
}
