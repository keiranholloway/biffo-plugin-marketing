import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { DistributionPack } from './DistributionPack'

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

describe('DistributionPack', () => {
  it('surfaces missing_placements rather than a pack that quietly omits them', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaign_id: CAMPAIGN,
          assets: [
            {
              id: 'as1',
              campaign_id: CAMPAIGN,
              media_kind: 'image',
              placement: null,
              media_id: 'm1',
              is_source: true,
              url: 'https://example.com/source.png',
            },
          ],
          missing_placements: ['feed_1x1', 'story_9x16'],
          copy: [
            { channel: 'linkedin', motion: 'organic', headline: 'H', body: 'B', cta: 'C', sources: [] },
          ],
          links: [{ channel: 'linkedin', variant: null, is_paid: false, url: 'https://x/c/tok' }],
          guidance: 'Disclose paid placements per platform policy.',
        }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/missing renders for: feed_1x1, story_9x16/i)).toBeInTheDocument()
    expect(screen.getAllByText(/linkedin/).length).toBeGreaterThan(0)
    expect(screen.getByText('https://x/c/tok')).toBeInTheDocument()
    expect(screen.getByText('Disclose paid placements per platform policy.')).toBeInTheDocument()
  })

  it('says plainly when a deployment has no public base URL, rather than a broken link', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaign_id: CAMPAIGN,
          assets: [],
          missing_placements: [],
          copy: [],
          links: [{ channel: 'linkedin', variant: null, is_paid: false, url: null }],
          guidance: '',
        }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/no public base url configured/i)).toBeInTheDocument()
  })

  it('reports a load failure with the server reason, not a raw body', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: 'No approved source creative for this campaign yet.' }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/no approved source creative/i)).toBeInTheDocument()
  })
})
