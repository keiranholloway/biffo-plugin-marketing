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

describe('destination URL suggestions (#129)', () => {
  // Distinct destination URLs from campaigns already on the page, offered
  // through the field's own `list` attribute (a <datalist>) — the field
  // stays free text (nothing here restricts what can be typed/submitted),
  // it just now suggests what this tenant has used before rather than
  // requiring every campaign to retype it from scratch.
  it('offers previously-used destination URLs as suggestions, deduplicated', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (typeof url === 'string' && url.endsWith('/channels')) {
          return Promise.resolve({ ok: true, json: async () => [] })
        }
        return Promise.resolve({
          ok: true,
          json: async () => [
            { id: '1', name: 'Spring', status: 'draft', destination_url: 'https://x.test/a' },
            { id: '2', name: 'Summer', status: 'draft', destination_url: 'https://x.test/b' },
            { id: '3', name: 'Autumn', status: 'draft', destination_url: 'https://x.test/a' },
          ],
        })
      }),
    )
    render(<App />)
    await screen.findByText('Spring')

    const input = screen.getByLabelText('Destination URL')
    const listId = input.getAttribute('list')
    expect(listId).not.toBeNull()

    // A <datalist>'s <option>s are not exposed through any accessible-name
    // query; reading them structurally is the only way to assert on them.
    const datalist = document.getElementById(listId!)
    expect(datalist).not.toBeNull()
    const values = Array.from(datalist!.querySelectorAll('option')).map((o) => o.getAttribute('value'))
    expect(values).toEqual(['https://x.test/a', 'https://x.test/b'])
  })

  it('offers nothing when no campaign has a destination URL yet', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (typeof url === 'string' && url.endsWith('/channels')) {
          return Promise.resolve({ ok: true, json: async () => [] })
        }
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: '1', name: 'Spring', status: 'draft', destination_url: null }],
        })
      }),
    )
    render(<App />)
    await screen.findByText('Spring')

    const input = screen.getByLabelText('Destination URL')
    const listId = input.getAttribute('list')
    const datalist = listId !== null ? document.getElementById(listId) : null
    expect(datalist?.querySelectorAll('option').length ?? 0).toBe(0)
  })

  it('still accepts free text outside the suggestion list', async () => {
    let campaigns: unknown[] = [
      { id: '1', name: 'Spring', status: 'draft', destination_url: 'https://x.test/a' },
    ]
    const user = userEvent.setup()
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.endsWith('/channels')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      if (typeof url === 'string' && url.endsWith('/campaigns') && init?.method === 'POST') {
        campaigns = [...campaigns, { id: '2', name: 'New', status: 'draft', destination_url: 'https://x.test/new' }]
        return Promise.resolve({ ok: true, json: async () => ({ id: '2' }) })
      }
      return Promise.resolve({ ok: true, json: async () => campaigns })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText('Spring')

    await user.type(screen.getByLabelText('Name'), 'New')
    await user.type(screen.getByLabelText('Destination URL'), 'https://x.test/new')
    await user.click(screen.getByRole('button', { name: /create campaign/i }))

    expect(await screen.findByText('New')).toBeInTheDocument()
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

describe('deep linking to a campaign (#142)', () => {
  const ID = '5380e9cf-457f-4932-a7ff-c1c6986524e5'

  /** Campaigns fetch, with `/channels` routed separately — see the note on
   *  'lists campaigns it receives' for why a blanket response is wrong. */
  function stubFetch(campaigns: unknown[]) {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        typeof url === 'string' && url.endsWith('/channels')
          ? Promise.resolve({ ok: true, json: async () => [] })
          : Promise.resolve({ ok: true, json: async () => campaigns }),
      ),
    )
  }

  /** Put the browser on a URL asking for `campaign`, as a shared link would. */
  function locationAsking(campaign: string | null) {
    const search = campaign === null ? '' : `?campaign=${campaign}`
    window.history.replaceState(null, '', `/api/v1/plugins/marketing/admin${search}`)
  }

  afterEach(() => locationAsking(null))

  it('opens the campaign a shared link names, without anyone clicking', async () => {
    // The whole point: a colleague pastes the URL and lands on the campaign.
    locationAsking(ID)
    stubFetch([{ id: ID, name: 'Shared campaign', status: 'draft', destination_url: null }])

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Shared campaign' })).toBeInTheDocument()
  })

  it('says why when the link names a campaign this reader cannot see', async () => {
    // Deleted, or another tenant's: it is simply absent from the tenant-scoped
    // list. Rendering an empty studio would be the wrong answer.
    locationAsking(ID)
    stubFetch([{ id: 'another-id', name: 'Not the one', status: 'draft', destination_url: null }])

    render(<App />)

    expect(await screen.findByText(/could not be opened/i)).toBeInTheDocument()
  })

  it('ignores a junk id and shows the list rather than an error page', async () => {
    window.history.replaceState(null, '', '/api/v1/plugins/marketing/admin?campaign=nonsense')
    stubFetch([{ id: ID, name: 'Spring demo push', status: 'draft', destination_url: null }])

    render(<App />)

    expect(await screen.findByText('Spring demo push')).toBeInTheDocument()
    expect(screen.queryByText(/could not be opened/i)).not.toBeInTheDocument()
  })

  it('puts the campaign in the address bar when one is opened', async () => {
    stubFetch([{ id: ID, name: 'Spring demo push', status: 'draft', destination_url: null }])
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: 'Open' }))

    expect(window.location.search).toBe(`?campaign=${ID}`)
  })

  it('takes it back out when the campaign is closed', async () => {
    // A stale `?campaign=` on the list view would make the address bar
    // disagree with the screen.
    stubFetch([{ id: ID, name: 'Spring demo push', status: 'draft', destination_url: null }])
    render(<App />)

    await userEvent.click(await screen.findByRole('button', { name: 'Open' }))
    // Asserted before closing so this cannot pass vacuously: without the
    // feature the search string is empty at BOTH points, and a test that only
    // checked the end state would report success against no implementation.
    expect(window.location.search).toBe(`?campaign=${ID}`)

    await userEvent.click(await screen.findByRole('button', { name: /Back to campaigns/i }))

    expect(window.location.search).toBe('')
  })
})
