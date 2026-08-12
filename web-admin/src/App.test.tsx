import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'

afterEach(() => vi.unstubAllGlobals())

describe('App', () => {
  it('says so plainly when there are no campaigns yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] }))
    render(<App />)
    expect(await screen.findByText(/No campaigns yet/)).toBeInTheDocument()
  })

  it('lists campaigns it receives', async () => {
    // Routed by URL, not a single blanket response — `App` now also fetches
    // `/channels` (`useChannelTaxonomy`, #84), and a campaign row is not a
    // `ChannelTaxonomyEntry`: reusing the campaigns array for both used to
    // hand `MintLinks` a list of `{key: undefined}` "channels".
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (typeof url === 'string' && url.endsWith('/channels')) {
          return Promise.resolve({ ok: true, json: async () => [] })
        }
        return Promise.resolve({
          ok: true,
          json: async () => [
            { id: '1', name: 'Spring demo push', status: 'draft', destination_url: null },
          ],
        })
      }),
    )
    render(<App />)
    expect(await screen.findByText('Spring demo push')).toBeInTheDocument()
  })

  it('reports a failure instead of rendering nothing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }))
    render(<App />)
    expect(await screen.findByText(/Could not load campaigns/)).toBeInTheDocument()
  })
})

describe('creating a campaign', () => {
  it('submits the form and refreshes the list', async () => {
    // Dispatched by URL/method, not call order (`CampaignDetail.test.tsx`'s
    // own pattern): `App` now also fetches `/channels` via
    // `useChannelTaxonomy` (#84), whose effect can fire before or after
    // `load()`'s — a fixed three-call queue silently mis-fed the taxonomy
    // fetch a `Campaign` and vice versa the moment a second concurrent
    // caller was added.
    let campaigns: unknown[] = []
    const user = userEvent.setup()
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.endsWith('/channels')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      if (typeof url === 'string' && url.endsWith('/campaigns') && init?.method === 'POST') {
        campaigns = [{ id: '1', name: 'Spring', status: 'draft', destination_url: 'https://x/demo' }]
        return Promise.resolve({ ok: true, json: async () => ({ id: '1' }) })
      }
      return Promise.resolve({ ok: true, json: async () => campaigns })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText(/No campaigns yet/)

    await user.type(screen.getByLabelText('Name'), 'Spring')
    await user.type(screen.getByLabelText('Destination URL'), 'https://x/demo')
    await user.click(screen.getByRole('button', { name: /create campaign/i }))

    expect(await screen.findByText('Spring')).toBeInTheDocument()
  })

  it('cannot be submitted with an empty field', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] }))
    render(<App />)
    expect(screen.getByRole('button', { name: /create campaign/i })).toBeDisabled()
  })
})

describe('opening a campaign studio', () => {
  it('switches to the campaign detail view, and back again', async () => {
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === 'string' && url.endsWith('/channels')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      return Promise.resolve({
        ok: true,
        json: async () => [
          { id: '1', name: 'Spring demo push', status: 'draft', destination_url: null, brief: null },
        ],
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Spring demo push')

    await user.click(screen.getByRole('button', { name: /^open$/i }))
    expect(screen.getByRole('heading', { name: 'Spring demo push' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Pipeline' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /back to campaigns/i }))
    expect(screen.getByRole('heading', { name: 'Campaign studio' })).toBeInTheDocument()
  })
})

describe('minting a tracked link from the campaign list', () => {
  it('keeps the mint form collapsed behind a disclosure, and reveals it on click', async () => {
    // A row used to embed the full channel picker inline, which is a per-row
    // ACTION masquerading as table content. It is now a `<details>`
    // disclosure — closed by default so the row stays one line, opened on
    // demand. Reverting to always-inline would still pass every other test
    // in this file, since none of them assert on collapsed/expanded state.
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === 'string' && url.endsWith('/channels')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      return Promise.resolve({
        ok: true,
        json: async () => [
          { id: '1', name: 'Spring demo push', status: 'draft', destination_url: null },
        ],
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Spring demo push')

    expect(screen.getByLabelText('Channel')).not.toBeVisible()

    // Selector-scoped: "Mint link" is also the (currently hidden) submit
    // button's own text, so an unscoped `getByText` matches both.
    await user.click(screen.getByText('Mint link', { selector: 'summary' }))
    expect(screen.getByLabelText('Channel')).toBeVisible()
  })
})
