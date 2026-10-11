import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import App from './App'
import type { Api } from './lib/api'

function fakeApi(overrides: Partial<Api> = {}): Api {
  return {
    listCampaigns: vi.fn().mockResolvedValue([]),
    createCampaign: vi.fn().mockResolvedValue({ id: 'c1', name: 'Spring', status: 'draft' }),
    ...overrides,
  } as Api
}

describe('founder campaign form', () => {
  it('creates a draft campaign from the typed fields and sends no status', async () => {
    const api = fakeApi()
    render(<App api={api} />)

    await userEvent.type(screen.getByLabelText(/campaign name/i), 'Spring')
    await userEvent.type(screen.getByLabelText(/brief/i), 'Reach cafes')
    await userEvent.type(screen.getByLabelText(/destination url/i), 'https://example.com')
    await userEvent.selectOptions(screen.getByLabelText(/motion/i), 'organic')
    await userEvent.click(screen.getByLabelText('image'))
    await userEvent.click(screen.getByRole('button', { name: /create draft campaign/i }))

    await waitFor(() => expect(api.createCampaign).toHaveBeenCalledTimes(1))
    expect(api.createCampaign).toHaveBeenCalledWith({
      name: 'Spring',
      brief: 'Reach cafes',
      destination_url: 'https://example.com',
      media_kinds: ['image'],
      motion: 'organic',
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Draft campaign “Spring” created.')
  })

  it('shows the server error when creation is refused', async () => {
    const api = fakeApi({ createCampaign: vi.fn().mockRejectedValue(new Error('nope')) })
    render(<App api={api} />)

    await userEvent.type(screen.getByLabelText(/campaign name/i), 'Spring')
    await userEvent.click(screen.getByRole('button', { name: /create draft campaign/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('nope')
  })

  it('does not submit without a name', async () => {
    const api = fakeApi()
    render(<App api={api} />)
    await waitFor(() => expect(api.listCampaigns).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: /create draft campaign/i })).toBeDisabled()
  })
})
