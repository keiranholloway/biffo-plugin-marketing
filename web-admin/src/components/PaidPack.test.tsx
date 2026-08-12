import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { PaidPack } from './PaidPack'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN = 'c1'

describe('PaidPack', () => {
  it('shows trimmed ad copy, targeting, budget, and spend as explicitly unmeasurable', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaign_id: CAMPAIGN,
          ad_copy: [
            {
              channel: 'Facebook Feed',
              platform: 'meta',
              headline: 'Fast onboarding, done right',
              headline_limit: 40,
              headline_truncated: false,
              body: 'A'.repeat(130),
              body_limit: 125,
              body_truncated: true,
              cta: 'Learn more',
              cta_limit: 20,
              cta_truncated: false,
            },
          ],
          assets: [],
          missing_placements: ['feed_1x1'],
          targeting: [{ name: 'Busy owners', description: 'Time-poor franchise owners.', source_count: 2 }],
          budget: {
            currency: 'USD',
            channel_count: 1,
            per_channel_daily: 20,
            total_daily: 20,
            test_window_days: 7,
            total_test_budget: 140,
            basis: 'A fixed starting-point heuristic.',
          },
          links: [],
          guidance: '',
          spend: {
            measurable: false,
            denominator: null,
            reason: 'No route reachable from this plugin exposes this yet.',
          },
        }),
      }),
    )

    render(<PaidPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/missing renders for: feed_1x1/i)).toBeInTheDocument()
    expect(screen.getByText(/trimmed to 125 chars/i)).toBeInTheDocument()
    expect(screen.getByText('Busy owners')).toBeInTheDocument()
    // Grounded, but the full {url, note} pairs are not repeated here — only
    // a count, pointing back at the positioning artefact for the citations.
    expect(screen.getByText(/grounded in 2 research sources/i)).toBeInTheDocument()
    expect(screen.getByText(/1 paid channel\(s\)/)).toBeInTheDocument()
    expect(screen.getByText(/not measurable — no route reachable/i)).toBeInTheDocument()
  })

  // The three distinct 4xx branches `paid_pack_routes.get_paid_pack_route`
  // actually raises (issue #85), mirroring `pack_routes.py`'s own wording —
  // asserting the operator-facing text the server sent, not the status code.
  // `detail` strings are copied verbatim from `paid_pack_routes.py`.
  it('says there is no copy yet, not just "(404)"', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: 'No copy artefact for this campaign yet.' }),
      }),
    )

    render(<PaidPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/no copy artefact for this campaign yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the paid pack \(404\)$/i)).not.toBeInTheDocument()
  })

  it('says the campaign was not found, not just "(404)"', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: 'Campaign not found.' }),
      }),
    )

    render(<PaidPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/^campaign not found\./i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the paid pack \(404\)$/i)).not.toBeInTheDocument()
  })

  it('says the copy needs approving on a 409 — one click from resolved, not a bare status', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          detail: 'The copy artefact must be approved before this can proceed (status: proposed).',
        }),
      }),
    )

    render(<PaidPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/copy artefact must be approved before this can proceed/i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the paid pack \(409\)$/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/"detail"/)).not.toBeInTheDocument()
  })
})
