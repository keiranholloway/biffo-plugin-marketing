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
  type Artefact,
  type ArtefactKind,
  type ChannelPlanBody,
  type CopySetBody,
  type PositioningBody,
  type ResearchSynthesisBody,
} from '../lib/api'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { POLL_CEILING_MS, usePendingPolling } from '../lib/usePendingPolling'
import { ChannelPlanArtefact, CopyArtefact, PositioningArtefact, ResearchArtefact } from './ArtefactBody'
import { PipelineStage } from './PipelineStage'

interface StageState {
  artefact: Artefact | null
  loading: boolean
  error: string | null
  busy: boolean
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
   * positioning's renderers simply ignore the second argument. */
  renderBody: (artefact: Artefact, channelLookup: ChannelLookup) => ReactNode
  gate: (approved: Approved, hasBrief: boolean) => Gate
}

/** Everything that varies per stage, in one place — a fifth stage is one new
 * entry here, not four disjoint edits across a starter map, a body-render
 * switch and a gate map with nothing enforcing they stay in step. */
const STAGES: StageConfig[] = [
  {
    kind: 'research',
    title: 'Research',
    start: startResearch,
    renderBody: (artefact) => {
      const body = parseArtefactBody<ResearchSynthesisBody>(artefact)
      return body !== null ? <ResearchArtefact body={body} /> : <p className="empty">No content to show.</p>
    },
    gate: (_approved, hasBrief) =>
      reason(hasBrief, 'This campaign has no brief yet — add one above before starting research.'),
  },
  {
    kind: 'positioning',
    title: 'Positioning',
    start: startPositioning,
    renderBody: (artefact) => {
      const body = parseArtefactBody<PositioningBody>(artefact)
      return body !== null ? <PositioningArtefact body={body} /> : <p className="empty">No content to show.</p>
    },
    gate: (approved) => reason(approved.research, 'Approve the research stage first.'),
  },
  {
    kind: 'channel_plan',
    title: 'Channel plan',
    start: startChannelPlan,
    renderBody: (artefact, channelLookup) => {
      const body = parseArtefactBody<ChannelPlanBody>(artefact)
      return body !== null ? (
        <ChannelPlanArtefact body={body} channelLookup={channelLookup} />
      ) : (
        <p className="empty">No content to show.</p>
      )
    },
    gate: (approved) => reason(approved.positioning, 'Approve the positioning stage first.'),
  },
  {
    kind: 'copy',
    title: 'Copy',
    start: startCopy,
    renderBody: (artefact, channelLookup) => {
      const body = parseArtefactBody<CopySetBody>(artefact)
      return body !== null ? (
        <CopyArtefact body={body} channelLookup={channelLookup} />
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
  const empty = (): StageState => ({ artefact: null, loading: true, error: null, busy: false })
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
  channelLookup,
}: {
  campaignId: string
  hasBrief: boolean
  channelLookup: ChannelLookup
}) {
  const [state, setState] = useState<Record<ArtefactKind, StageState>>(initialState)

  function patch(kind: ArtefactKind, next: Partial<StageState>) {
    setState((prev) => ({ ...prev, [kind]: { ...prev[kind], ...next } }))
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
    if (!quiet) patch(kind, { loading: true, error: null })
    try {
      const artefact = await getArtefact(campaignId, kind)
      patch(kind, quiet ? { artefact } : { artefact, loading: false })
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e)
      patch(kind, quiet ? { error: message } : { loading: false, error: message })
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
    patch(kind, { busy: true, error: null })
    try {
      const artefact = await mutate()
      patch(kind, { artefact, busy: false })
    } catch (e: unknown) {
      patch(kind, { busy: false, error: e instanceof Error ? e.message : String(e) })
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
        const gate = stage.gate(approved, hasBrief)
        // `pending`'s body is bookkeeping (e.g. research's in-flight run
        // ids), not the real stage output — see `Artefact`'s own doc
        // comment. Nothing to render until the gate has actually produced
        // something.
        const body =
          s.artefact !== null && s.artefact.status !== 'pending' ? stage.renderBody(s.artefact, channelLookup) : null
        return (
          <PipelineStage
            key={stage.kind}
            kind={stage.kind}
            title={stage.title}
            artefact={s.artefact}
            loading={s.loading}
            error={s.error}
            canStart={gate.canStart}
            blockedReason={gate.blockedReason}
            busy={s.busy}
            onStart={() => runMutation(stage.kind, () => stage.start(campaignId))}
            onRefresh={() => runMutation(stage.kind, () => getArtefact(campaignId, stage.kind))}
            onApprove={() => runMutation(stage.kind, () => approveArtefact(campaignId, stage.kind))}
            onReject={() => runMutation(stage.kind, () => rejectArtefact(campaignId, stage.kind))}
          >
            {body}
          </PipelineStage>
        )
      })}
    </div>
  )
}
