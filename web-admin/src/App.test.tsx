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

describe('creating a campaign', () => {
  it('submits the form and refreshes the list', async () => {
    const user = userEvent.setup()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => [] })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: '1' }) })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => [
          { id: '1', name: 'Spring', status: 'draft', destination_url: 'https://x/demo' },
        ],
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
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [
        { id: '1', name: 'Spring demo push', status: 'draft', destination_url: null, brief: null },
      ],
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
