import type { ReactNode } from 'react'

import type { Artefact, ArtefactKind, ArtefactStatus } from '../lib/api'

const STATUS_LABEL: Record<ArtefactStatus, string> = {
  pending: 'Running…',
  proposed: 'Awaiting review',
  approved: 'Approved',
  rejected: 'Rejected',
}

interface PipelineStageProps {
  kind: ArtefactKind
  title: string
  artefact: Artefact | null
  loading: boolean
  error: string | null
  /** Whether the upstream gate this stage needs is satisfied. */
  canStart: boolean
  /** Human reason `canStart` is false, shown instead of a start button that
   * would only 409. `null` when `canStart` is true. */
  blockedReason: string | null
  busy: boolean
  onStart: () => void
  onRefresh: () => void
  onApprove: () => void
  onReject: () => void
  /** The rendered artefact body — `null` while there is nothing approved,
   * proposed or rejected to show yet. */
  children?: ReactNode
}

/** One pipeline stage's gate: `pending → proposed → approved | rejected`.
 *
 * The gate is the point (see `pipeline.py`'s module docstring) — this
 * component is deliberately generic over all four stages (research,
 * positioning, channel plan, copy) rather than four near-identical copies,
 * because the gate shape is exactly the same for all of them; only the start
 * action and the body renderer differ, and both are supplied by the caller.
 */
export function PipelineStage({
  kind,
  title,
  artefact,
  loading,
  error,
  canStart,
  blockedReason,
  busy,
  onStart,
  onRefresh,
  onApprove,
  onReject,
  children,
}: PipelineStageProps) {
  const status = artefact?.status ?? null

  return (
    <section className="stage" aria-label={title} data-testid={`stage-${kind}`}>
      <div className="stage-head">
        <h4>{title}</h4>
        {status !== null && <span className={`badge badge-${status}`}>{STATUS_LABEL[status]}</span>}
      </div>

      {loading && <p className="empty">Loading…</p>}
      {error !== null && <p className="error">{error}</p>}

      {!loading && (
        <>
          {status === null && (
            <>
              {blockedReason !== null && <p className="hint">{blockedReason}</p>}
              <button type="button" onClick={onStart} disabled={!canStart || busy}>
                {busy ? 'Starting…' : `Start ${title.toLowerCase()}`}
              </button>
            </>
          )}

          {status === 'pending' && (
            <button type="button" onClick={onRefresh} disabled={busy}>
              {busy ? 'Checking…' : 'Check for result'}
            </button>
          )}

          {(status === 'proposed' || status === 'approved' || status === 'rejected') && children}

          {status === 'proposed' && (
            <div className="stage-actions">
              <button type="button" onClick={onApprove} disabled={busy}>
                {busy ? 'Approving…' : 'Approve'}
              </button>
              <button type="button" className="secondary" onClick={onReject} disabled={busy}>
                {busy ? 'Rejecting…' : 'Reject'}
              </button>
            </div>
          )}

          {status === 'rejected' && (
            <button type="button" onClick={onStart} disabled={!canStart || busy}>
              {busy ? 'Starting…' : `Run ${title.toLowerCase()} again`}
            </button>
          )}
        </>
      )}
    </section>
  )
}
