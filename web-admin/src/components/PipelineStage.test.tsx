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
})
