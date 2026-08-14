import type { ReactNode } from 'react'

import type { Artefact, ArtefactKind, ArtefactStatus } from '../lib/api'
import type { SelectableElement } from '../lib/elementSelection'

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
  /** True when `error` is `approveArtefact` failing with `StaleArtefactError`
   * (issue #145) — an unknown-element-id 422, which means the artefact
   * changed under the operator (a re-run, or a page left open) rather than
   * anything wrong with the choice they made. Offers "Reload" instead of
   * leaving them to retry the exact request that just failed. */
  stale?: boolean
  /** Whether the upstream gate this stage needs is satisfied. */
  canStart: boolean
  /** Human reason `canStart` is false, shown instead of a start button that
   * would only 409. `null` when `canStart` is true. */
  blockedReason: string | null
  busy: boolean
  onStart: () => void
  onRefresh: () => void
  /** `elementIds`: `null` to approve everything — sends no request body, the
   * pre-#145 behaviour and still what a click with nothing deselected sends
   * (issue #145: "approving without touching anything must behave exactly
   * as it does now"). A non-empty array to approve only that subset. Never
   * called with an empty array — {@link PipelineStage} itself disables the
   * button rather than letting that request reach the server's 422. */
  onApprove: (elementIds: string[] | null) => void
  onReject: () => void
  /** Every operator-selectable element in this stage's `proposed` artefact —
   * omitted (or `[]`) for a stage with nothing to select from yet (no
   * artefact, still pending, or a legacy body with no element ids at all),
   * in which case the Approve button behaves exactly as it always has. */
  elements?: SelectableElement[]
  /** Element ids the operator has unchecked — always a subset of
   * `elements`'s ids. Everything else in `elements` is selected; an empty
   * (or omitted) set is "everything selected", the default this stage opens
   * in every time a fresh `proposed` artefact arrives. */
  deselectedIds?: Set<string>
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
 *
 * **Owns the approve/reject controls (issue #145).** `elements`/
 * `deselectedIds` are supplied by `Pipeline.tsx`, which is where the
 * selection actually lives (one `Set<string>` per stage, alongside
 * `artefact`/`busy`/`error`) — this component's job is turning that state
 * into what `onApprove` is called with, and into what the operator sees
 * before they commit: what a partial approval would drop, and why an empty
 * selection is blocked rather than silently sent.
 */
export function PipelineStage({
  kind,
  title,
  artefact,
  loading,
  error,
  stale = false,
  canStart,
  blockedReason,
  busy,
  onStart,
  onRefresh,
  onApprove,
  onReject,
  elements = [],
  deselectedIds,
  children,
}: PipelineStageProps) {
  const status = artefact?.status ?? null
  const deselected = deselectedIds ?? new Set<string>()
  const dropped = elements.filter((el) => deselected.has(el.id))
  const selectedCount = elements.length - dropped.length
  // Only meaningful when there is something to select from at all — an
  // artefact with no ids yet (legacy, or before this stage has ever run)
  // must not be treated as "nothing selected".
  const noneSelected = elements.length > 0 && selectedCount === 0
  const hasDeselection = dropped.length > 0

  function handleApprove() {
    if (!hasDeselection) {
      // Untouched: send no body at all. This is the one branch that must
      // behave byte-for-byte as it did before #145 — see this file's own
      // `onApprove` doc.
      onApprove(null)
      return
    }
    const ids = elements.filter((el) => !deselected.has(el.id)).map((el) => el.id)
    onApprove(ids)
  }

  return (
    <section className="stage" aria-label={title} data-testid={`stage-${kind}`}>
      <div className="stage-head">
        <h4>{title}</h4>
        {status !== null && <span className={`badge badge-${status}`}>{STATUS_LABEL[status]}</span>}
      </div>

      {loading && <p className="empty">Loading…</p>}
      {error !== null && (
        <div className="error-panel">
          <p className="error">{error}</p>
          {stale && (
            <button type="button" className="secondary" onClick={onRefresh}>
              Reload
            </button>
          )}
        </div>
      )}

      {!loading && (
        <>
          {/* `null` (never started) and `rejected` (started, then turned
              down) share the exact same start gate — both need canStart and
              both explain themselves with the same blockedReason when it
              isn't met. Only the button's own label differs. Keeping this as
              one branch is what stopped the rejected case from silently
              losing the hint the null case always had. */}
          {(status === null || status === 'rejected') && (
            <>
              {blockedReason !== null && <p className="hint">{blockedReason}</p>}
              <button type="button" onClick={onStart} disabled={!canStart || busy}>
                {busy
                  ? 'Starting…'
                  : status === 'rejected'
                    ? `Run ${title.toLowerCase()} again`
                    : `Start ${title.toLowerCase()}`}
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
            <div className="stage-actions-group">
              {elements.length > 0 && hasDeselection && (
                <div className="selection-summary" aria-live="polite">
                  {noneSelected ? (
                    <p className="hint">
                      Nothing is selected. Approving nothing is not the same as rejecting — use "Reject" below if
                      none of this is usable, or tick at least one item to approve a subset.
                    </p>
                  ) : (
                    <>
                      <p className="hint">
                        Approving now will drop {dropped.length} of {elements.length} item
                        {elements.length === 1 ? '' : 's'}:
                      </p>
                      <ul className="dropped-elements">
                        {dropped.map((el) => (
                          <li key={el.id}>{el.label}</li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
              <div className="stage-actions">
                <button type="button" onClick={handleApprove} disabled={busy || noneSelected}>
                  {busy
                    ? 'Approving…'
                    : hasDeselection
                      ? `Approve ${selectedCount} of ${elements.length}`
                      : 'Approve'}
                </button>
                <button type="button" className="secondary" onClick={onReject} disabled={busy}>
                  {busy ? 'Rejecting…' : 'Reject'}
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  )
}
