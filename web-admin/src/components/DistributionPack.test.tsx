import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import * as auth from '../lib/auth'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
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

/** `channelLookup` is fetched once by `CampaignDetail` (`useChannelTaxonomy`)
 * and passed down — `DistributionPack` itself never fetches `/channels`, so
 * these tests only need this stub, not a second mocked response. */
function makeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), loading }
}

const LINKEDIN: ChannelTaxonomyEntry = {
  key: 'linkedin_organic',
  label: 'LinkedIn — organic',
  motion: 'organic',
  category: 'social',
  ad_platform: null,
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
            { channel_key: 'linkedin_organic', motion: 'organic', headline: 'H', body: 'B', cta: 'C', sources: [] },
          ],
          links: [{ channel: 'linkedin', variant: null, is_paid: false, url: 'https://x/c/tok' }],
          guidance: 'Disclose paid placements per platform policy.',
        }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/missing renders for: feed_1x1, story_9x16/i)).toBeInTheDocument()
    // The copy section shows the taxonomy label, not the raw channel_key.
    expect(screen.getByText('LinkedIn — organic')).toBeInTheDocument()
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

    render(<DistributionPack campaignId={CAMPAIGN} channelLookup={makeLookup([LINKEDIN])} />)
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

    render(<DistributionPack campaignId={CAMPAIGN} channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/no approved source creative/i)).toBeInTheDocument()
  })

  it('marks a channel_key with no taxonomy row as unrecognised rather than blank', async () => {
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
          copy: [
            { channel_key: 'deleted_channel', motion: 'organic', headline: 'H', body: 'B', cta: 'C', sources: [] },
          ],
          links: [],
          guidance: '',
        }),
      }),
    )

    // An empty taxonomy (not yet loaded any real entries) — a channel_key
    // with nothing to resolve against.
    render(<DistributionPack campaignId={CAMPAIGN} channelLookup={makeLookup([])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    const badge = await screen.findByText(/unrecognised channel/i)
    expect(badge).toBeInTheDocument()
    // "deleted_channel" and the badge are sibling nodes inside the same
    // list item, not one isolated element's full text.
    expect(screen.getByRole('listitem').textContent).toContain('deleted_channel')
  })

  it('does not flash a blank channel name while the taxonomy is still loading', async () => {
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
          copy: [
            {
              channel_key: 'linkedin_organic',
              motion: 'organic',
              headline: 'H',
              body: 'B',
              cta: 'C',
              sources: [],
            },
          ],
          links: [],
          guidance: '',
        }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} channelLookup={makeLookup([], true)} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/loading channel/i)).toBeInTheDocument()
    expect(screen.queryByText(/unrecognised channel/i)).not.toBeInTheDocument()
  })
})
