import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChannelTaxonomyEntry } from '../lib/api'
import * as auth from '../lib/auth'
import type { ChannelLookup } from '../lib/useChannelTaxonomy'
import { MintLinks } from './MintLinks'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN = 'b3f1c0de-0000-4000-8000-0000000000ab'

const LINKEDIN_ORGANIC: ChannelTaxonomyEntry = {
  key: 'linkedin_organic',
  label: 'LinkedIn — organic',
  motion: 'organic',
  category: 'social',
  ad_platform: null,
  publish_url: 'https://www.linkedin.com/feed/?shareActive=true',
}
const LINKEDIN_PAID: ChannelTaxonomyEntry = {
  key: 'linkedin_paid',
  label: 'LinkedIn ads',
  motion: 'paid',
  category: 'social',
  ad_platform: 'linkedin',
  publish_url: 'https://www.linkedin.com/campaignmanager/',
}

/** A stub taxonomy lookup, matching the shape `useChannelTaxonomy` produces
 * (`ArtefactBody.test.tsx`/`DistributionPack.test.tsx` use the identical
 * helper) — `App.tsx` fetches this once and threads it down, so `MintLinks`
 * itself never calls `listChannels`. */
function fakeLookup(entries: ChannelTaxonomyEntry[], loading = false): ChannelLookup {
  const byKey = new Map(entries.map((e) => [e.key, e]))
  return { get: (key) => byKey.get(key), entries, loading }
}

//: The exact 121-character channel name that overflowed `marketing_link.channel`
//: on dev (#75, #83's own repro) — kept as the realistic long value for the
//: variant-overflow test below, rather than a short hand-typed placeholder.
//: The channel itself can no longer carry a value this long (#84 turned it
//: into a picker over the taxonomy's short keys), but `variant` is still free
//: text up to 64 characters, so the exact same shape of 422 — a FastAPI
//: validation error, `detail` as a list — is still a live, reachable path
//: through this form.
const REALISTIC_OVERLONG_VALUE =
  "Google Search ads (non-brand: terms like 'franchise management software UK', 'franchise operations pricing per location')"

describe('MintLinks', () => {
  it('mints a link and shows the URL to publish', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          links: [
            {
              id: 'l1',
              channel: 'linkedin_organic',
              variant: null,
              is_paid: false,
              url: 'https://dev.tabsii.com/c/tok',
            },
          ],
        }),
      }),
    )

    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([LINKEDIN_ORGANIC])} />)
    await user.selectOptions(screen.getByLabelText('Channel'), 'linkedin_organic')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    expect(await screen.findByText('https://dev.tabsii.com/c/tok')).toBeInTheDocument()
  })

  it('posts to the admin app, not generated CRUD, and sends only the selected key', async () => {
    // The token and the destination (carrying utm_campaign) are DERIVED
    // server-side. A caller that could supply either would put back the
    // hand-typed utm_campaign this whole feature exists to remove. Nor does
    // it send `is_paid` any more (#84) — the server derives it from the
    // channel's own taxonomy row.
    stubSession()
    const user = userEvent.setup()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ links: [] }) })
    vi.stubGlobal('fetch', fetchMock)

    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([LINKEDIN_ORGANIC])} />)
    await user.selectOptions(screen.getByLabelText('Channel'), 'linkedin_organic')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`/api/v1/plugins/marketing/admin/campaigns/${CAMPAIGN}/links`)
    const sent = JSON.parse(init.body as string).links[0]
    expect(sent).toEqual({ channel: 'linkedin_organic' })
    expect(sent.is_paid).toBeUndefined()
    expect(sent.token).toBeUndefined()
    expect(sent.destination_url).toBeUndefined()
  })

  it('only offers channels from the taxonomy — a non-key value cannot be typed or submitted', async () => {
    stubSession()
    render(
      <MintLinks
        campaignId={CAMPAIGN}
        channelLookup={fakeLookup([LINKEDIN_ORGANIC, LINKEDIN_PAID])}
      />,
    )

    const select = screen.getByLabelText('Channel') as HTMLSelectElement
    // A blank placeholder plus exactly the two taxonomy entries — no free-text
    // input exists on this form for #84's "totally made up channel" to go into.
    const options = Array.from(select.options).map((o) => o.value)
    expect(options).toEqual(['', 'linkedin_organic', 'linkedin_paid'])
  })

  it("shows the channel's own motion, and offers no separate paid checkbox", async () => {
    // #84: motion lives on the channel now — picking `linkedin_paid` already
    // says the link is paid, so a second "Paid placement" control would be a
    // second, independently-wrong answer to the same question.
    stubSession()
    const user = userEvent.setup()
    render(
      <MintLinks
        campaignId={CAMPAIGN}
        channelLookup={fakeLookup([LINKEDIN_ORGANIC, LINKEDIN_PAID])}
      />,
    )

    expect(screen.queryByText(/paid placement/i)).not.toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Channel'), 'linkedin_paid')
    expect(screen.getByText(/^paid/i)).toBeInTheDocument()
  })

  it('explains a 422 as the campaign having no destination when nothing else is readable', async () => {
    // The pre-#83 behaviour, still correct when the body genuinely carries
    // nothing usable — never silence, but a named fallback rather than a
    // guess at unstructured text.
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422 }))

    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([LINKEDIN_ORGANIC])} />)
    await user.selectOptions(screen.getByLabelText('Channel'), 'linkedin_organic')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    expect(await screen.findByText(/no destination URL/i)).toBeInTheDocument()
  })

  it('names the field and the constraint on a real FastAPI validation 422, rather than showing nothing (#83)', async () => {
    // This is the actual defect: a `Field` constraint 422 (here, `variant`
    // over its 64-character ceiling) has `detail` as a LIST of pydantic
    // error objects, not the hand-authored string the UI used to assume.
    // Before the fix this rendered no error at all — not the wrong text,
    // nothing, which is why the button looked unresponsive.
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: [
            {
              type: 'string_too_long',
              loc: ['body', 'links', 0, 'variant'],
              msg: 'String should have at most 64 characters',
            },
          ],
        }),
      }),
    )

    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([LINKEDIN_ORGANIC])} />)
    await user.selectOptions(screen.getByLabelText('Channel'), 'linkedin_organic')
    await user.type(screen.getByLabelText('Variant (optional)'), REALISTIC_OVERLONG_VALUE)
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    // Matched on the constraint text alone — "variant" also appears in the
    // field's own <label>, so a regex naming just the field would ambiguously
    // match both.
    const error = await screen.findByText(/64 characters/)
    expect(error.textContent).toMatch(/variant/i)
  })

  it('cannot be submitted without a channel selected', () => {
    stubSession()
    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([LINKEDIN_ORGANIC])} />)
    expect(screen.getByRole('button', { name: /mint link/i })).toBeDisabled()
  })

  it('disables the picker and says so while the taxonomy is still loading', () => {
    stubSession()
    render(<MintLinks campaignId={CAMPAIGN} channelLookup={fakeLookup([], true)} />)
    expect(screen.getByLabelText('Channel')).toBeDisabled()
    expect(screen.getByText(/loading channels/i)).toBeInTheDocument()
  })
})
