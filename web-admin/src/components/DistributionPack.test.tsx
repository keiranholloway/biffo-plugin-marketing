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

/** jsdom has no `navigator.clipboard` at all — see `PaidPack.test.tsx`'s own
 * copy of this helper for why a click must go through a real stub rather
 * than nothing, or every copy button here would silently land on
 * `useClipboard`'s "could not copy" error path instead of the success one
 * these tests are checking. */
function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
  return writeText
}

/** `channelLookup` is fetched once by `CampaignDetail` (`useChannelTaxonomy`)
 * and passed down — `DistributionPack` itself never fetches `/channels`, so
 * these tests only need this stub, not a second mocked response. */
function makeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

const LINKEDIN: ChannelTaxonomyEntry = {
  key: 'linkedin_organic',
  label: 'LinkedIn — organic',
  motion: 'organic',
  category: 'social',
  ad_platform: null,
  publish_url: 'https://www.linkedin.com/feed/?shareActive=true',
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/no public base url configured/i)).toBeInTheDocument()
  })

  // The three distinct 4xx branches `pack_routes.get_pack_route` actually
  // raises (issue #85) — each asserts the operator-facing wording the server
  // sent, not the status code, and not the response's raw JSON. The fixture
  // `detail` strings are copied verbatim from `pack_routes.py` rather than
  // invented, per the issue's own warning that a fixture built from what the
  // author expects, rather than what the server produces, is the recurring
  // defect here.
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/no copy artefact for this campaign yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the distribution pack \(404\)$/i)).not.toBeInTheDocument()
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/^campaign not found\./i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the distribution pack \(404\)$/i)).not.toBeInTheDocument()
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/copy artefact must be approved before this can proceed/i)).toBeInTheDocument()
    expect(screen.queryByText(/^could not load the distribution pack \(409\)$/i)).not.toBeInTheDocument()
    // No raw JSON — only the string `detail` field, never the object itself.
    expect(screen.queryByText(/"detail"/)).not.toBeInTheDocument()
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
    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([])} />)
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([], true)} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    expect(await screen.findByText(/loading channel/i)).toBeInTheDocument()
    expect(screen.queryByText(/unrecognised channel/i)).not.toBeInTheDocument()
  })

  // #103a/#103b: a copy control per copy block, a copy-all control that
  // composes the channel's own tracked link in, and a publish link sourced
  // from the taxonomy — the operator-experience gaps issue #103 reports.
  it('gives every copy block its own copy button, composes copy-all with the tracked link, and links to where to publish', async () => {
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
          assets: [],
          missing_placements: [],
          copy: [
            {
              channel_key: 'linkedin_organic',
              motion: 'organic',
              headline: 'Fast onboarding, done right',
              body: 'Get every franchise unit live in a day.',
              cta: 'Book a demo',
              sources: [],
            },
          ],
          links: [{ channel: 'linkedin_organic', variant: null, is_paid: false, url: 'https://x/c/tok' }],
          guidance: '',
        }),
      }),
    )

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([LINKEDIN])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))
    await screen.findByText('Fast onboarding, done right')

    // A copy control per block.
    await user.click(screen.getByRole('button', { name: 'Copy headline' }))
    expect(writeText).toHaveBeenLastCalledWith('Fast onboarding, done right')
    await user.click(screen.getByRole('button', { name: 'Copy body' }))
    expect(writeText).toHaveBeenLastCalledWith('Get every franchise unit live in a day.')
    await user.click(screen.getByRole('button', { name: 'Copy CTA' }))
    expect(writeText).toHaveBeenLastCalledWith('Book a demo')

    // Copy-all composes headline + body + CTA + this channel's own tracked
    // link — the real unit of work when pasting into a composer (#103a).
    await user.click(screen.getByRole('button', { name: 'Copy all for this channel' }))
    expect(writeText).toHaveBeenLastCalledWith(
      ['Fast onboarding, done right', 'Get every franchise unit live in a day.', 'Book a demo', 'https://x/c/tok'].join(
        '\n\n',
      ),
    )

    // The publish link comes from the taxonomy (`LINKEDIN.publish_url`), not
    // from anything hardcoded in this component (#103b).
    const publishLink = screen.getByRole('link', { name: /publish on linkedin — organic/i })
    expect(publishLink).toHaveAttribute('href', 'https://www.linkedin.com/feed/?shareActive=true')
    expect(publishLink).toHaveAttribute('target', '_blank')
    expect(publishLink).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('renders no publish link for a channel with no publish_url, never a dead or placeholder link', async () => {
    stubSession()
    const user = userEvent.setup()
    const NO_COMPOSER: ChannelTaxonomyEntry = {
      key: 'trade_press_earned',
      label: 'Trade press — earned coverage',
      motion: 'organic',
      category: 'trade_press',
      ad_platform: null,
      publish_url: null,
    }
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
              channel_key: 'trade_press_earned',
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

    render(<DistributionPack campaignId={CAMPAIGN} campaignName="Spring Launch" channelLookup={makeLookup([NO_COMPOSER])} />)
    await user.click(screen.getByRole('button', { name: /load pack/i }))
    await screen.findByText('Trade press — earned coverage')

    expect(screen.queryByRole('link', { name: /publish on/i })).not.toBeInTheDocument()
  })

  // #117: the pack exists so an operator standing in a venue can get the
  // creative out of it. Until this, the only affordance was long-pressing
  // the `<img>` and hoping.
  it('offers a download per creative asset, named for the campaign rather than the storage key', async () => {
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
              url: 'https://bucket.s3.amazonaws.com/plugins/marketing/9f1c.png?X-Amz-Signature=a',
            },
            {
              id: 'as2',
              campaign_id: CAMPAIGN,
              media_kind: 'image',
              placement: 'feed_1x1',
              media_id: 'm2',
              is_source: false,
              url: 'https://bucket.s3.amazonaws.com/plugins/marketing/7b2d.png?X-Amz-Signature=b',
            },
          ],
          missing_placements: [],
          copy: [],
          links: [],
          guidance: '',
        }),
      }),
    )

    render(
      <DistributionPack
        campaignId={CAMPAIGN}
        campaignName="Spring Launch"
        channelLookup={makeLookup([LINKEDIN])}
      />,
    )
    await user.click(screen.getByRole('button', { name: /load pack/i }))

    const source = await screen.findByRole('link', { name: /download source creative/i })
    // Built from the URL the page already holds — a presign is minted per
    // read and cannot be re-derived here (`pack_routes._asset_with_url`).
    expect(source).toHaveAttribute(
      'href',
      'https://bucket.s3.amazonaws.com/plugins/marketing/9f1c.png?X-Amz-Signature=a',
    )
    expect(source).toHaveAttribute('download', 'spring-launch-source.png')

    const placed = screen.getByRole('link', { name: /download feed_1x1 creative/i })
    expect(placed).toHaveAttribute(
      'href',
      'https://bucket.s3.amazonaws.com/plugins/marketing/7b2d.png?X-Amz-Signature=b',
    )
    expect(placed).toHaveAttribute('download', 'spring-launch-feed-1x1.png')
  })
})
