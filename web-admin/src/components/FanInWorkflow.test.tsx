import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as api from '../lib/api'
import { FanInWorkflow } from './FanInWorkflow'

afterEach(() => {
  vi.restoreAllMocks()
})

const NAME = 'Marketing — synthesise research once both angles complete'

const CORE_ORIGIN = 'https://api.example.invalid'

function declaration(
  config: Record<string, unknown> = { model: 'anthropic/claude-opus-5' },
  coreApiUrl: string = CORE_ORIGIN,
) {
  return {
    name: NAME,
    fingerprint: 'sha256:31883429cd45ebe4',
    core_api_url: coreApiUrl,
    definition: {
      name: NAME,
      trigger_source: 'biffo.core',
      trigger_detail_type: 'agent.run.completed',
      action_type: 'agent_fan_in',
      action_config: config,
      enabled: true,
    },
  }
}

function stub(
  declared: ReturnType<typeof declaration>,
  workflows: { id: string; name: string; action_config: Record<string, unknown> | null }[],
) {
  vi.spyOn(api, 'getDeclaredFanInWorkflow').mockResolvedValue(declared)
  vi.spyOn(api, 'listCoreWorkflows').mockResolvedValue(workflows)
}

describe('FanInWorkflow', () => {
  it('says it is in step when the deployed config matches, and offers no button', async () => {
    stub(declaration(), [{ id: 'w1', name: NAME, action_config: { model: 'anthropic/claude-opus-5' } }])

    render(<FanInWorkflow />)

    expect(await screen.findByText(/In step/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /seed/i })).not.toBeInTheDocument()
  })

  it('names every stale key with both values, not just that something differs', async () => {
    // #62 recorded its lesson as "synthesis is stuck on an old model", so
    // #131's timeout change sailed past on the same mechanism two days later.
    // A panel that said only "stale" would repeat that.
    stub(declaration({ model: 'anthropic/claude-opus-5', timeout_seconds: 240 }), [
      { id: 'w1', name: NAME, action_config: { model: 'anthropic/claude-opus-4.8' } },
    ])

    render(<FanInWorkflow />)

    expect(await screen.findByText(/Stale in 2 keys/)).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /model/ })).toHaveTextContent('anthropic/claude-opus-4.8')
    expect(screen.getByRole('row', { name: /timeout_seconds/ })).toHaveTextContent('(absent)')
  })

  it('replaces the existing definition rather than creating a second one', async () => {
    // Two definitions on the same trigger would fire synthesis twice per
    // chain — the plugin would pay for a duplicate run and reconcile the
    // wrong one.
    stub(declaration(), [{ id: 'w1', name: NAME, action_config: { model: 'old' } }])
    const seed = vi.spyOn(api, 'seedFanInWorkflow').mockResolvedValue(undefined)

    render(<FanInWorkflow />)
    await userEvent.click(await screen.findByRole('button', { name: /Re-seed it now/ }))

    await waitFor(() =>
      expect(seed).toHaveBeenCalledWith(
        expect.objectContaining({ name: NAME }),
        'w1',
        `${CORE_ORIGIN}/api/v1/orchestration`,
      ),
    )
  })

  it('offers to seed, with no id, when Core holds no workflow of this name', async () => {
    stub(declaration(), [{ id: 'other', name: 'Something else', action_config: {} }])
    const seed = vi.spyOn(api, 'seedFanInWorkflow').mockResolvedValue(undefined)

    render(<FanInWorkflow />)

    expect(await screen.findByText(/Not seeded/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Seed it now/ }))
    await waitFor(() =>
      expect(seed).toHaveBeenCalledWith(
        expect.anything(),
        null,
        `${CORE_ORIGIN}/api/v1/orchestration`,
      ),
    )
  })


  it('addresses Core by its own origin, never this page\'s (#171)', async () => {
    // The whole of #171. Asked for on THIS origin, the request reaches the
    // instance's public site — CloudFront routes only `/api/v1/plugins/*` to
    // the API — and comes back 403 with an HTML body, identically with a valid
    // admin token and with no token at all. So what is asserted here is the
    // absolute base, not merely that a call was made.
    stub(declaration(), [{ id: 'w1', name: NAME, action_config: { model: 'anthropic/claude-opus-5' } }])

    render(<FanInWorkflow />)

    await waitFor(() =>
      expect(api.listCoreWorkflows).toHaveBeenCalledWith(`${CORE_ORIGIN}/api/v1/orchestration`),
    )
    const [base] = vi.mocked(api.listCoreWorkflows).mock.calls[0]
    expect(base.startsWith('http')).toBe(true)
  })

  it('says so, and calls nothing, when the deployment supplies no Core origin', async () => {
    // Falling back to a same-origin path is what produced a 403 that looked
    // like a permissions problem for a morning. Not knowing is a reportable
    // state, not a reason to guess.
    stub(declaration({ model: 'm' }, ''), [])

    render(<FanInWorkflow />)

    expect(await screen.findByText(/BIFFO_CORE_API_URL is unset/)).toBeInTheDocument()
    expect(api.listCoreWorkflows).not.toHaveBeenCalled()
    expect(screen.queryByText(/In step/)).not.toBeInTheDocument()
  })

  it('reports a failed read as a failure, never as "in step"', async () => {
    // The fail-open shape this whole issue is about: a check that cannot read
    // the deployed config must not render as a clean bill of health.
    vi.spyOn(api, 'getDeclaredFanInWorkflow').mockResolvedValue(declaration())
    vi.spyOn(api, 'listCoreWorkflows').mockRejectedValue(new Error('403 forbidden'))

    render(<FanInWorkflow />)

    expect(await screen.findByText(/Could not check the deployed workflow/)).toBeInTheDocument()
    expect(screen.queryByText(/In step/)).not.toBeInTheDocument()
  })

  it('surfaces a refused write instead of reporting success it did not get', async () => {
    stub(declaration(), [{ id: 'w1', name: NAME, action_config: { model: 'old' } }])
    vi.spyOn(api, 'seedFanInWorkflow').mockRejectedValue(new Error('403 forbidden'))

    render(<FanInWorkflow />)
    await userEvent.click(await screen.findByRole('button', { name: /Re-seed it now/ }))

    expect(await screen.findByText(/403 forbidden/)).toBeInTheDocument()
  })

  it('re-reads Core after seeding rather than assuming the write landed', async () => {
    stub(declaration(), [{ id: 'w1', name: NAME, action_config: { model: 'old' } }])
    vi.spyOn(api, 'seedFanInWorkflow').mockResolvedValue(undefined)

    render(<FanInWorkflow />)
    await userEvent.click(await screen.findByRole('button', { name: /Re-seed it now/ }))

    await waitFor(() => expect(api.listCoreWorkflows).toHaveBeenCalledTimes(2))
  })
})
