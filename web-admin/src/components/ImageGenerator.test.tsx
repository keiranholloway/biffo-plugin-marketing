import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import { ImageGenerator } from './ImageGenerator'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN = 'c1'

describe('ImageGenerator', () => {
  it('generates a still and shows it', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          asset: { id: 'as1' },
          media: { id: 'm1' },
          url: 'https://example.com/still.png',
          ledger: { id: 'l1', cost_usd: 0.04, unpriced: false },
        }),
      }),
    )

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByAltText('Generated still')).toHaveAttribute(
      'src',
      'https://example.com/still.png',
    )
    expect(screen.getByText('Cost: $0.0400')).toBeInTheDocument()
  })

  it('shows "unpriced", never $0, when cost_usd is null', async () => {
    // The null case is load-bearing — image_routes.py's own reasoning.
    stubSession()
    const user = userEvent.setup()
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

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByText('Cost: unpriced')).toBeInTheDocument()
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument()
  })

  it('cannot be submitted with an empty prompt', () => {
    render(<ImageGenerator campaignId={CAMPAIGN} />)
    expect(screen.getByRole('button', { name: /generate still/i })).toBeDisabled()
  })

  it('surfaces a provider failure rather than silently doing nothing', async () => {
    stubSession()
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: async () => ({ detail: 'the image provider returned an error' }),
      }),
    )

    render(<ImageGenerator campaignId={CAMPAIGN} />)
    await user.type(screen.getByLabelText('Prompt'), 'a storefront photo')
    await user.click(screen.getByRole('button', { name: /generate still/i }))

    expect(await screen.findByText(/image provider returned an error/i)).toBeInTheDocument()
  })
})
