import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  approveArtefact,
  createCampaign,
  generateStill,
  getArtefact,
  getPack,
  getResults,
  listCampaigns,
  listChannels,
  mintLinks,
  parseArtefactBody,
  startResearch,
  updateCampaign,
  type ResearchSynthesisBody,
} from './api'
import * as auth from './auth'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The admin panel runs behind the portal's shared Cognito session; these tests
 * stub it rather than standing up a real pool. */
function stubSession(jwt: string | null = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue(
    jwt === null
      ? null
      : ({ getIdToken: () => ({ getJwtToken: () => jwt }) } as never),
  )
}

describe('listCampaigns', () => {
  it('calls the plugin API relative to the origin, not an absolute host', async () => {
    stubSession()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)

    await listCampaigns()

    const url = fetchMock.mock.calls[0][0] as string
    expect(url).toBe('/api/v1/plugins/marketing/campaigns')
    expect(url.startsWith('http')).toBe(false)
  })

  it('throws with the status, never the response body', async () => {
    // A body can be an HTML error page or an authorization message. Rendering
    // it is how another plugin showed `{"detail":"Administrator access
    // required"}` where its content belonged.
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({ detail: 'Administrator access required' }),
      }),
    )

    await expect(listCampaigns()).rejects.toThrow(/403/)
    await expect(listCampaigns()).rejects.not.toThrow(/Administrator/)
  })

  it('returns an empty list when the body is not an array', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }))
    await expect(listCampaigns()).resolves.toEqual([])
  })
})

describe('listCampaigns authorization', () => {
  it('sends the Cognito id token as a bearer, not cookies', async () => {
    // The first version used `credentials: 'include'`. API Gateway's
    // /api/v1/plugins/* route is JWT-authorized, so every call 401'd and the
    // panel rendered "Could not load campaigns: request failed (401)".
    stubSession('abc123')
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)

    await listCampaigns()

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer abc123')
    expect(init.credentials).toBeUndefined()
  })

  it('says plainly that the caller is not signed in on a 401', async () => {
    stubSession(null)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }))
    await expect(listCampaigns()).rejects.toThrow(/not signed in/)
  })
})

describe('createCampaign', () => {
  it('posts the campaign with a bearer and starts it at draft', async () => {
    stubSession('abc123')
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ id: '1', name: 'x', status: 'draft' }) })
    vi.stubGlobal('fetch', fetchMock)

    await createCampaign({ name: 'Spring', destination_url: 'https://example.com/demo' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/plugins/marketing/campaigns')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer abc123')
    // status is the pipeline's vocabulary, not the operator's — a new campaign
    // always starts at draft, so the form does not offer it.
    expect(JSON.parse(init.body as string).status).toBe('draft')
  })

  it('names the missing admin role on a 403, rather than a bare status', async () => {
    // `marketing_campaign`'s create permission requires `admin`, evaluated by
    // Core. Without this the form silently appears not to work.
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403 }))
    await expect(
      createCampaign({ name: 'x', destination_url: 'https://example.com' }),
    ).rejects.toThrow(/admin role/)
  })
})

describe('updateCampaign', () => {
  it('PATCHes generated CRUD, not an admin-app route', async () => {
    stubSession('abc123')
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 'c1' }) })
    vi.stubGlobal('fetch', fetchMock)

    await updateCampaign('c1', { brief: 'Who this is for, and why now.' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/plugins/marketing/campaigns/c1')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({ brief: 'Who this is for, and why now.' })
  })
})

describe('pipeline artefacts', () => {
  it('returns null on a 404 rather than throwing — "not started yet" is not an error', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }))
    await expect(getArtefact('c1', 'research')).resolves.toBeNull()
  })

  it('starts research at the admin-app route, not generated CRUD', async () => {
    stubSession('abc123')
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ id: 'a1', status: 'pending' }) })
    vi.stubGlobal('fetch', fetchMock)

    await startResearch('c1')

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/plugins/marketing/admin/campaigns/c1/research')
  })

  it('surfaces the server-authored reason on a 422, not a bare status', async () => {
    // Unlike listCampaigns/createCampaign above, this hits the plugin's OWN
    // admin route — the detail text is authored specifically for an
    // operator to read (admin_app.py's start_research_route), not Core's
    // generic-CRUD wording, so surfacing it is the point.
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ detail: 'This campaign has no brief, so there is nothing to research.' }),
      }),
    )
    await expect(startResearch('c1')).rejects.toThrow(/no brief/)
  })

  it('names the missing group on a 403 from the admin app', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({ detail: "This surface requires the 'admin' group." }),
      }),
    )
    await expect(startResearch('c1')).rejects.toThrow(/admin' group/)
  })

  it('approves at the artefact-scoped route', async () => {
    stubSession('abc123')
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ id: 'a1', status: 'approved' }) })
    vi.stubGlobal('fetch', fetchMock)

    await approveArtefact('c1', 'channel_plan')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/plugins/marketing/admin/campaigns/c1/artefacts/channel_plan/approve')
    expect(init.method).toBe('POST')
  })
})

describe('mintLinks', () => {
  it('sends only the channel key (and variant, if given) — no is_paid (#84)', async () => {
    stubSession('abc123')
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ links: [] }) })
    vi.stubGlobal('fetch', fetchMock)

    await mintLinks('c1', [{ channel: 'linkedin_organic' }])

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/plugins/marketing/admin/campaigns/c1/links')
    expect(JSON.parse(init.body as string)).toEqual({ links: [{ channel: 'linkedin_organic' }] })
  })

  it('surfaces a FastAPI validation error verbatim from the real API shape (#83)', async () => {
    // The exact response the API returns for an over-long channel (issue
    // #83's own repro): `detail` is a LIST of pydantic error objects, not
    // the hand-authored string every other endpoint returns. This is a
    // direct API caller's route to the bug — the web-admin picker (#84)
    // makes an over-long CHANNEL unreachable through the form, but this is
    // the shape the whole plugin can return, which is why the fix lives in
    // `detailMessage`/`onError`, not in one component.
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: [
            {
              type: 'string_too_long',
              loc: ['body', 'links', 0, 'channel'],
              msg: 'String should have at most 64 characters',
              ctx: { max_length: 64 },
            },
          ],
        }),
      }),
    )

    await expect(mintLinks('c1', [{ channel: 'x'.repeat(80) }])).rejects.toThrow(
      /channel: String should have at most 64 characters \(422\)/,
    )
  })

  it('surfaces the hand-authored string detail for an unrecognised channel key (#84)', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: "Not a recognised channel key: totally made up channel. Valid channel keys: linkedin_organic.",
        }),
      }),
    )

    await expect(mintLinks('c1', [{ channel: 'totally made up channel' }])).rejects.toThrow(
      /not a recognised channel key/i,
    )
  })
})

describe('parseArtefactBody', () => {
  it('parses the JSON-serialised body column into its structured shape', () => {
    const body: ResearchSynthesisBody = { summary: 'x', findings: [] }
    const parsed = parseArtefactBody<ResearchSynthesisBody>({
      id: 'a1',
      campaign_id: 'c1',
      kind: 'research',
      status: 'proposed',
      body: JSON.stringify(body),
      citations: null,
      causation_id: null,
      agent_run_id: null,
    })
    expect(parsed).toEqual(body)
  })

  it('returns null rather than throwing on no artefact, no body, or unparseable JSON', () => {
    expect(parseArtefactBody(null)).toBeNull()
    expect(
      parseArtefactBody({
        id: 'a1',
        campaign_id: 'c1',
        kind: 'research',
        status: 'pending',
        body: null,
        citations: null,
        causation_id: null,
        agent_run_id: null,
      }),
    ).toBeNull()
  })
})

describe('generateStill', () => {
  it('reports a null cost_usd as unpriced, never as $0', async () => {
    // The null case is load-bearing (image_routes.py) — a caller must never
    // read "cost_usd is null" as "this cost nothing".
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          asset: { id: 'as1' },
          media: { id: 'm1' },
          url: 'https://example.com/still.png',
          ledger: { id: 'l1', cost_usd: null, unpriced: true },
        }),
      }),
    )
    const result = await generateStill('c1', 'a storefront photo')
    expect(result.ledger.cost_usd).toBeNull()
    expect(result.ledger.unpriced).toBe(true)
  })
})

describe('getPack', () => {
  it('surfaces missing_placements rather than hiding the gap', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaign_id: 'c1',
          assets: [],
          missing_placements: ['feed_1x1', 'story_9x16'],
          copy: [],
          links: [],
          guidance: '',
        }),
      }),
    )
    const pack = await getPack('c1')
    expect(pack.missing_placements).toEqual(['feed_1x1', 'story_9x16'])
  })
})

describe('listChannels', () => {
  it('calls the plugin API relative to the origin, not an absolute host', async () => {
    stubSession()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)

    await listChannels()

    const url = fetchMock.mock.calls[0][0] as string
    expect(url).toBe('/api/v1/plugins/marketing/channels')
  })

  it('returns the taxonomy rows as given', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => [
          { key: 'linkedin_organic', label: 'LinkedIn — organic', motion: 'organic', category: 'social', ad_platform: null },
        ],
      }),
    )
    await expect(listChannels()).resolves.toEqual([
      { key: 'linkedin_organic', label: 'LinkedIn — organic', motion: 'organic', category: 'social', ad_platform: null },
    ])
  })

  it('returns an empty list when the body is not an array', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }))
    await expect(listChannels()).resolves.toEqual([])
  })
})

describe('getResults', () => {
  it('keeps leads/conversions/cost as UnmeasuredMetric, distinct from a real click count', async () => {
    stubSession()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          campaigns: [
            {
              campaign_id: 'c1',
              campaign_name: 'Spring',
              clicks: { total: 4, paid: 1, organic: 3, unknown_channel_type: 0 },
              leads: { measurable: false, denominator: 4, reason: 'no transport' },
              conversions: { measurable: false, denominator: null, reason: 'no transport' },
              cost: { measurable: false, denominator: null, reason: 'no transport' },
            },
          ],
          unattributed_clicks: 0,
        }),
      }),
    )
    const results = await getResults()
    expect(results.campaigns[0].clicks.total).toBe(4)
    expect(results.campaigns[0].leads.measurable).toBe(false)
  })
})
