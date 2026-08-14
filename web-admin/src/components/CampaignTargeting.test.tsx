import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Campaign } from '../lib/api'
import * as auth from '../lib/auth'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { CampaignTargeting } from './CampaignTargeting'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN: Campaign = {
  id: 'c1',
  name: 'Spring demo push',
  status: 'draft',
  destination_url: 'https://example.com/demo',
  brief: 'Who this is for.',
  guidance: null,
  motion: null,
  target_channel_keys: null,
}

const ENTRIES = [
  {
    key: 'linkedin_organic',
    label: 'LinkedIn — organic',
    motion: 'organic' as const,
    category: 'social',
    ad_platform: null,
    publish_url: null,
  },
  {
    key: 'google_search_paid',
    label: 'Google Search ads',
    motion: 'paid' as const,
    category: 'search',
    ad_platform: 'google',
    publish_url: null,
  },
  {
    key: 'trade_show_presence',
    label: 'Trade show / expo presence',
    motion: 'organic' as const,
    category: 'events',
    ad_platform: null,
    publish_url: null,
  },
]

const LOOKUP: ChannelLookup = {
  get: (key: string) => ENTRIES.find((e) => e.key === key),
  entries: ENTRIES,
  loading: false,
}

function patchStub(response: Partial<Campaign> = {}) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.endsWith('/campaigns/c1') && init?.method === 'PATCH') {
      return Promise.resolve({
        ok: true,
        json: async () => ({ ...CAMPAIGN, ...JSON.parse(String(init.body)), ...response }),
      })
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
  })
}

describe('CampaignTargeting', () => {
  it('offers the three campaign motions and, until one is chosen, no channels', async () => {
    stubSession()
    vi.stubGlobal('fetch', patchStub())

    render(
      <CampaignTargeting
        campaign={CAMPAIGN}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )

    expect(screen.getByRole('radio', { name: /organic/i })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /^paid$/i })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /both/i })).toBeInTheDocument()
    // No motion yet: nothing to pick from, because which channels are even
    // eligible depends on the motion.
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('shows only the channels the chosen motion can run', async () => {
    stubSession()
    vi.stubGlobal('fetch', patchStub())
    const user = userEvent.setup()

    render(
      <CampaignTargeting
        campaign={CAMPAIGN}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )
    await user.click(screen.getByRole('radio', { name: /organic/i }))

    expect(screen.getByRole('checkbox', { name: /LinkedIn — organic/ })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Trade show/ })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Google Search ads/ })).not.toBeInTheDocument()
  })

  it('saves the motion and the selected channel keys onto the campaign', async () => {
    stubSession()
    const fetchMock = patchStub()
    vi.stubGlobal('fetch', fetchMock)
    const onCampaignUpdated = vi.fn()
    const user = userEvent.setup()

    render(
      <CampaignTargeting
        campaign={CAMPAIGN}
        channelLookup={LOOKUP}
        onCampaignUpdated={onCampaignUpdated}
      />,
    )
    await user.click(screen.getByRole('radio', { name: /both/i }))
    await user.click(screen.getByRole('checkbox', { name: /LinkedIn — organic/ }))
    await user.click(screen.getByRole('checkbox', { name: /Google Search ads/ }))
    await user.click(screen.getByRole('button', { name: /save targeting/i }))

    const [, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      motion: 'both',
      target_channel_keys: 'linkedin_organic,google_search_paid',
    })
    expect(onCampaignUpdated).toHaveBeenCalledWith(
      expect.objectContaining({ motion: 'both', target_channel_keys: 'linkedin_organic,google_search_paid' }),
    )
  })

  it('pre-checks what the campaign already targets', async () => {
    stubSession()
    vi.stubGlobal('fetch', patchStub())

    render(
      <CampaignTargeting
        campaign={{ ...CAMPAIGN, motion: 'organic', target_channel_keys: 'linkedin_organic' }}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )

    expect(screen.getByRole('radio', { name: /organic/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /LinkedIn — organic/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Trade show/ })).not.toBeChecked()
  })

  it('narrowing the motion drops the channels it excludes, and says so', async () => {
    stubSession()
    const fetchMock = patchStub()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(
      <CampaignTargeting
        campaign={{
          ...CAMPAIGN,
          motion: 'both',
          target_channel_keys: 'linkedin_organic,google_search_paid',
        }}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )
    await user.click(screen.getByRole('radio', { name: /organic/i }))

    // Told, not silently discarded — the paid channel is still stored on the
    // campaign until this is saved.
    expect(screen.getByText(/1 selected channel/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /save targeting/i }))
    const [, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      motion: 'organic',
      target_channel_keys: 'linkedin_organic',
    })
  })

  it('will not save a motion with no channels selected', async () => {
    stubSession()
    vi.stubGlobal('fetch', patchStub())
    const user = userEvent.setup()

    render(
      <CampaignTargeting
        campaign={CAMPAIGN}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )
    await user.click(screen.getByRole('radio', { name: /^paid$/i }))

    expect(screen.getByRole('button', { name: /save targeting/i })).toBeDisabled()
  })

  it('groups the channels by category so a broad taxonomy stays readable', async () => {
    stubSession()
    vi.stubGlobal('fetch', patchStub())
    const user = userEvent.setup()

    render(
      <CampaignTargeting
        campaign={CAMPAIGN}
        channelLookup={LOOKUP}
        onCampaignUpdated={() => {}}
      />,
    )
    await user.click(screen.getByRole('radio', { name: /both/i }))

    const events = screen.getByRole('group', { name: /events/i })
    expect(within(events).getByRole('checkbox', { name: /Trade show/ })).toBeInTheDocument()
    expect(within(events).queryByRole('checkbox', { name: /LinkedIn/ })).not.toBeInTheDocument()
  })
})
