import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as auth from '../lib/auth'
import type { Campaign } from '../lib/api'
import { CampaignDetail } from './CampaignDetail'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSession(jwt = 'test-jwt') {
  vi.spyOn(auth, 'getCurrentSession').mockResolvedValue({
    getIdToken: () => ({ getJwtToken: () => jwt }),
  } as never)
}

const CAMPAIGN: Campaign = {
  id: 'c1',
  name: 'Spring demo push',
  status: 'draft',
  destination_url: 'https://example.com/demo',
  brief: null,
  guidance: null,
}

describe('CampaignDetail', () => {
  it('renders the campaign name and every section, and calls back on "Back"', async () => {
    stubSession()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }))

    const onBack = vi.fn()
    const user = userEvent.setup()
    render(<CampaignDetail campaign={CAMPAIGN} onBack={onBack} onCampaignUpdated={() => {}} />)

    expect(screen.getByRole('heading', { name: 'Spring demo push' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Pipeline' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Images' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Distribution pack' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Paid brief pack' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Results' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /back to campaigns/i }))
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('blocks research with no brief, and saving one unlocks it', async () => {
    stubSession()
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.endsWith('/campaigns/c1') && init?.method === 'PATCH') {
        return Promise.resolve({ ok: true, json: async () => ({ ...CAMPAIGN, brief: 'Who this is for.' }) })
      }
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const onCampaignUpdated = vi.fn()
    const user = userEvent.setup()
    render(<CampaignDetail campaign={CAMPAIGN} onBack={() => {}} onCampaignUpdated={onCampaignUpdated} />)

    const research = await screen.findByTestId('stage-research')
    expect(within(research).getByRole('button', { name: /start research/i })).toBeDisabled()

    await user.type(screen.getByLabelText('Brief'), 'Who this is for.')
    await user.click(screen.getByRole('button', { name: /save brief/i }))

    expect(onCampaignUpdated).toHaveBeenCalledWith(expect.objectContaining({ brief: 'Who this is for.' }))
  })
})
