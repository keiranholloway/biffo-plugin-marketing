import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { Results } from './Results'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

describe('Results', () => {
  it('shows real clicks, and leads/conversions/cost as not measurable — never as zero', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [
            {
              campaign_id: 'c1',
              campaign_name: 'Spring',
              clicks: { total: 12, paid: 5, organic: 7, unknown_channel_type: 0 },
              leads: {
                measurable: false,
                denominator: 12,
                reason: 'No route reachable from this plugin exposes this yet.',
              },
              conversions: { measurable: false, denominator: null, reason: 'no transport' },
              cost: { measurable: false, denominator: null, reason: 'no transport' },
            },
          ],
          unattributed_clicks: 0,
        }),
      }),
    )

    render(<Results campaignId="c1" />)

    expect(await screen.findByText(/12 total — 5 paid, 7 organic/i)).toBeInTheDocument()
    expect(screen.getAllByText(/not measurable/i)).toHaveLength(3)
    expect(screen.getByText(/denominator: 12/)).toBeInTheDocument()
    // Never render "0" for an unmeasurable count.
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument()
  })

  it('says plainly when this campaign has no results yet', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ campaigns: [], unattributed_clicks: 0 }) }),
    )

    render(<Results campaignId="missing" />)

    expect(await screen.findByText(/no results yet/i)).toBeInTheDocument()
  })

  it('reports a load failure rather than rendering nothing', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }))

    render(<Results campaignId="c1" />)

    expect(await screen.findByText(/could not load results/i)).toBeInTheDocument()
  })
})
