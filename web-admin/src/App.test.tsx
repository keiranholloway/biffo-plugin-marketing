import { render, screen } from '@testing-library/react'
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
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => [
          { id: '1', name: 'Spring demo push', status: 'draft', destination_url: null },
        ],
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
