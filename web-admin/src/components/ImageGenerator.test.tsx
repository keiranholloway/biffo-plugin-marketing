import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { ImageGenerator } from './ImageGenerator'

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

interface Reply {
  ok: boolean
  status?: number
  body: unknown
}

const NO_ASSETS: Reply = {
  ok: true,
  body: { campaign_id: CAMPAIGN, assets: [], missing_placements: [], superseded_source_count: 0 },
}

const STILL: Reply = {
  ok: true,
  body: {
    asset: { id: 'as1' },
    media: { id: 'm1' },
    url: 'https://example.com/still.png',
    ledger: { id: 'l1', cost_usd: 0.04, unpriced: false },
  },
}

/** Routes on path + method, rather than one blanket `mockResolvedValue`: this
 * panel now makes two different calls (the on-mount assets read added for
 * #102, and the generation POST), and a blanket stub would answer both with
 * the same body — which is how a test can pass while the on-mount read is
 * not actually being made at all. `assets` may be a list, consumed one reply
 * per call, so a test can say what the refresh after a generation returns. */
function stubFetch(routes: { assets?: Reply | Reply[]; generate?: Reply }) {
  const assetReplies = Array.isArray(routes.assets)
    ? [...routes.assets]
    : [routes.assets ?? NO_ASSETS]
  const calls: string[] = []
  const mock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    calls.push(`${method} ${url}`)
    if (method === 'GET' && url.endsWith(`/campaigns/${CAMPAIGN}/assets`)) {
      const reply = assetReplies.length > 1 ? (assetReplies.shift() as Reply) : assetReplies[0]
      return Promise.resolve({ ok: reply.ok, status: reply.status ?? 200, json: async () => reply.body })
    }
    if (method === 'POST' && url.endsWith(`/campaigns/${CAMPAIGN}/stills`) && routes.generate) {
      const reply = routes.generate
      return Promise.resolve({ ok: reply.ok, status: reply.status ?? 200, json: async () => reply.body })
    }
    return Promise.reject(new Error(`unexpected ${method} ${url}`))
  })
  vi.stubGlobal('fetch', mock)
  return calls
}

describe('ImageGenerator', () => {
  it('generates a still and shows it', async () => {
    stubSession()
    const user = userEvent.setup()
    stubFetch({ generate: STILL })

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByAltText('Generated still')).toHaveAttribute(
      'src',
      'https://example.com/still.png',
    )
    expect(screen.getByText('Cost: $0.0400')).toBeInTheDocument()
  })

  it('shows "unpriced", never $0, when cost_usd is null', async () => {
    // The null case is load-bearing — image_routes.py's own reasoning.
    stubSession()
    const user = userEvent.setup()
    stubFetch({
      generate: {
        ok: true,
        body: {
          asset: { id: 'as1' },
          media: { id: 'm1' },
          url: 'https://example.com/still.png',
          ledger: { id: 'l1', cost_usd: null, unpriced: true },
        },
      },
    })

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByText('Cost: unpriced')).toBeInTheDocument()
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument()
  })

  it('cannot be submitted with an empty prompt', async () => {
    stubSession()
    stubFetch({})
    render(<ImageGenerator campaignId={CAMPAIGN} />)
    expect(screen.getByRole('button', { name: /generate still/i })).toBeDisabled()
    // Awaited so the on-mount assets read settles inside the test rather than
    // after it — an unawaited effect is only a React `act` warning today, but
    // it is also a state update landing on an unmounted tree.
    await screen.findByText(/no creative for this campaign yet/i)
  })

  it('surfaces a provider failure rather than silently doing nothing', async () => {
    stubSession()
    const user = userEvent.setup()
    stubFetch({
      generate: { ok: false, status: 502, body: { detail: 'the image provider returned an error' } },
    })

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByText(/image provider returned an error/i)).toBeInTheDocument()
  })

  // ── #102: creative that already exists must be visible on load ────────────

  it("shows creative generated in an earlier session, and never claims there is none", async () => {
    // The defect this panel was filed for. Generation is the one billable,
    // irreversible step in the plugin, and an operator who cannot see the
    // still that already exists pays to make it again.
    stubSession()
    stubFetch({
      assets: {
        ok: true,
        body: {
          campaign_id: CAMPAIGN,
          assets: [
            {
              id: 'a-source',
              campaign_id: CAMPAIGN,
              media_kind: 'image',
              placement: null,
              media_id: 'm-source',
              is_source: true,
              url: 'https://example.com/source.jpg',
            },
            {
              id: 'a-story',
              campaign_id: CAMPAIGN,
              media_kind: 'image',
              placement: 'instagram_story',
              media_id: 'm-story',
              is_source: false,
              url: 'https://example.com/story.jpg',
            },
          ],
          missing_placements: [],
          superseded_source_count: 0,
        },
      },
    })

    render(<ImageGenerator campaignId={CAMPAIGN} />)

    expect(await screen.findByAltText('Source creative')).toHaveAttribute(
      'src',
      'https://example.com/source.jpg',
    )
    expect(screen.getByAltText('instagram_story')).toHaveAttribute(
      'src',
      'https://example.com/story.jpg',
    )
    expect(screen.queryByText(/no creative for this campaign yet/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/this session/i)).not.toBeInTheDocument()
  })

  it('says the campaign has no creative only when the read actually came back empty', async () => {
    stubSession()
    stubFetch({ assets: NO_ASSETS })

    render(<ImageGenerator campaignId={CAMPAIGN} />)

    expect(await screen.findByText(/no creative for this campaign yet/i)).toBeInTheDocument()
  })

  it('does not report "no creative" when the check itself failed', async () => {
    // "We could not check" and "there is nothing here" differ by the price of
    // one generation, so a failed read must never render as the empty state.
    stubSession()
    stubFetch({ assets: { ok: false, status: 502, body: { detail: 'storage is unavailable' } } })

    render(<ImageGenerator campaignId={CAMPAIGN} />)

    expect(await screen.findByText(/could not check this campaign for existing creative/i))
      .toBeInTheDocument()
    expect(screen.getByText(/storage is unavailable/)).toBeInTheDocument()
    expect(screen.queryByText(/no creative for this campaign yet/i)).not.toBeInTheDocument()
  })

  it('re-reads the campaign after generating, and shows the new still once', async () => {
    // `generate_still_route` writes the placement renders (#36) after it
    // returns the source, so the refresh is what surfaces them — but the
    // source row it returns is now in both lists, and must render once.
    stubSession()
    const user = userEvent.setup()
    stubFetch({
      generate: STILL,
      assets: [
        NO_ASSETS,
        {
          ok: true,
          body: {
            campaign_id: CAMPAIGN,
            assets: [
              {
                id: 'as1',
                campaign_id: CAMPAIGN,
                media_kind: 'image',
                placement: null,
                media_id: 'm1',
                is_source: true,
                url: 'https://example.com/still.png',
              },
            ],
            missing_placements: [],
            superseded_source_count: 0,
          },
        },
      ],
    })

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByAltText('Generated still')).toBeInTheDocument()
    expect(await screen.findAllByRole('img')).toHaveLength(1)
    expect(screen.queryByAltText('Source creative')).not.toBeInTheDocument()
  })
})
