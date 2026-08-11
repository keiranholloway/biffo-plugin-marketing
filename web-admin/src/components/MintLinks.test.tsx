import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
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
              channel: 'linkedin',
              variant: null,
              is_paid: false,
              url: 'https://dev.tabsii.com/c/tok',
            },
          ],
        }),
      }),
    )

    render(<MintLinks campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Channel'), 'linkedin')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    expect(await screen.findByText('https://dev.tabsii.com/c/tok')).toBeInTheDocument()
  })

  it('posts to the admin app, not generated CRUD, and sends only the channel', async () => {
    // The token and the destination (carrying utm_campaign) are DERIVED
    // server-side. A caller that could supply either would put back the
    // hand-typed utm_campaign this whole feature exists to remove.
    stubSession()
    const user = userEvent.setup()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ links: [] }) })
    vi.stubGlobal('fetch', fetchMock)

    render(<MintLinks campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Channel'), 'instagram')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`/api/v1/plugins/marketing/admin/campaigns/${CAMPAIGN}/links`)
    const sent = JSON.parse(init.body as string).links[0]
    expect(sent).toEqual({ channel: 'instagram', is_paid: false })
    expect(sent.token).toBeUndefined()
    expect(sent.destination_url).toBeUndefined()
  })

  it('explains a 422 as the campaign having no destination', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422 }))

    render(<MintLinks campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Channel'), 'linkedin')
    await user.click(screen.getByRole('button', { name: /mint link/i }))

    expect(await screen.findByText(/no destination URL/i)).toBeInTheDocument()
  })

  it('cannot be submitted without a channel', () => {
    render(<MintLinks campaignId={CAMPAIGN} />)
    expect(screen.getByRole('button', { name: /mint link/i })).toBeDisabled()
  })
})
