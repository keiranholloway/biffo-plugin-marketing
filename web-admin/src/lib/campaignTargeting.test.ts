import { describe, expect, it } from 'vitest'

import type { Campaign } from './api'
import { channelMotionsFor, hasTargeting, targetChannelKeys } from './campaignTargeting'

const CAMPAIGN: Campaign = {
  id: 'c1',
  name: 'Spring demo push',
  status: 'draft',
  destination_url: null,
}

function campaign(patch: Partial<Campaign>): Campaign {
  return { ...CAMPAIGN, ...patch }
}

describe('channelMotionsFor', () => {
  it('widens only `both`', () => {
    expect([...channelMotionsFor('organic')]).toEqual(['organic'])
    expect([...channelMotionsFor('paid')]).toEqual(['paid'])
    expect([...channelMotionsFor('both')].sort()).toEqual(['organic', 'paid'])
  })
})

describe('targetChannelKeys', () => {
  it('parses the campaign’s one comma-separated column', () => {
    expect(targetChannelKeys(campaign({ target_channel_keys: 'a,b' }))).toEqual(['a', 'b'])
  })

  it('is empty for a campaign that has never been targeted', () => {
    expect(targetChannelKeys(CAMPAIGN)).toEqual([])
    expect(targetChannelKeys(campaign({ target_channel_keys: null }))).toEqual([])
    expect(targetChannelKeys(campaign({ target_channel_keys: '' }))).toEqual([])
  })

  it('drops blank entries rather than yielding keys nothing matches', () => {
    expect(targetChannelKeys(campaign({ target_channel_keys: 'a,, b ,' }))).toEqual(['a', 'b'])
  })
})

describe('hasTargeting', () => {
  it('needs BOTH decisions, since either alone still cannot plan', () => {
    expect(hasTargeting(campaign({ motion: 'organic', target_channel_keys: 'a' }))).toBe(true)
    expect(hasTargeting(campaign({ motion: 'organic' }))).toBe(false)
    expect(hasTargeting(campaign({ target_channel_keys: 'a' }))).toBe(false)
    expect(hasTargeting(CAMPAIGN)).toBe(false)
  })

  it('rejects a motion outside the closed set rather than trusting the row', () => {
    expect(
      hasTargeting(campaign({ motion: 'hybrid' as never, target_channel_keys: 'a' })),
    ).toBe(false)
  })
})
