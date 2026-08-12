import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import * as auth from '../lib/auth'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
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

/** jsdom has no `navigator.clipboard` at all, so `useClipboard`'s real
 * `navigator.clipboard.writeText` would throw and every copy button would
 * quietly land on its "could not copy" error path — this stands in the same
 * seam a real browser provides, so a `CopyButton` click here exercises the
 * exact code path production does. */
function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
  return writeText
}

/** Matching `DistributionPack.test.tsx`'s own `makeLookup` — `channelLookup`
 * is fetched once by `CampaignDetail` and threaded down, so `PaidPack`
 * itself never fetches `/channels`. */
function makeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

const FACEBOOK_PAID: ChannelTaxonomyEntry = {
  key: 'facebook_paid',
  label: 'Facebook ads',
  motion: 'paid',
  category: 'social',
  ad_platform: 'meta',
  publish_url: 'https://adsmanager.facebook.com/adsmanager/',
}

const CAMPAIGN = 'c1'

describe('PaidPack', () => {
  it('shows trimmed ad copy, targeting, budget, and spend as explicitly unmeasurable', async () => {
    stubSession()
    // `userEvent.setup()` installs (and resets) its own `navigator.clipboard`
    // stub, so this must be stubbed AFTER setup(), not before — otherwise
    // `useClipboard` silently talks to userEvent's stub, not this test's.
    const user = userEvent.setup()
    const writeText = stubClipboard()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaign_id: CAMPAIGN,
          ad_copy: [
            {
              channel: 'facebook_paid',
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
          links: [{ channel: 'facebook_paid', variant: null, is_paid: true, url: 'https://x/c/fb1' }],
          guidance: '',
          spend: {
            measurable: false,
            denominator: null,
            reason: 'No route reachable from this plugin exposes this yet.',
          },
        }),
      }),
    )

    render(<PaidPack campaignId={CAMPAIGN} channelLookup={makeLookup([FACEBOOK_PAID])} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/missing renders for: feed_1x1/i)).toBeInTheDocument()
    expect(screen.getByText(/trimmed to 125 chars/i)).toBeInTheDocument()
    expect(screen.getByText('Busy owners')).toBeInTheDocument()
    // Grounded, but the full {url, note} pairs are not repeated here — only
    // a count, pointing back at the positioning artefact for the citations.
    expect(screen.getByText(/grounded in 2 research sources/i)).toBeInTheDocument()
    expect(screen.getByText(/1 paid channel\(s\)/)).toBeInTheDocument()
    expect(screen.getByText(/not measurable — no route reachable/i)).toBeInTheDocument()

    // #103a: a copy control per copy block — the trimmed body must say so in
    // its own control, not just in the inline "(trimmed to ... chars)" text.
    expect(screen.getByRole('button', { name: 'Copy headline' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy body (trimmed)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy CTA' })).toBeInTheDocument()

    // #103a: copy-all composes headline + body + CTA + the channel's own
    // tracked link, in that order, joined by blank lines — never silently
    // dropping the fact that the body was trimmed to fit the platform limit.
    const copyAll = screen.getByRole('button', { name: /copy all for this channel \(includes trimmed copy\)/i })
    await user.click(copyAll)
    expect(writeText).toHaveBeenCalledWith(
      ['Fast onboarding, done right', 'A'.repeat(130), 'Learn more', 'https://x/c/fb1'].join('\n\n'),
    )
    expect(await screen.findByText('Copied all')).toBeInTheDocument()

    // #103b: the publish link comes from the taxonomy, opens in a new tab,
    // and never carries a referrer back to this pack.
    const publishLink = screen.getByRole('link', { name: /publish on facebook ads/i })
    expect(publishLink).toHaveAttribute('href', 'https://adsmanager.facebook.com/adsmanager/')
    expect(publishLink).toHaveAttribute('target', '_blank')
    expect(publishLink).toHaveAttribute('rel', 'noopener noreferrer')
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

    render(<PaidPack campaignId={CAMPAIGN} channelLookup={makeLookup([])} />)
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

    render(<PaidPack campaignId={CAMPAIGN} channelLookup={makeLookup([])} />)
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

    render(<PaidPack campaignId={CAMPAIGN} channelLookup={makeLookup([])} />)
    await user.click(screen.getByRole('button', { name: /load paid pack/i }))

    expect(await screen.findByText(/copy artefact must be approved before this can proceed/i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the paid pack \(409\)$/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/"detail"/)).not.toBeInTheDocument()
  })
})
