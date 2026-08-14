import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { Artefact } from '../lib/api'
import { PipelineStage } from './PipelineStage'

function artefact(status: Artefact['status']): Artefact {
  return {
    id: 'a1',
    campaign_id: 'c1',
    kind: 'research',
    status,
    body: null,
    citations: null,
    causation_id: null,
    agent_run_id: null,
  }
}

const noop = () => {}

describe('PipelineStage', () => {
  it('offers a start button, disabled with a reason, when the gate is not satisfied', () => {
    render(
      <PipelineStage
        kind="positioning"
        title="Positioning"
        artefact={null}
        loading={false}
        error={null}
        canStart={false}
        blockedReason="Approve the research stage first."
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      />,
    )
    expect(screen.getByText('Approve the research stage first.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start positioning/i })).toBeDisabled()
  })

  it('offers an enabled start button once the gate is satisfied', async () => {
    const onStart = vi.fn()
    const user = userEvent.setup()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={null}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={onStart}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      />,
    )
    await user.click(screen.getByRole('button', { name: /start research/i }))
    expect(onStart).toHaveBeenCalledOnce()
  })

  it('offers "check for result" while pending, not approve/reject', () => {
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('pending')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      />,
    )
    expect(screen.getByRole('button', { name: /check for result/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^approve$/i })).not.toBeInTheDocument()
  })

  it('offers approve and reject once proposed, and renders the artefact body', async () => {
    const onApprove = vi.fn()
    const onReject = vi.fn()
    const user = userEvent.setup()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={onApprove}
        onReject={onReject}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    expect(screen.getByText('the research findings')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^approve$/i }))
    expect(onApprove).toHaveBeenCalledOnce()
    await user.click(screen.getByRole('button', { name: /^reject$/i }))
    expect(onReject).toHaveBeenCalledOnce()
  })

  it('offers to run again once rejected', () => {
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('rejected')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      />,
    )
    expect(screen.getByRole('button', { name: /run research again/i })).toBeInTheDocument()
  })

  it('explains why re-running is blocked, not just a disabled button with no reason', () => {
    // A rejected stage whose upstream gate has since closed again (e.g. the
    // campaign's brief was cleared) must say why "run again" cannot be
    // clicked — the null-status case always did; the rejected case silently
    // did not.
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('rejected')}
        loading={false}
        error={null}
        canStart={false}
        blockedReason="This campaign has no brief yet — add one above before starting research."
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      />,
    )
    expect(screen.getByText(/no brief yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run research again/i })).toBeDisabled()
  })

  // ── Partial approval (issue #145) ─────────────────────────────────────────

  const TWO_ELEMENTS = [
    { id: 'e1', label: 'Owners want faster onboarding' },
    { id: 'e2', label: 'Competitors take 3 weeks' },
  ]

  it('defaults to everything selected: approving untouched sends no body, exactly as before #145', async () => {
    const onApprove = vi.fn()
    const user = userEvent.setup()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={onApprove}
        onReject={noop}
        elements={TWO_ELEMENTS}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    // Nothing deselected: the button is plain "Approve", not "Approve N of
    // M" — the one-click common case must not change shape just because
    // there is now something to select from.
    const approveButton = screen.getByRole('button', { name: /^approve$/i })
    expect(screen.queryByText(/will drop/i)).not.toBeInTheDocument()
    await user.click(approveButton)
    expect(onApprove).toHaveBeenCalledOnce()
    expect(onApprove).toHaveBeenCalledWith(null)
  })

  it('deselecting one element sends only the remaining ids, and shows what will be dropped', async () => {
    const onApprove = vi.fn()
    const user = userEvent.setup()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={onApprove}
        onReject={noop}
        elements={TWO_ELEMENTS}
        deselectedIds={new Set(['e1'])}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    // The dropped element is named, so an operator sees exactly what they
    // are about to lose before committing — not just a bare count.
    expect(screen.getByText(/will drop 1 of 2/i)).toBeInTheDocument()
    expect(screen.getByText('Owners want faster onboarding')).toBeInTheDocument()
    expect(screen.queryByText('Competitors take 3 weeks')).not.toBeInTheDocument()

    const approveButton = screen.getByRole('button', { name: /approve 1 of 2/i })
    await user.click(approveButton)
    expect(onApprove).toHaveBeenCalledOnce()
    expect(onApprove).toHaveBeenCalledWith(['e2'])
  })

  it('blocks approving when everything is deselected, pointing at reject instead', async () => {
    const onApprove = vi.fn()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error={null}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={onApprove}
        onReject={noop}
        elements={TWO_ELEMENTS}
        deselectedIds={new Set(['e1', 'e2'])}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    expect(screen.getByText(/nothing is selected/i)).toBeInTheDocument()
    expect(screen.getByText(/use "reject"/i)).toBeInTheDocument()
    const approveButton = screen.getByRole('button', { name: /approve 0 of 2/i })
    expect(approveButton).toBeDisabled()
    // A disabled button ignores clicks outright, but assert the intent too:
    // this route must never be reachable from the UI, since the server 422s
    // an empty selection on purpose (issue #145).
    expect(onApprove).not.toHaveBeenCalled()
  })

  it('offers a Reload action on a stale-selection error, not just the raw message', async () => {
    const onRefresh = vi.fn()
    const user = userEvent.setup()
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error="Unknown element id(s): e9. This selection may be stale — refresh the artefact and try again. (422)"
        stale={true}
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={onRefresh}
        onApprove={noop}
        onReject={noop}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    expect(screen.getByText(/unknown element id/i)).toBeInTheDocument()
    const reloadButton = screen.getByRole('button', { name: /reload/i })
    await user.click(reloadButton)
    expect(onRefresh).toHaveBeenCalledOnce()
  })

  it('does not offer Reload for an ordinary error', () => {
    render(
      <PipelineStage
        kind="research"
        title="Research"
        artefact={artefact('proposed')}
        loading={false}
        error="could not approve the research stage (500)"
        canStart={true}
        blockedReason={null}
        busy={false}
        onStart={noop}
        onRefresh={noop}
        onApprove={noop}
        onReject={noop}
      >
        <p>the research findings</p>
      </PipelineStage>,
    )
    expect(screen.queryByRole('button', { name: /reload/i })).not.toBeInTheDocument()
  })
})
