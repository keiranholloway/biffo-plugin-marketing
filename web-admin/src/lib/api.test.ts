import { afterEach, describe, expect, it, vi } from 'vitest'

import { listCampaigns } from './api'
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
