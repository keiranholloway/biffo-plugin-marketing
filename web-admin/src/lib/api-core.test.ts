import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, createRequest } from './api-core'

/**
 * Guards for the distributed admin-API request core (biffo-template#1492),
 * and in particular for the `onError` mapper added so the marketing plugin
 * could adopt it without losing its endpoint-specific messages.
 */

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubFetch(res: Response) {
  const fetchMock = vi.fn().mockResolvedValue(res)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('createRequest without a mapper (the default path)', () => {
  it('throws ApiError carrying the status and the response body', async () => {
    stubFetch(new Response('you may not do that', { status: 403 }))
    const request = createRequest(() => 'tok', '/base')

    await expect(request('GET', '/thing')).rejects.toBeInstanceOf(ApiError)
  })

  it('falls back to statusText when the body is empty', async () => {
    stubFetch(new Response('', { status: 500, statusText: 'Internal Server Error' }))
    const request = createRequest(() => 'tok', '/base')

    await expect(request('GET', '/thing')).rejects.toThrow('Internal Server Error')
  })
})

describe('createRequest with an onError mapper', () => {
  /**
   * THE POINT OF THE HOOK. The mapper must be able to read the JSON body,
   * which means `request()` must not have consumed the stream before calling
   * it. A mapper handed an already-read Response sees an empty body and falls
   * back to a generic message — the silent degradation this exists to stop.
   */
  it('hands the mapper a response whose body is still readable', async () => {
    stubFetch(jsonResponse(403, { detail: 'you are not an admin of this brand' }))
    const request = createRequest(
      () => 'tok',
      '/base',
      async (res, context) => {
        const body = (await res.json()) as { detail?: string }
        throw new Error(`${body.detail} (${res.status}) while trying to ${context}`)
      },
    )

    await expect(request('POST', '/links', {}, '/base', 'mint links')).rejects.toThrow(
      'you are not an admin of this brand (403) while trying to mint links',
    )
  })

  it('passes the per-call context through, and undefined when omitted', async () => {
    const seen: (string | undefined)[] = []
    stubFetch(jsonResponse(500, {}))
    const request = createRequest(
      () => 'tok',
      '/base',
      (_res, context) => {
        seen.push(context)
        throw new Error('mapped')
      },
    )

    await expect(request('GET', '/a', undefined, '/base', 'load campaigns')).rejects.toThrow(
      'mapped',
    )
    stubFetch(jsonResponse(500, {}))
    await expect(request('GET', '/b')).rejects.toThrow('mapped')

    expect(seen).toEqual(['load campaigns', undefined])
  })

  it('never reaches the default ApiError once the mapper has thrown', async () => {
    stubFetch(jsonResponse(403, { detail: 'nope' }))
    const request = createRequest(
      () => 'tok',
      '/base',
      () => {
        throw new Error('mapped')
      },
    )

    // If the default path still ran, this would be an ApiError instead.
    await expect(request('GET', '/thing')).rejects.not.toBeInstanceOf(ApiError)
  })

  /**
   * A mapper is typed `=> never`, but types are not enforcement at runtime and
   * this is the dangerous failure: a mapper that returns normally would let
   * `request()` fall through to the default throw. That is recoverable. What
   * must never happen is a failed request RESOLVING — so this pins that even a
   * misbehaving mapper cannot turn a 403 into a successful call.
   */
  it('still throws if a mapper wrongly returns instead of throwing', async () => {
    stubFetch(jsonResponse(403, { detail: 'nope' }))
    const request = createRequest(() => 'tok', '/base', (() => undefined) as never)

    await expect(request('GET', '/thing')).rejects.toBeInstanceOf(ApiError)
  })

  it('does not run the mapper on a successful response', async () => {
    stubFetch(jsonResponse(200, { ok: true }))
    const onError = vi.fn(() => {
      throw new Error('should not run')
    })
    const request = createRequest(() => 'tok', '/base', onError as never)

    await expect(request('GET', '/thing')).resolves.toEqual({ ok: true })
    expect(onError).not.toHaveBeenCalled()
  })
})
