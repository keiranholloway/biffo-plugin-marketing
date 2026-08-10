import { afterEach, describe, expect, it, vi } from 'vitest'

import { listCampaigns } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('listCampaigns', () => {
  it('calls the plugin API relative to the origin, not an absolute host', async () => {
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }))
    await expect(listCampaigns()).resolves.toEqual([])
  })
})
