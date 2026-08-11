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
          targeting: [{ name: 'Busy owners', description: 'Time-poor franchise owners.', sources: [] }],
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
    expect(screen.getByText(/1 paid channel\(s\)/)).toBeInTheDocument()
    expect(screen.getByText(/not measurable — no route reachable/i)).toBeInTheDocument()
  })
})
